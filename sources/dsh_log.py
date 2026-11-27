# -*- coding: utf-8 -*-
"""从 DSH 本地会话日志计算用量 —— 零 API key、零网络。

日志：~/.dsh/sessions/<工作区>/session-<uuid>/session.v4.jsonl.zstd
计费事件只有两种：assistant/message、compaction/summary，用量都在 data.usage 里。

设计要点（改代码前先读）：
  1. 只提取 usage/time/source 这几个字段，绝不把对话正文带出本模块（隐私）。
  2. fork / 续接会话会把父会话事件原样复制一份，必须按
     (类型, time_ms, turn, step) 去重，且**先按会话 createdAt 升序**处理：
     父会话先占位，后建的分叉自然被丢弃。不去重会虚高 1.5 倍以上。
  3. 增量缓存按 (文件 size, mtime_ns) 判定；CACHE_VERSION 一改就整体失效。
     不要用字节偏移当游标——zstd 是多帧追加流，不能按字节切。
  4. 依赖：zstandard（Python < 3.14）。也支持 3.14 内置的 compression.zstd，
     并在仓库内 pylibs/ 里找 vendored 副本；都没有时给出可操作的提示。

对外只有 load_events() / as_rows() 两个入口，monitor.py 不需要知道日志格式。
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys
import time

CACHE_VERSION = 4            # 解析逻辑变更时 +1，旧缓存自动失效
LOG_VERSION = "v4"           # session.v4.jsonl.zstd
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BILLING_TYPES = ("assistant/message", "compaction/summary")

# 单位：元 / 1M tokens。没有账单可反解时按平台公开价填；缺省用 default 段。
_DEFAULT_PRICES = {
    "version": 1,
    "effective_from": "2026-11-01",
    "default": {"uncached": 0.6307, "cache_read": 0.2120, "output": 4.1905},
    "prices": {
        "ustc/deepseek-flash": {"uncached": 0.6307, "cache_read": 0.2120, "output": 4.1905},
        "deepseek-account/deepseek-flash": {"uncached": 0.6307, "cache_read": 0.2120,
                                           "output": 4.1905},
        "relay-a/deepseek-flash": {"uncached": 0.8514, "cache_read": 0.2862, "output": 5.6572},
    },
}

LAST_ERROR = None            # 最近一次非致命问题（供 /api/summary 展示）
_PRICE_CACHE = {"path": None, "mtime": None, "table": None}


# --------------------------------------------------------------------------- #
# 路径：全部可用环境变量覆盖，换机器不用改代码
# --------------------------------------------------------------------------- #
def sessions_root():
    """DSH 会话日志根目录。USTC_TOKEN_DSH_ROOT 可覆盖（别人把 .dsh 放在别处时）。"""
    env = os.environ.get("USTC_TOKEN_DSH_ROOT") or os.environ.get("DSH_SESSIONS_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".dsh", "sessions")


def cache_path():
    """增量缓存文件。USTC_TOKEN_CACHE 可覆盖；默认放用户缓存目录（可写、不进仓库）。"""
    env = os.environ.get("USTC_TOKEN_CACHE")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "ustc-token-monitor", "dsh-usage-cache.json")


def prices_path():
    env = os.environ.get("USTC_TOKEN_PRICES")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "prices.json")


# --------------------------------------------------------------------------- #
# zstd 后端：zstandard → 3.14 内置 → 仓库内 pylibs
# --------------------------------------------------------------------------- #
_ZSTD = {"tried": False, "backend": None}


def zstd_backend():
    """返回 ("pkg", zstandard) / ("std", compression.zstd) / None。"""
    if _ZSTD["tried"]:
        return _ZSTD["backend"]
    _ZSTD["tried"] = True
    try:
        import zstandard
        _ZSTD["backend"] = ("pkg", zstandard)
        return _ZSTD["backend"]
    except Exception:
        pass
    try:                                            # Python 3.14+
        from compression import zstd as std_zstd
        _ZSTD["backend"] = ("std", std_zstd)
        return _ZSTD["backend"]
    except Exception:
        pass
    libs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pylibs")
    if os.path.isdir(libs):
        sys.path.insert(0, libs)
        try:
            import zstandard
            _ZSTD["backend"] = ("pkg", zstandard)
            return _ZSTD["backend"]
        except Exception:
            pass
    _ZSTD["backend"] = None
    return None


def has_zstd():
    return zstd_backend() is not None


def zstd_hint():
    return ("读不了 .jsonl.zstd：需要 zstandard 模块。\n"
            "  pip install zstandard        （Python 3.14+ 已内置，可忽略）\n"
            "装不上时仍可用 --source api 从网关账单采集。")


def read_text(path):
    """读一个会话日志文件为文本；未压缩的（无 zstd 魔数）直接按 UTF-8 解。"""
    with open(path, "rb") as f:
        head = f.read(4)
        f.seek(0)
        if head != ZSTD_MAGIC and head[:2] != b"\x28\xb5":
            return f.read().decode("utf-8", "replace")
        be = zstd_backend()
        if be is None:
            raise RuntimeError(zstd_hint())
        kind, mod = be
        if kind == "pkg":
            try:                                    # 多帧追加流：跨帧读
                rd = mod.ZstdDecompressor().stream_reader(f, read_across_frames=True)
            except TypeError:                       # 老版本没有这个参数
                f.seek(0)
                rd = mod.ZstdDecompressor().stream_reader(f)
            with rd:
                return rd.read().decode("utf-8", "replace")
        return mod.decompress(f.read()).decode("utf-8", "replace")     # 3.14 内置


# --------------------------------------------------------------------------- #
# 解析单个文件
# --------------------------------------------------------------------------- #
def _msg_source(d):
    return ((d.get("message") or {}).get("source") or {})


def _first_chunk_ms(stream):
    """首个 stream chunk 的时间（毫秒）；没有就 None。"""
    if not isinstance(stream, list):
        return None
    for c in stream:
        if isinstance(c, dict):
            t = c.get("time")
            if t:
                return t
    return None


def _parse_file(path):
    """解析一个会话日志 → (created_ms, [事件])。坏行跳过，不抛异常。"""
    global LAST_ERROR
    try:
        text = read_text(path)
    except Exception as e:
        # 读失败（文件正被写、zstd 帧不完整、权限…）：不返回"空"，让调用方别把它写进缓存，
        # 下一轮还会重试；否则这个文件会一直缺数据直到 mtime 变化。
        LAST_ERROR = f"{os.path.basename(os.path.dirname(path))}: {e}"
        return 0, [], False
    sid = os.path.basename(os.path.dirname(path))
    created, seeded = 0, False
    step_start = {}                      # (turn, step) -> 毫秒
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        t = o.get("type")
        if t == "session":
            if not created:
                created = o.get("createdAt") or 0
                seeded = bool(o.get("isSeeded"))
            continue
        if t == "step/start":
            d = o.get("data") or {}
            step_start[(d.get("turn"), d.get("step"))] = o.get("time") or 0
            continue
        if t not in BILLING_TYPES:
            continue
        d = o.get("data") or {}
        u = d.get("usage")
        if not isinstance(u, dict) or not u:
            continue
        time_ms = o.get("time") or 0
        if t == "assistant/message":
            src = _msg_source(d)
            turn, step = d.get("turn"), d.get("step")
            t1_ms = _first_chunk_ms(d.get("stream"))
            kind = "call"
            key = f"call|{time_ms}|{turn}|{step}"
        else:                                              # compaction/summary
            src = {"provider": d.get("provider"), "model": d.get("model")}
            turn = d.get("compactionId")
            step = None
            t1_ms = None
            kind = "compact"
            key = f"compact|{time_ms}|{turn}|-"
        out.append({
            "sid": sid, "seeded": seeded, "kind": kind, "key": key,
            "created": created or 0, "turn": turn, "step": step,
            "time_ms": time_ms,
            "t0": time_ms / 1000.0, "t1": (t1_ms / 1000.0) if t1_ms else None,
            "t2": time_ms / 1000.0,
            "provider": src.get("provider"), "model": src.get("model"),
            "uncached": u.get("inputTokens", 0) or 0,
            "cache_read": u.get("cacheReadTokens", 0) or 0,
            "cache_write": u.get("cacheWriteTokens", 0) or 0,
            "output": u.get("outputTokens", 0) or 0,
        })
    # 用 step/start 回填真实开始时刻；回填不到就退一步（绝不能让 t0 == t2）
    for e in out:
        st = step_start.get((e["turn"], e["step"])) if e["kind"] == "call" else None
        if st:
            e["t0"] = st / 1000.0
            e["t0_src"] = "step"
        elif e["t1"]:
            e["t0"] = min(e["t1"], e["t2"]) - 0.001
            e["t0_src"] = "chunk"
        else:
            e["t0"] = e["t2"] - 0.001
            e["t0_src"] = "time"
        if e["t0"] >= e["t2"]:                 # 兜底：时间戳乱序时也不产生负 TTFT
            e["t0"] = (e["t1"] if e["t1"] and e["t1"] < e["t2"] else e["t2"]) - 0.001
            e["t0_src"] = "fallback"
    return created or 0, out, True


# --------------------------------------------------------------------------- #
# 全量扫描（带增量缓存）+ 去重
# --------------------------------------------------------------------------- #
def _log_files(root=None):
    pattern = os.path.join(root or sessions_root(), "*", "*", "*.jsonl.zstd")
    try:
        return sorted(glob.glob(pattern))
    except OSError:
        return []


def _load_cache():
    p = cache_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            blob = json.load(f)
        if blob.get("v") == CACHE_VERSION:
            files = blob.get("files")
            return files if isinstance(files, dict) else {}
    except Exception:
        pass
    return {}


def _save_cache(files):
    p = cache_path()
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": CACHE_VERSION, "files": files}, f, ensure_ascii=False)
        os.replace(tmp, p)                     # 原子替换，别留半个文件
    except Exception:
        pass                                   # 缓存写不进去也不影响统计


def load_events(use_cache=True, root=None):
    """扫描全部会话日志，返回去重后按时间排序的计费事件。"""
    global LAST_ERROR
    files = _log_files(root)
    old = _load_cache() if use_cache else {}
    new_cache, dirty = {}, False
    per_file = []
    for p in files:
        try:
            st = os.stat(p)
            stamp = "%d:%d" % (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
        hit = old.get(p)
        if hit and hit.get("stamp") == stamp and isinstance(hit.get("events"), list):
            new_cache[p] = hit
            per_file.append((hit.get("created") or 0, hit["events"]))
            continue
        created, evs, ok = _parse_file(p)
        if ok:                                  # 读失败的文件不落缓存，下一轮重试
            new_cache[p] = {"stamp": stamp, "created": created, "events": evs}
            dirty = True
        per_file.append((created, evs))
    if dirty and use_cache:
        _save_cache(new_cache)
    # 父会话先占位：分叉会话复制来的同一事件会被丢弃
    per_file.sort(key=lambda x: x[0])
    seen, events = set(), []
    for _created, evs in per_file:
        for e in evs:
            k = e.get("key")
            if not k or k in seen:
                continue
            seen.add(k)
            events.append(e)
    events.sort(key=lambda e: e.get("t0") or 0)
    return events


# --------------------------------------------------------------------------- #
# 价目表与金额
# --------------------------------------------------------------------------- #
def price_table():
    """读 prices.json；文件不存在/坏了就用内置默认值（绝不因此崩掉）。"""
    global LAST_ERROR
    p = prices_path()
    try:
        mt = os.stat(p).st_mtime_ns
    except OSError:
        return dict(_DEFAULT_PRICES)
    if _PRICE_CACHE["path"] == p and _PRICE_CACHE["mtime"] == mt and _PRICE_CACHE["table"]:
        return _PRICE_CACHE["table"]
    try:
        with open(p, "r", encoding="utf-8") as f:
            tbl = json.load(f)
        if not isinstance(tbl, dict):
            raise ValueError("prices.json 顶层应为对象")
    except Exception as e:
        LAST_ERROR = f"prices.json 读取失败，改用内置单价：{e}"
        return dict(_DEFAULT_PRICES)
    _PRICE_CACHE.update(path=p, mtime=mt, table=tbl)
    return tbl


def price_of(provider, model, table=None):
    """按 provider/model 取单价；没有就退 * / default；都没有返回 None（金额写 NULL）。"""
    tbl = table or price_table()
    prices = tbl.get("prices") or {}
    if not isinstance(prices, dict):
        prices = {}
    for key in (f"{provider}/{model}", f"{provider}/*", f"*/{model}"):
        p = prices.get(key)
        if isinstance(p, dict):
            return p
    d = tbl.get("default")
    return d if isinstance(d, dict) else None


def compute_cost(e, table=None):
    p = price_of(e.get("provider"), e.get("model"), table)
    if not p:
        return None
    try:
        uncached = e["uncached"] * float(p.get("uncached") or 0)
        cached = (e["cache_read"] + e["cache_write"]) * float(p.get("cache_read") or 0)
        out = e["output"] * float(p.get("output") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return (uncached + cached + out) / 1e6


# --------------------------------------------------------------------------- #
# 转成 requests 表的行（列顺序必须与 monitor.upsert_rows 的 SQL 完全一致）
# --------------------------------------------------------------------------- #
ROW_COLUMNS = ("request_id", "t0", "t1", "t2", "model", "mgroup", "prompt", "completion",
               "cached", "reasoning", "spend", "user_agent", "finish",
               "provider", "usage_source")


def as_rows(events, table=None):
    tbl = table or price_table()
    rows = []
    for e in events:
        provider = e.get("provider") or "unknown"
        model = e.get("model") or "?"
        cost = compute_cost(e, tbl)
        rows.append((
            "dsh:" + e["key"],
            e["t0"], e["t1"], e["t2"],
            model,
            provider + "/" + model,                      # mgroup = 来源维度
            e["uncached"] + e["cache_read"] + e["cache_write"],   # prompt 必须含缓存
            e["output"],
            e["cache_read"] + e["cache_write"],
            0,                                           # reasoning：DSH 未单列
            None if cost is None else round(cost, 6),    # 估算金额；无单价写 NULL
            "dsh-local",
            "ok",
            provider,
            "estimated",
        ))
    return rows


# --------------------------------------------------------------------------- #
# 诊断（--once / /api/reconcile 用）
# --------------------------------------------------------------------------- #
def stats(events=None, root=None):
    ev = load_events(root=root) if events is None else events
    files = _log_files(root)
    prov = {}
    first = last = None
    for e in ev:
        p = e.get("provider") or "unknown"
        prov[p] = prov.get(p, 0) + 1
        t = e.get("t0") or 0
        first = t if first is None or t < first else first
        last = t if last is None or t > last else last
    srcs = {}
    for e in ev:
        srcs[e.get("t0_src") or "?"] = srcs.get(e.get("t0_src") or "?", 0) + 1
    return {
        "root": root or sessions_root(),
        "files": len(files),
        "events": len(ev),
        "t0_sources": srcs,
        "providers": prov,
        "first": first,
        "last": last,
        "zstd": (zstd_backend() or ("none",))[0],
        "cache": cache_path(),
        "prices": prices_path(),
        "error": LAST_ERROR,
    }
