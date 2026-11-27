#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USTC 大模型平台 Token 速率监测器
--------------------------------
数据源: 默认本机 DSH 会话日志（零密钥、零网络）；--source api 可选网关账单
指标  : 平均生成速度 (tok/s)、1h 用量、3h/12h 限额进度、全天用量与费用

用法:
  python monitor.py --once           # 采集一轮并打印摘要，用于核对
  python monitor.py --serve          # 常驻采集 + 看板 http://127.0.0.1:8790
  python monitor.py --serve --port 9000

依赖: 标准库 + zstandard（仅 Python<3.14 读 .jsonl.zstd 需要；3.14+ 内置 compression.zstd）
密钥: local 模式完全不读凭证；api 模式读 USTC_API_KEY 或 ~/.dsh/.credentials.yaml
"""

import argparse
import collections
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "https://api.llm.ustc.edu.cn"
TZ = timezone(timedelta(hours=8))          # Asia/Shanghai (无夏令时)
POLL_SECONDS = 10
BUDGET_REFRESH_SECONDS = 60
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "usage.db")

# 数据源：local（本机 DSH 会话日志，默认、零密钥）| api（网关账单）| both（账单优先）
SOURCE_CHOICES = ("local", "api", "both")
SOURCE = (os.environ.get("USTC_TOKEN_SOURCE") or "local").strip().lower()
if SOURCE not in SOURCE_CHOICES:
    SOURCE = "local"

MASK = lambda k: (k[:3] + "..." + k[-4:]) if k and len(k) > 10 else "***"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def parse_ts(s):
    """解析 LiteLLM 时间戳为 UTC epoch 秒。支持 Z / +00:00 / 6 位以上小数。"""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*\.)(\d+)([+-]\d{2}:\d{2})$", s)
    if m:  # 统一小数位到 6 位
        frac = (m.group(2) + "000000")[:6]
        s = m.group(1) + frac + m.group(3)
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def fmt_int(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def local(epoch):
    return datetime.fromtimestamp(epoch, TZ)


def load_key():
    key = os.environ.get("USTC_API_KEY", "").strip()
    if key:
        return key, "env"
    path = os.path.expanduser("~/.dsh/.credentials.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = re.search(r"USTC_API_KEY:\s*[\"']?(sk-[A-Za-z0-9_\-]+)", f.read())
        if m:
            return m.group(1), path
    except OSError:
        pass
    return "", None


# 懒加载：local 模式绝不触碰凭证文件（零密钥承诺）；只有 api 路径才会调 ensure_key()
KEY, KEY_SRC = "", None


def ensure_key():
    global KEY, KEY_SRC
    if not KEY:
        KEY, KEY_SRC = load_key()
    return KEY


_OPENER = None


def _builder():
    """默认绕过系统代理的 opener。

    Windows 的 WinINET 代理（注册表 Internet Settings）会被 urllib 自动读走，
    而 curl / DSH 只认环境变量，于是出现"curl 通、采集断"的怪象——系统代理
    （如 Clash 7890）会重置到国内站点的连接。需要代理时设 USTC_PROXY。
    """
    global _OPENER
    if _OPENER is None:
        proxy = os.environ.get("USTC_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        _OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
        )
    return _OPENER


def api_get(path, params=None, timeout=25):
    if not ensure_key():
        raise RuntimeError("未配置 API Key：设置 USTC_API_KEY 或写进 ~/.dsh/.credentials.yaml，"
                           "也可以改用 --source local（本机日志，无需密钥）")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"},
    )
    with _builder().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    t0 REAL, t1 REAL, t2 REAL,
    model TEXT, mgroup TEXT, prompt INTEGER, completion INTEGER,
    cached INTEGER, reasoning INTEGER,
    spend REAL, user_agent TEXT, finish TEXT,
    provider TEXT NOT NULL DEFAULT 'ustc',
    usage_source TEXT NOT NULL DEFAULT 'billing'
);
CREATE INDEX IF NOT EXISTS idx_t0 ON requests(t0);
CREATE TABLE IF NOT EXISTS budget_snap (
    ts REAL PRIMARY KEY, payload TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(requests)")}
        if "mgroup" not in cols:
            c.execute("ALTER TABLE requests ADD COLUMN mgroup TEXT")
        # 老库没有这两列：provider 区分平台，usage_source 区分"账单真值 / 单价估算"
        if "provider" not in cols:
            c.execute("ALTER TABLE requests ADD COLUMN provider TEXT NOT NULL DEFAULT 'ustc'")
        if "usage_source" not in cols:
            c.execute("ALTER TABLE requests ADD COLUMN usage_source TEXT NOT NULL DEFAULT 'billing'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prov_t0 ON requests(provider, t0)")
        # 老数据没有 model_group，用 model 顶上（两者在平台上几乎总是同名）
        c.execute("UPDATE requests SET mgroup = model WHERE mgroup IS NULL OR mgroup = ''")


def upsert_rows(rows):
    if not rows:
        return 0
    # 列顺序必须与 sources.dsh_log.ROW_COLUMNS / row_to_tuple 完全一致
    sql = """INSERT INTO requests
             (request_id,t0,t1,t2,model,mgroup,prompt,completion,cached,reasoning,spend,
              user_agent,finish,provider,usage_source)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(request_id) DO UPDATE SET
               t0=excluded.t0, t1=excluded.t1, t2=excluded.t2, model=excluded.model,
               mgroup=excluded.mgroup,
               prompt=excluded.prompt, completion=excluded.completion, cached=excluded.cached,
               reasoning=excluded.reasoning, spend=excluded.spend,
               user_agent=excluded.user_agent, finish=excluded.finish,
               provider=excluded.provider, usage_source=excluded.usage_source"""
    with db() as c:
        before = c.total_changes
        c.executemany(sql, rows)
        return c.total_changes - before


def row_to_tuple(r):
    meta = r.get("metadata") or {}
    usage = meta.get("usage_object") or {}
    pt = usage.get("prompt_tokens_details") or {}
    ct = usage.get("completion_tokens_details") or {}
    model = r.get("model") or r.get("model_group") or "?"
    group = r.get("model_group") or model
    return (
        r.get("request_id"),
        parse_ts(r.get("startTime")),
        parse_ts(r.get("completionStartTime")),
        parse_ts(r.get("endTime")),
        model,
        group,
        int(r.get("prompt_tokens") or 0),
        int(r.get("completion_tokens") or 0),
        int(pt.get("cached_tokens") or 0),
        int(ct.get("reasoning_tokens") or 0),
        float(r.get("spend") or 0.0),
        (meta.get("user_agent") or "")[:120],
        r.get("finish_reason") or "",
        "ustc",          # provider：账单行都来自 USTC 网关
        "billing",       # usage_source：金额是账单真值
    )


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
STATE = {
    "last_ok": 0.0,
    "last_err": None,
    "polls": 0,
    "rows": 0,
    "budget": None,
    "budget_ts": 0.0,
    "key_info": None,
    "source": SOURCE,
    "local_events": 0,
}
LOCK = threading.Lock()


def fetch_rows(full=False, page_size=100, max_pages=12):
    """增量拉取：默认只翻到"遇到已知 request_id"为止；full=True 时拉完整区间。"""
    today = local(time.time()).date()
    start = (today - timedelta(days=2)).isoformat()
    end = (today + timedelta(days=2)).isoformat()
    out, page = [], 1
    while page <= max_pages:
        j = api_get("/spend/logs/v2", {
            "start_date": start, "end_date": end,
            "page": page, "page_size": page_size,
        })
        data = j.get("data") or []
        if not data:
            break
        out.extend(data)
        if not full:
            ids = [r.get("request_id") for r in data if r.get("request_id")]
            if ids and known_count(ids) > 0:
                break
        if page >= int(j.get("total_pages") or 1):
            break
        page += 1
    return out


DEFAULT_BACKFILL_START = "2026-10-29"


def backfill(start=DEFAULT_BACKFILL_START):
    """按天回填全部历史请求行（平台按 UTC 解释日期，这里按 UTC 日切片即可）。

    start 为本地日期（YYYY-MM-DD），默认平台开通日 2026-10-29；可用 --since 覆盖。
    """
    from datetime import date
    if isinstance(start, str):
        start = start.strip()
    d = start if isinstance(start, date) else date.fromisoformat(start)
    today = local(time.time()).date()
    total_new = 0
    while d <= today:
        nxt = (d + timedelta(days=1)).isoformat()
        page, got = 1, 0
        while page <= 80:
            j = api_get("/spend/logs/v2", {
                "start_date": d.isoformat(), "end_date": nxt,
                "page": page, "page_size": 100,
            })
            data = j.get("data") or []
            tuples = [row_to_tuple(r) for r in data if r.get("request_id")]
            total_new += upsert_rows(tuples)
            got += len(data)
            if not data or page >= int(j.get("total_pages") or 1):
                break
            page += 1
        if got:
            print(f"  {d}  {got:>5} 行", flush=True)
        d += timedelta(days=1)
    print(f"回填完成，新增/更新 {total_new} 行")


def known_count(ids):
    if not ids:
        return 0
    q = "SELECT COUNT(*) FROM requests WHERE request_id IN (%s)" % ",".join("?" * len(ids))
    with db() as c:
        return c.execute(q, ids).fetchone()[0]


def refresh_key_info():
    info = api_get("/key/info")
    d = info.get("info") or info
    budgets = []
    usage = d.get("budget_limits_usage") or {}
    for b in (d.get("budget_limits") or []):
        dur = b.get("budget_duration") or "?"
        key = dur if dur in usage else None
        cur = None
        if key and isinstance(usage.get(key), dict):
            cur = usage[key].get("current_spend")
        budgets.append({
            "window": dur,
            "used": float(cur) if cur is not None else None,
            "max": float(b.get("max_budget") or 0),
            "reset_at": b.get("reset_at"),
        })
    with LOCK:
        STATE["budget"] = budgets
        STATE["budget_ts"] = time.time()
        STATE["key_info"] = {
            "alias": d.get("key_alias"),
            "rpm": d.get("rpm_limit"),
            "tpm": d.get("tpm_limit"),
            "spend": d.get("spend"),
            "status": d.get("status"),
            "last_active": d.get("last_active"),
        }


TRANSIENT_RETRIES = (0.0, 1.5, 4.0)  # 秒；平台偶发 5xx 时在同一轮内重试


def _transient(e):
    """平台侧抖动（5xx / 限流 / 网络）值得当场重试，我们自己的 bug 不值得。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code == 429
    return isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError))


def _err_text(e):
    """把异常翻成人话，看板状态栏直接用。"""
    if isinstance(e, urllib.error.HTTPError):
        hint = {401: "密钥无效", 403: "无权限", 429: "请求过快被限流",
                502: "网关错误", 503: "平台暂时不可用", 504: "网关超时"}.get(e.code, "")
        return f"平台返回 {e.code}" + (f"（{hint}）" if hint else "")
    if isinstance(e, urllib.error.URLError):
        return f"网络不可达：{e.reason}"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "请求超时"
    return f"{type(e).__name__}: {e}"


def _fetch_with_retry(full, verbose):
    last = None
    for i, delay in enumerate(TRANSIENT_RETRIES):
        if delay:
            time.sleep(delay)
        try:
            return fetch_rows(full=full)
        except Exception as e:
            last = e
            if not _transient(e):
                raise
            if verbose and i + 1 < len(TRANSIENT_RETRIES):
                print(f"  采集暂时失败（{_err_text(e)}），{TRANSIENT_RETRIES[i + 1]}s 后重试",
                      file=sys.stderr)
    raise last


def poll_once(verbose=True, full=False, force_full=None):
    try:
        now0 = time.time()
        if force_full is not None:
            full = force_full
        else:
            full = full or (now0 - STATE.get("last_full", 0) > 1800)
        rows = _fetch_with_retry(full, verbose)
        tuples = [row_to_tuple(r) for r in rows if r.get("request_id")]
        changed = upsert_rows(tuples)
        now = time.time()
        if full:
            STATE["last_full"] = now
        if now - STATE["budget_ts"] > BUDGET_REFRESH_SECONDS:
            try:
                refresh_key_info()
            except Exception as e:  # 预算失败不阻断主采集
                if verbose:
                    print(f"  key/info 失败: {e}", file=sys.stderr)
        with LOCK:
            STATE.update(last_ok=now, last_err=None, polls=STATE["polls"] + 1,
                         rows=len(tuples))
        return changed
    except Exception as e:
        with LOCK:
            STATE["last_err"] = _err_text(e)
            STATE["polls"] += 1
        if verbose:
            print(f"采集失败: {_err_text(e)}", file=sys.stderr)
        return -1


def poll_local(verbose=True):
    """扫描本机 DSH 会话日志 → upsert（幂等，重复跑行数不变）。不读密钥、不发请求。"""
    try:
        from sources.dsh_log import as_rows, load_events
        events = load_events()
        rows = as_rows(events)
        changed = upsert_rows(rows)
        with LOCK:
            STATE.update(last_ok=time.time(), last_err=None, polls=STATE["polls"] + 1,
                         rows=len(rows), local_events=len(events))
        return changed
    except Exception as e:
        with LOCK:
            STATE["last_err"] = _err_text(e)
            STATE["polls"] += 1
        if verbose:
            print(f"本地日志采集失败: {_err_text(e)}", file=sys.stderr)
        return -1


def _usage_of(r):
    return r.get("usage_source") or "billing"


def _usage_for(mode):
    """local 看估算行（本机日志产物），api/both 看账单行（账单优先，不重复计数）。"""
    return "estimated" if mode == "local" else "billing"


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def group_of(r):
    """来源名：优先 model_group，缺省退回 model。"""
    return r.get("mgroup") or r.get("model") or "?"


def build_sources(allrows):
    """全部历史按来源聚合，用于看板下拉框。金额缺价时返回 None，不写 0。"""
    agg_src = {}
    for r in allrows:
        name = group_of(r)
        s = agg_src.setdefault(name, {"name": name, "req": 0, "tokens": 0,
                                      "spend": 0.0, "known": 0, "last": 0.0,
                                      "provider": r.get("provider") or "ustc",
                                      "usage_source": _usage_of(r)})
        s["req"] += 1
        s["tokens"] += r["prompt"] + r["completion"]
        if r["spend"] is not None:
            s["spend"] += r["spend"]
            s["known"] += 1
        s["last"] = max(s["last"], r["t0"])
    out = []
    for s in agg_src.values():
        s["spend"] = round(s["spend"], 3) if s.pop("known") else None
        s["last_local"] = local(s.pop("last")).strftime("%m-%d %H:%M")
        out.append(s)
    return sorted(out, key=lambda x: -x["tokens"])


# 折轴图的细度：格子宽度（秒）。窗口固定 60 格，所以 30s ≈ 近 30 分钟、1h ≈ 近 60 小时
BUCKET_CHOICES = (30, 60, 300, 900, 1800, 3600)
BUCKET_DEFAULT = 60
# 空档折叠阈值：固定 15 格，换算成时间就是 15 × 细度
# （30s→7.5m，1m→15m，5m→1h15m，15m→3h45m，30m→7h30m，1h→15h）
GAP_CELLS = 15


def bucket_norm(v):
    """把用户传的细度收拢到最接近的合法值。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return BUCKET_DEFAULT
    return min(BUCKET_CHOICES, key=lambda x: abs(x - v))


def summarize(window_hours=1.0, recent=12, source=None, gap_min=None, bucket_s=BUCKET_DEFAULT,
              provider=None, usage=None):
    """usage=None/"auto" 时按当前 SOURCE 只统计一份数据：
    local → usage_source='estimated'（本机日志 + 单价估算），api/both → 'billing'（账单真值）。
    两份互斥，绝不求和，否则同一次调用会被算两遍。
    gap_min 保留只为兼容旧调用方；空档阈值固定按 GAP_CELLS 格算，与细度联动。"""
    now = time.time()
    day_start = local(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    w_start = now - window_hours * 3600
    m5_start = now - 300

    with db() as c:
        day_rows = [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE t0 >= ? ORDER BY t0 ASC", (day_start,)
        )]
        allrows = [dict(r) for r in c.execute("SELECT * FROM requests ORDER BY t0")]

    u = _usage_for(SOURCE) if usage in (None, "auto", "") else str(usage)
    if u in ("estimated", "billing"):
        allrows = [r for r in allrows if _usage_of(r) == u]
        day_rows = [r for r in day_rows if _usage_of(r) == u]
    if provider:
        allrows = [r for r in allrows if (r.get("provider") or "ustc") == provider]
        day_rows = [r for r in day_rows if (r.get("provider") or "ustc") == provider]
    sources = build_sources(allrows)
    if source:
        day_rows = [r for r in day_rows if group_of(r) == source]
        allrows = [r for r in allrows if group_of(r) == source]
    rows = day_rows

    def agg(sel):
        total = sum(r["prompt"] + r["completion"] for r in sel)
        comp = sum(r["completion"] for r in sel)
        dec = sum((r["t2"] - r["t1"]) for r in sel
                  if r["t1"] and r["t2"] and r["t2"] > r["t1"] and r["completion"] > 0)
        known = [r["spend"] for r in sel if r["spend"] is not None]
        return {
            "req": len(sel),
            "prompt": sum(r["prompt"] for r in sel),
            "completion": comp,
            "total": total,
            "cached": sum(r["cached"] for r in sel),
            # 没有单价的行不参与求和：全没价时返回 None（前端显示 –），绝不写 0 假装免费
            "spend": round(sum(known), 4) if known else None,
            "spend_cov": "%d/%d" % (len(known), len(sel)),
            "decode_s": dec,
            "tps": (comp / dec) if dec > 0 else None,
        }

    h1 = agg([r for r in rows if r["t0"] >= w_start])
    m5 = agg([r for r in rows if r["t0"] >= m5_start])
    day = agg(rows)

    # 最近一次有调用的 5 分钟窗口：有活动就是滚动 5 分钟，空闲时回退到最后活跃的那 5 分钟
    last_t0 = max((r["t0"] for r in rows), default=None)
    if last_t0 is None:
        m5a = {"req": 0, "tokens": 0, "total": 0, "tps": None, "idle_s": None,
               "start_local": None, "end_local": None, "last_call_local": None}
    else:
        end = now if (now - last_t0) <= 300 else min(now, last_t0 + 60)
        start = end - 300
        a = agg([r for r in rows if start <= r["t0"] <= end])
        m5a = {
            "req": a["req"], "tokens": a["completion"], "total": a["total"],
            "spend": a["spend"], "tps": a["tps"], "decode_s": a["decode_s"],
            "idle_s": int(now - last_t0),
            "start_local": local(start).strftime("%H:%M"),
            "end_local": local(end).strftime("%H:%M"),
            "last_call_local": local(last_t0).strftime("%H:%M:%S"),
        }

    # 首 token 延迟（仅 1h、有 t1 的请求）
    ttf = [r["t1"] - r["t0"] for r in rows
           if r["t0"] >= w_start and r["t1"] and r["t1"] >= r["t0"]]
    ttft_ms = (sum(ttf) / len(ttf) * 1000) if ttf else None

    # 分钟序列（最近 60 分钟 + 全天小时序列）
    def bucket(sel, step, anchor, n):
        out = [{"t": anchor + i * step, "prompt": 0, "completion": 0,
                "dec": 0.0, "comp": 0, "req": 0} for i in range(n)]
        for r in sel:
            idx = int((r["t0"] - anchor) // step)
            if 0 <= idx < n:
                b = out[idx]
                b["prompt"] += r["prompt"]
                b["completion"] += r["completion"]
                b["req"] += 1
                if r["t1"] and r["t2"] and r["t2"] > r["t1"] and r["completion"] > 0:
                    b["dec"] += r["t2"] - r["t1"]
                    b["comp"] += r["completion"]
        for b in out:
            b["tps"] = (b["comp"] / b["dec"]) if b["dec"] > 0 else None
            b["t_local"] = local(b["t"]).strftime("%H:%M")
        return out

    bucket_s = bucket_norm(bucket_s)
    minute_anchor = (now // 60) * 60 - 59 * 60
    minutes = bucket([r for r in rows if r["t0"] >= minute_anchor], 60, minute_anchor, 60)

    # 折轴（时间轴压缩）：单段空档超过阈值（默认 30 分钟）就压成一个断点格子，其余照常；
    # 这样闲置几小时后仍能看到"上次活动长什么样 —— 空了多久 —— 现在"。
    minutes_cut = []
    gap_s = GAP_CELLS * bucket_s
    last_any = max((r["t0"] for r in allrows), default=None)
    if last_any is not None:
        lo = min(now - 48 * 3600, last_any - 1800)          # 至少覆盖上一次活动
        lo = max(lo, now - 4000 * bucket_s)                  # 30s 细度下别真铺 48 小时的格子
        a0 = (int(lo) // bucket_s + 1) * bucket_s
        cnt = max(1, int((now - a0) // bucket_s) + 1)
        raw = bucket([r for r in allrows if r["t0"] >= a0], bucket_s, a0, cnt)

        # 时间标签：30s 带秒、跨天窗口带日期，其余只到分
        # 可见窗口 = 60 格 × 细度；跨天时只在 00:00 那格写日期，其余写时分（标签短才排得开）
        visible_s = min(cnt, 60) * bucket_s
        for b in raw:
            d = local(b["t"])
            if bucket_s < 60:
                b["t_local"] = d.strftime("%H:%M:%S")
            elif visible_s > 24 * 3600 and d.hour == 0 and d.minute == 0:
                b["t_local"] = d.strftime("%m-%d")
            else:
                b["t_local"] = d.strftime("%H:%M")

        def _gap_label(sec):
            m = max(1, int(round(sec / 60.0)))
            if m < 60:
                return f"{m}m"
            if m < 1440:
                return f"{m // 60}h{m % 60:02d}m"
            d, rem = divmod(m, 1440)
            return f"{d}d" + (f"{rem // 60}h" if rem >= 60 else "")

        # 固定 15 格：空档长度的判定不看分钟数，只看"连着空了多少格"（≥15 格即折）
        gap_cells = GAP_CELLS
        gap_s = gap_cells * bucket_s
        runs = {}                                           # 空档起点 -> 空档格数
        i = 0
        while i < len(raw):
            if raw[i]["req"]:
                i += 1
                continue
            j = i
            while j < len(raw) and not raw[j]["req"]:
                j += 1
            if j - i >= gap_cells:                          # 连着空 15 格（含）就算空档
                runs[i] = j - i
            i = j
        i = 0
        while i < len(raw):
            ln = runs.get(i)
            if ln:
                to_ts = a0 + (i + ln) * bucket_s if i + ln < len(raw) else now
                minutes_cut.append({
                    "brk": True, "gap_s": ln * bucket_s, "gap_label": _gap_label(ln * bucket_s),
                    "from_local": local(a0 + i * bucket_s).strftime("%m-%d %H:%M"),
                    "to_local": local(to_ts).strftime("%m-%d %H:%M"),
                })
                i += ln
            else:
                minutes_cut.append(raw[i])
                i += 1
        if len(minutes_cut) > 60:
            minutes_cut = minutes_cut[-60:]
        # 压缩模式下把开头的空白格子去掉（它们只是窗口起点带来的填充）
        if any(x.get("brk") for x in minutes_cut):
            while minutes_cut and not minutes_cut[0].get("brk") and not minutes_cut[0]["req"]:
                minutes_cut.pop(0)
        while minutes_cut and minutes_cut[0].get("brk"):
            minutes_cut.pop(0)

    hour_anchor = day_start
    hours = bucket(rows, 3600, hour_anchor, 24)

    # ---- 历史（全部已入库请求，按本地日聚合） ----
    hist_map = {}
    for r in allrows:
        k = local(r["t0"]).date().isoformat()
        h = hist_map.setdefault(k, {"date": k, "req": 0, "prompt": 0, "completion": 0,
                                    "spend": 0.0, "known": 0, "dec": 0.0, "comp": 0,
                                    "ttf": [], "cached": 0})
        h["req"] += 1
        h["prompt"] += r["prompt"]
        h["completion"] += r["completion"]
        h["cached"] += r["cached"]
        if r["spend"] is not None:
            h["spend"] += r["spend"]
            h["known"] += 1
        if r["t1"] and r["t2"] and r["t2"] > r["t1"] and r["completion"] > 0:
            h["dec"] += r["t2"] - r["t1"]
            h["comp"] += r["completion"]
        if r["t1"] and r["t1"] >= r["t0"]:
            h["ttf"].append(r["t1"] - r["t0"])
    history = []
    for k in sorted(hist_map):
        h = hist_map[k]
        history.append({
            "date": k,
            "req": h["req"],
            "tokens": h["prompt"] + h["completion"],
            "spend": round(h["spend"], 3) if h["known"] else None,
            "tps": round(h["comp"] / h["dec"], 1) if h["dec"] > 0 else None,
            "ttft_ms": round(sum(h["ttf"]) / len(h["ttf"]) * 1000) if h["ttf"] else None,
        })

    all_dec = sum(h["dec"] for h in hist_map.values())
    all_comp = sum(h["comp"] for h in hist_map.values())
    all_ttf = [x for h in hist_map.values() for x in h["ttf"]]
    _known = sum(h["known"] for h in hist_map.values())
    _allreq = sum(h["req"] for h in hist_map.values())
    alltime = {
        "days": len(hist_map),
        "req": _allreq,
        "tokens": sum(h["prompt"] + h["completion"] for h in hist_map.values()),
        "spend": round(sum(h["spend"] for h in hist_map.values()), 3) if _known else None,
        "spend_cov": "%d/%d" % (_known, _allreq),
        "tps": round(all_comp / all_dec, 1) if all_dec > 0 else None,
        "ttft_ms": round(sum(all_ttf) / len(all_ttf) * 1000) if all_ttf else None,
    }

    with LOCK:
        # local 模式没有 key/info（零密钥），额度卡整块不显示，而不是显示 0
        budgets = list(STATE["budget"] or []) if SOURCE != "local" else []
        ki = dict(STATE["key_info"] or {})
        st = {k: STATE[k] for k in ("last_ok", "last_err", "polls", "rows")}

    for b in budgets:
        ra = parse_ts(b["reset_at"]) if b["reset_at"] else None
        b["reset_in_s"] = max(0, int(ra - now)) if ra else None
        if b.get("used") is not None and b.get("max"):
            b["pct"] = round(b["used"] / b["max"] * 100, 1)
        else:
            b["pct"] = None

    recent_rows = sorted(rows, key=lambda r: r["t0"], reverse=True)[:recent]
    recent_list = []
    for r in recent_rows:
        dec = (r["t2"] - r["t1"]) if (r["t1"] and r["t2"] and r["t2"] > r["t1"]) else 0
        recent_list.append({
            "time": local(r["t0"]).strftime("%H:%M:%S"),
            "model": r["model"],
            "prompt": r["prompt"],
            "completion": r["completion"],
            "cached": r["cached"],
            "tps": round(r["completion"] / dec, 1) if dec > 0 and r["completion"] > 0 else None,
            "ttft_ms": int((r["t1"] - r["t0"]) * 1000) if r["t1"] else None,
            "dur_s": round((r["t2"] - r["t0"]), 1) if r["t2"] else None,
            "spend": None if r["spend"] is None else round(r["spend"], 6),
            "finish": r["finish"],
        })

    return {
        "now_local": local(now).strftime("%Y-%m-%d %H:%M:%S"),
        "day_label": local(now).strftime("%Y-%m-%d"),
        "source": source,
        "provider": provider,
        "mode": SOURCE,
        "usage_source": u,
        "sources": sources,
        "poll": {
            "last_ok_ago_s": int(now - st["last_ok"]) if st["last_ok"] else None,
            "last_err": st["last_err"],
            "polls": st["polls"],
        },
        "key": ki,
        "h1": h1, "m5": m5, "m5a": m5a, "day": day,
        "ttft_ms": round(ttft_ms) if ttft_ms else None,
        "budgets": budgets,
        "minutes": minutes,
        "minutes_cut": minutes_cut,
        "gap_cells": GAP_CELLS,
        "gap_s": gap_s,
        "bucket_s": bucket_s,
        "points": 60,
        "span_s": 60 * bucket_s,
        "buckets": list(BUCKET_CHOICES),
        "hours": hours,
        "history": history,
        "alltime": alltime,
        "recent": recent_list,
    }


# --------------------------------------------------------------------------- #
# 对账：本机日志 vs 网关账单（改造自 codex-bridge/compare.py）
# --------------------------------------------------------------------------- #
KNOWN_GAPS = [
    {"what": "账单比本地多约 110 条", "why": "网关把中断/失败请求也记成零 token 行",
     "handling": "不追平，属预期"},
    {"what": "账单多约 17 条（10-29/10-30）", "why": "DSH 尚未开始记录用量",
     "handling": "忽略"},
    {"what": "TTFT 本地偏高约 470ms", "why": "本地口径含 harness 装配/工具结果处理开销",
     "handling": "不参与对账，仅标注"},
]

_RZERO = {"n": 0, "prompt": 0, "completion": 0, "spend": 0.0, "known": 0,
          "dec": 0.0, "comp": 0, "ttf": []}


def _rzero():
    return dict(_RZERO, ttf=[])


def _rpct(a, b):
    if not b:
        return None
    return round((a - b) / b * 100, 2)


def reconcile(days=30, provider=None):
    """逐日对账：本机日志（估算口径） vs requests 里 usage_source='billing' 的账单行。

    只读接口：不改库、不改数据。days 为回看天数（含今天）。
    provider 为 None 时只对比"账单里真正出现过的 provider"（默认就是 ustc），
    本机日志里那些没走网关的 provider（例如别人代充的账号）单独列在 unbilled，
    否则它们会被算成"对不上"，看起来像 bug。
    """
    from sources.dsh_log import compute_cost, load_events, price_table, stats as _stats
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(365, days))
    now = time.time()
    since = now - days * 86400

    events = [e for e in load_events() if (e.get("t0") or 0) >= since]
    if provider:
        events = [e for e in events if (e.get("provider") or "") == provider]
    tbl = price_table()

    # provider -> 日期 -> 聚合
    loc = {}
    for e in events:
        pv = e.get("provider") or "unknown"
        k = local(e["t0"]).date().isoformat()
        x = loc.setdefault(pv, {}).setdefault(k, _rzero())
        x["n"] += 1
        x["prompt"] += e["uncached"] + e["cache_read"] + e["cache_write"]
        x["completion"] += e["output"]
        c = compute_cost(e, tbl)
        if c is not None:
            x["spend"] += c
            x["known"] += 1
        if e["t1"] and e["t2"] > e["t1"] and e["output"] > 0:
            x["dec"] += e["t2"] - e["t1"]
            x["comp"] += e["output"]
        if e["t1"] and e["t1"] >= e["t0"]:
            x["ttf"].append(e["t1"] - e["t0"])

    try:
        with db() as c:
            bill_rows = [dict(r) for r in c.execute(
                "SELECT * FROM requests WHERE t0 >= ? AND (usage_source = 'billing' "
                "OR usage_source IS NULL)", (since,))]
    except sqlite3.Error:                       # 极老的库没有 usage_source：整表当账单
        with db() as c:
            bill_rows = [dict(r) for r in c.execute(
                "SELECT * FROM requests WHERE t0 >= ?", (since,))]
    if provider:
        bill_rows = [r for r in bill_rows if (r.get("provider") or "ustc") == provider]
    bil, bil_prov = {}, set()
    for r in bill_rows:
        pv = r.get("provider") or "ustc"
        bil_prov.add(pv)
        k = local(r["t0"]).date().isoformat()
        x = bil.setdefault(k, _rzero())
        x["n"] += 1
        x["prompt"] += r.get("prompt") or 0
        x["completion"] += r.get("completion") or 0
        if r.get("spend") is not None:
            x["spend"] += r["spend"]
            x["known"] += 1
        if r.get("t1") and r.get("t2") and r["t2"] > r["t1"] and (r.get("completion") or 0) > 0:
            x["dec"] += r["t2"] - r["t1"]
            x["comp"] += r["completion"]
        if r.get("t1") and r.get("t0") and r["t1"] >= r["t0"]:
            x["ttf"].append(r["t1"] - r["t0"])

    # 只比账单里有的 provider；账单为空（还没跑过 api 模式）时退回"全部本地 provider"
    cmp_prov = [provider] if provider else (sorted(bil_prov) or sorted(loc))
    cmp_prov = [p for p in cmp_prov if p in loc or p in bil_prov]

    def merge(days_map, provs):
        out = {}
        for pv in provs:
            for k, x in (days_map.get(pv) or {}).items():
                y = out.setdefault(k, _rzero())
                for f in ("n", "prompt", "completion", "spend", "known", "dec", "comp"):
                    y[f] += x[f]
                y["ttf"].extend(x["ttf"])
        return out

    loc_cmp = merge(loc, cmp_prov)
    unb_days = merge({p: v for p, v in loc.items() if p not in cmp_prov}, list(loc))
    unb = _rzero()
    for x in unb_days.values():
        for f in ("n", "prompt", "completion", "spend", "known", "dec", "comp"):
            unb[f] += x[f]
        unb["ttf"].extend(x["ttf"])

    def pack(x):
        return {
            "req": x["n"],
            "prompt": x["prompt"],
            "completion": x["completion"],
            "tokens": x["prompt"] + x["completion"],
            "spend": round(x["spend"], 3) if x["known"] else None,
            "tps": round(x["comp"] / x["dec"], 2) if x["dec"] > 0 else None,
            "ttft_ms": round(sum(x["ttf"]) / len(x["ttf"]) * 1000) if x["ttf"] else None,
        }

    def pct(a, b):
        return round((a - b) / b * 100, 2) if b else None

    out = []
    for k in sorted(set(loc_cmp) | set(bil)):
        L = pack(loc_cmp.get(k) or _rzero())
        B = pack(bil.get(k) or _rzero())
        out.append({
            "date": k, "local": L, "bill": B,
            "diff": {
                "req": L["req"] - B["req"],
                "prompt_pct": pct(L["prompt"], B["prompt"]),
                "spend_pct": pct(L["spend"] or 0, B["spend"] or 0),
                "tps_pct": pct(L["tps"] or 0, B["tps"] or 0),
            },
        })

    TL, TB = _rzero(), _rzero()
    for k in set(loc_cmp) | set(bil):
        for tgt, src_map in ((TL, loc_cmp), (TB, bil)):
            x = src_map.get(k)
            if not x:
                continue
            for f in ("n", "prompt", "completion", "spend", "known", "dec", "comp"):
                tgt[f] += x[f]
            tgt["ttf"].extend(x["ttf"])
    L, B = pack(TL), pack(TB)
    return {
        "ok": True,
        "days": days,
        "provider": provider,
        "compared_providers": cmp_prov,
        "billing_providers": sorted(bil_prov),
        "since_local": local(since).strftime("%Y-%m-%d %H:%M"),
        "daily": out,
        "total": {
            "local": L, "bill": B,
            "diff": {
                "req": L["req"] - B["req"],
                "prompt_pct": pct(L["prompt"], B["prompt"]),
                "spend_pct": pct(L["spend"] or 0, B["spend"] or 0),
                "tps_pct": pct(L["tps"] or 0, B["tps"] or 0),
                "ttft_pct": pct(L["ttft_ms"] or 0, B["ttft_ms"] or 0),
            },
        },
        # 本机日志里有、但网关账单里没有的 provider（没走这个网关，不该算作"对不上"）
        "unbilled": {"providers": sorted(p for p in loc if p not in cmp_prov), **pack(unb)},
        "notes": KNOWN_GAPS,
        "local_stats": _stats(events, root=None),
        "prices": price_table_path(),
    }


def price_table_path():
    from sources.dsh_log import prices_path
    return prices_path()


# --------------------------------------------------------------------------- #
# 看板
# --------------------------------------------------------------------------- #
PAGE_PATH = os.path.join(HERE, "web", "index.html")
SELFTEST_PATH = os.path.join(HERE, "web", "_selftest.html")
_PAGE_CACHE = {"mtime": None, "html": ""}

# --------------------------------------------------------------------------- #
# 悬浮小窗的配色（看板选，小窗读）——浅色为主，最后一个给夜间用
# --------------------------------------------------------------------------- #
WIDGET_CFG_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "ustc-token-monitor", "widget.json")

WIDGET_THEMES = {
    "paper": {"name": "白", "panel": "#ffffff", "border": "#d3dae5", "txt": "#16202c",
              "dim": "#55606f", "faint": "#7b8595", "track": "#e7ecf3",
              "green": "#12915a", "amber": "#b06a00", "blue": "#1f6feb", "red": "#cf2233"},
    "cream": {"name": "米白", "panel": "#fbf6ec", "border": "#e3d9c6", "txt": "#2a2418",
              "dim": "#6b6250", "faint": "#9a917c", "track": "#ece2cf",
              "green": "#0f8a54", "amber": "#a86400", "blue": "#1c64c8", "red": "#c22626"},
    "mint": {"name": "薄荷", "panel": "#eefaf3", "border": "#cfe7db", "txt": "#12281f",
             "dim": "#4f6b5f", "faint": "#84a094", "track": "#d8ece2",
             "green": "#0d8f58", "amber": "#a86500", "blue": "#1868c9", "red": "#c22a3a"},
    "sky": {"name": "天蓝", "panel": "#eef5fd", "border": "#d0dfef", "txt": "#132437",
            "dim": "#506275", "faint": "#8598ab", "track": "#dbe8f6",
            "green": "#0f8a5a", "amber": "#a86500", "blue": "#1a63c8", "red": "#c22636"},
    "lilac": {"name": "淡紫", "panel": "#f4f1fd", "border": "#ddd5f0", "txt": "#221c33",
              "dim": "#5d5470", "faint": "#918aa3", "track": "#e7e1f6",
              "green": "#0f8a58", "amber": "#a86400", "blue": "#5a49c8", "red": "#c22a4a"},
    "gray": {"name": "浅灰", "panel": "#f3f5f8", "border": "#dde2e9", "txt": "#1a2029",
             "dim": "#57606d", "faint": "#8b95a2", "track": "#e3e8ef",
             "green": "#0f8a56", "amber": "#a46200", "blue": "#2a63c4", "red": "#c22636"},
    "dark": {"name": "深色", "panel": "#161c25", "border": "#2a3542", "txt": "#e9eff7",
             "dim": "#8695a8", "faint": "#5f6c7d", "track": "#232c39",
             "green": "#3ddc97", "amber": "#ffb454", "blue": "#5aa9ff", "red": "#ff6b6b"},
}
DEFAULT_WIDGET_THEME = "paper"


def _theme_colors(key):
    t = dict(WIDGET_THEMES.get(key) or WIDGET_THEMES[DEFAULT_WIDGET_THEME])
    t.pop("name", None)
    return t


def widget_cfg_read():
    try:
        with open(WIDGET_CFG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def widget_cfg_write(d):
    try:
        os.makedirs(os.path.dirname(WIDGET_CFG_PATH), exist_ok=True)
        tmp = WIDGET_CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, WIDGET_CFG_PATH)   # 原子替换，小窗不会读到半个文件
        return True
    except Exception as e:
        print(f"写 widget 配置失败: {e}", file=sys.stderr)
        return False


WIDGET_PATH = os.path.join(HERE, "widget.py")


def widget_open():
    """唤起悬浮小窗：已经在跑就把它拎到最前，没跑就拉一个新进程。"""
    if sys.platform == "win32":
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, "USTC Token")
            if hwnd:
                u.ShowWindow(hwnd, 5)              # SW_SHOW
                u.SetForegroundWindow(hwnd)
                return {"ok": True, "action": "raised"}
        except Exception:
            pass
    if not os.path.exists(WIDGET_PATH):
        return {"ok": False, "error": f"找不到 {WIDGET_PATH}"}
    now = time.time()
    with LOCK:
        last = STATE.get("widget_spawn_at") or 0
        if now - last < 3:                          # 连点防抖：3 秒内只拉一次
            return {"ok": True, "action": "starting"}
        STATE["widget_spawn_at"] = now
    exe = sys.executable
    if sys.platform == "win32":
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            exe = cand
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([exe, WIDGET_PATH], cwd=HERE, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "action": "started"}


# --------------------------------------------------------------------------- #
# 打开看板：同一份看板只留一个窗口
# --------------------------------------------------------------------------- #
DASHBOARD_TITLE = "USTC Token 监测"
BROWSER_IMAGES = {"msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe",
                  "firefox.exe", "arc.exe"}


def _enum_windows():
    """[(hwnd, 标题, pid, 窗口类, 是否可见)]；只调 user32，出错就返回空表。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    out = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        title = ""
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
        out.append((hwnd, title, pid.value, cls.value, bool(u.IsWindowVisible(hwnd))))
        return True

    u.EnumWindows(proc(cb), 0)
    return out


def _proc_image(pid):
    """进程 exe 名（小写）；拿不到返回空串。"""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    except Exception:
        pass
    finally:
        k.CloseHandle(h)
    return ""


def _force_foreground(hwnd):
    """把窗口恢复/最大化并真正抢到前台。

    后台进程直接 SetForegroundWindow 常被系统的"防抢焦点"挡下来（返回 0、窗口不动），
    所以这里先把自己的输入线程挂到当前前台窗口上，失败再模拟一次 ALT 解锁。
    """
    import ctypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)                        # SW_RESTORE：最小化也救回来
    if not u.IsZoomed(hwnd):
        u.ShowWindow(hwnd, 3)                        # SW_MAXIMIZE：别以小窗口出现
    fg = u.GetForegroundWindow()
    tid_fg = u.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = k.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        try:
            attached = bool(u.AttachThreadInput(tid_me, tid_fg, True))
        except Exception:
            attached = False
    ok = bool(u.SetForegroundWindow(hwnd))
    if not ok:
        VK_ALT, KEYUP = 0x12, 0x0002
        u.keybd_event(VK_ALT, 0, 0, 0)
        u.keybd_event(VK_ALT, 0, KEYUP, 0)
        ok = bool(u.SetForegroundWindow(hwnd))
    u.BringWindowToTop(hwnd)
    try:
        u.SwitchToThisWindow(hwnd, True)             # 最后一招，比 SetForegroundWindow 更硬
    except Exception:
        pass
    if attached:
        try:
            u.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass
    return ok


_raise_window = _force_foreground                     # 兼容旧调用点


def _find_dash_window():
    """(hwnd, 标题)：正在显示看板的窗口；没有返回 None。"""
    try:
        for hwnd, title, _pid, _cls, vis in _enum_windows():
            if vis and DASHBOARD_TITLE in title:
                return hwnd, title
    except Exception as e:
        STATE["dash_err"] = str(e)
    return None


def _watch_and_raise(seconds=8.0):
    """浏览器冷启动要几秒才有窗口，后台等它出现再最大化抬前台（不阻塞请求）。"""
    def run():
        end = time.time() + seconds
        while time.time() < end:
            hit = _find_dash_window()
            if hit:
                _force_foreground(hit[0])
                return
            time.sleep(0.2)
    threading.Thread(target=run, daemon=True).start()


def dashboard_open(port=8790):
    """有看板窗口就最大化抬到最前；没有就先把标签页开出来，再等窗口出现抬起来。"""
    url = f"http://127.0.0.1:{port}/"
    if sys.platform == "win32":
        hit = _find_dash_window()
        if hit:
            _force_foreground(hit[0])
            return {"ok": True, "action": "focused", "window": hit[1]}
    if sys.platform == "win32" and time.time() - (STATE.get("dash_open_at") or 0) < 2.5:
        # 刚开过（连点/双击）：别再堆一个标签页，把现有的浏览器窗口抬起来就行
        for hwnd, title, pid, cls, vis in _enum_windows():
            if vis and title and cls == "Chrome_WidgetWin_1" and _proc_image(pid) in BROWSER_IMAGES:
                _force_foreground(hwnd)
                return {"ok": True, "action": "browser", "window": title}
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception as e:
        STATE["dash_err"] = str(e)
    STATE["dash_open_at"] = time.time()
    if not opened:
        try:
            os.startfile(url)                        # 浏览器关联坏了时的兜底
            opened = True
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if sys.platform == "win32":
        _watch_and_raise()
    return {"ok": True, "action": "opened"}


def widget_payload():
    cfg = widget_cfg_read()
    key = cfg.get("theme")
    if key not in WIDGET_THEMES:
        key = DEFAULT_WIDGET_THEME
    return {
        "theme": key,
        "colors": _theme_colors(key),
        "themes": WIDGET_THEMES,
        "path": WIDGET_CFG_PATH,
    }


def widget_set_theme(key):
    if key not in WIDGET_THEMES:
        return False
    cfg = widget_cfg_read()
    cfg["theme"] = key
    cfg["colors"] = _theme_colors(key)
    return widget_cfg_write(cfg)



def selftest_html():
    """自检页由看板直接提供：部署完打开 /_selftest.html 就能跑用例，不用另起静态服务。"""
    try:
        with open(SELFTEST_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "<!doctype html><meta charset=utf-8><h1>缺少 web/_selftest.html</h1>"


def page_html():
    """每次请求按 mtime 校验，改完 index.html 刷新浏览器就生效，不用重启进程。"""
    try:
        mtime = os.path.getmtime(PAGE_PATH)
    except OSError:
        return "<!doctype html><meta charset=utf-8><h1>缺少 web/index.html</h1>"
    if _PAGE_CACHE["mtime"] != mtime:
        with open(PAGE_PATH, encoding="utf-8") as f:
            _PAGE_CACHE["html"] = f.read()
        _PAGE_CACHE["mtime"] = mtime
    return _PAGE_CACHE["html"]



class _Server(ThreadingHTTPServer):
    """跳过 HTTPServer.server_bind 里的 getfqdn() 反查，避免 DNS 慢时启动卡死。"""
    daemon_threads = True

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 允许 DSH 页面（另一个本地端口）直接读取
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, "", "text/plain")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/summary":
            STATE.setdefault("hits", collections.deque(maxlen=200)).append(
                (time.time(), self.headers.get("User-Agent", "")[:60]))
            source = (query.get("source") or [None])[0] or None
            provider = (query.get("provider") or [None])[0] or None
            usage = (query.get("usage") or [None])[0] or None
            bucket_s = bucket_norm((query.get("bucket") or [BUCKET_DEFAULT])[0])
            ua = self.headers.get("User-Agent", "")
            if "Mozilla" in ua:
                STATE["dash_seen"] = time.time()          # 有个浏览器标签开着看板
            try:
                body = json.dumps(summarize(source=source, bucket_s=bucket_s,
                                            provider=provider, usage=usage),
                                  ensure_ascii=False)
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")
        elif parsed.path == "/api/reconcile":
            try:
                days = int((query.get("days") or ["30"])[0])
            except (TypeError, ValueError):
                days = 30
            provider = (query.get("provider") or [None])[0] or None
            try:
                body = json.dumps(reconcile(days=days, provider=provider), ensure_ascii=False)
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif parsed.path == "/api/widget":
            self._send(200, json.dumps(widget_payload(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif parsed.path == "/api/hits":
            hits = list(STATE.get("hits") or ())
            self._send(200, json.dumps({
                "count": len(hits),
                "recent": [{"at": datetime.fromtimestamp(t).strftime("%H:%M:%S"),
                            "ua": ua} for t, ua in hits[-20:]],
            }, ensure_ascii=False), "application/json; charset=utf-8")
        elif parsed.path in ("/", "/index.html"):
            self._send(200, page_html(), "text/html; charset=utf-8")
        elif parsed.path in ("/_selftest.html", "/_selftest"):
            self._send(200, selftest_html(), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/dashboard/open":
            self._send(200, json.dumps(dashboard_open(self.server.server_port), ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if parsed.path == "/api/widget/open":
            self._send(200, json.dumps(widget_open(), ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if parsed.path != "/api/widget":
            self._send(404, "not found", "text/plain")
            return
        theme = (query.get("theme") or [None])[0]
        if not theme:
            try:                                  # 也接受 JSON body {"theme": "mint"}
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    theme = (json.loads(self.rfile.read(n).decode("utf-8")) or {}).get("theme")
            except Exception:
                theme = None
        if theme not in WIDGET_THEMES:
            self._send(400, json.dumps({"error": f"unknown theme: {theme!r}",
                                        "themes": list(WIDGET_THEMES)},
                                       ensure_ascii=False), "application/json; charset=utf-8")
            return
        ok = widget_set_theme(theme)
        self._send(200 if ok else 500,
                   json.dumps(dict(widget_payload(), saved=ok), ensure_ascii=False),
                   "application/json; charset=utf-8")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="采集一轮并打印摘要")
    ap.add_argument("--source", choices=SOURCE_CHOICES, default=None,
                    help="数据源：local=本机 DSH 会话日志（默认，零密钥）| api=网关账单 | both")
    ap.add_argument("--backfill", action="store_true", help="回填全部历史请求")
    ap.add_argument("--since", default=DEFAULT_BACKFILL_START,
                    help=f"配合 --backfill：起始日期 YYYY-MM-DD（默认 {DEFAULT_BACKFILL_START}）")
    ap.add_argument("--serve", action="store_true", help="常驻采集并启动看板")
    ap.add_argument("--port", type=int, default=8790,
                    help="看板端口（默认 8790；插件宿主默认也连 8790，换端口要同步设 USTC_MONITOR_PORT）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认仅本机 127.0.0.1）")
    args = ap.parse_args()

    if args.backfill:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"--since 需要 YYYY-MM-DD，收到 {args.since!r}", file=sys.stderr)
            return 2

    global SOURCE
    if args.source:
        SOURCE = args.source
    need_api = SOURCE in ("api", "both")

    init_db()                                    # 先迁移表结构，两种源都能写
    print(f"数据源: {SOURCE}"
          + ("（本机 DSH 会话日志，零密钥、零网络）" if SOURCE == "local"
             else "（网关账单）" if SOURCE == "api" else "（账单优先，日志用于对账）"))
    if SOURCE in ("local", "both"):
        try:
            from sources.dsh_log import stats as _dsh_stats, zstd_hint
            _st = _dsh_stats()
            print(f"  日志: {_st['files']} 个会话文件 · 去重后 {_st['events']} 条计费事件"
                  f" · zstd={_st['zstd']}")
            if _st["events"] == 0 and not _st["zstd"]:
                print(zstd_hint(), file=sys.stderr)
        except Exception as e:                   # 诊断失败不阻断，真问题在 poll_local 里报
            print(f"  日志诊断失败: {e}", file=sys.stderr)
    if need_api and not ensure_key():
        print("未找到 API Key：请设置 USTC_API_KEY 环境变量", file=sys.stderr)
        print("  PowerShell:  $env:USTC_API_KEY = 'sk-...'   （也可写进 ~/.dsh/.credentials.yaml）",
              file=sys.stderr)
        print("  不想用 key：直接跑 --source local（本机日志，零密钥）。", file=sys.stderr)
        return 2
    if need_api:
        print(f"Key: {MASK(KEY)} (来源: {KEY_SRC})")
    print(f"DB : {DB_PATH}")
    if args.backfill:
        if not need_api:
            print("--backfill 需要 --source api（历史账单只在网关侧）", file=sys.stderr)
            return 2
        try:
            backfill(args.since)
        except Exception as e:
            print(f"回填失败: {_err_text(e)}", file=sys.stderr)
            return 1
        return 0
    warm = 0
    if SOURCE in ("local", "both"):
        warm = poll_local()
    if SOURCE in ("api", "both"):
        warm = poll_once(full=False, force_full=False)   # 增量补最新，看板立刻可用

    if args.once:
        changed = 0
        if SOURCE in ("api", "both"):
            changed = poll_once(full=True)
        if SOURCE in ("local", "both"):
            changed = poll_local()
        if warm < 0 or changed < 0:
            print(f"\n采集失败: {STATE['last_err']}", file=sys.stderr)
            return 1
        s = summarize()
        money = "—" if s["day"]["spend"] is None else f"{s['day']['spend']:.4f}"
        print(f"\n时间 {s['now_local']}   口径 {s['usage_source']}")
        print(f"1h  : {fmt_int(s['h1']['total'])} tok | 入 {fmt_int(s['h1']['prompt'])} "
              f"出 {fmt_int(s['h1']['completion'])} | {s['h1']['req']} 次 | "
              f"{s['h1']['tps'] and round(s['h1']['tps'],1)} tok/s | TTFT {s['ttft_ms']} ms")
        print(f"5m  : {fmt_int(s['m5']['total'])} tok | {s['m5']['tps'] and round(s['m5']['tps'],1)} tok/s")
        print(f"全天: {fmt_int(s['day']['total'])} tok | {s['day']['req']} 次 | 费用 {money}"
              f" | 金额覆盖 {s['day'].get('spend_cov')}")
        print(f"累计: {fmt_int(s['alltime']['tokens'])} tok | {s['alltime']['req']} 次 | "
              f"{s['alltime']['days']} 天 | 费用 "
              + ("—" if s['alltime']['spend'] is None else f"{s['alltime']['spend']:.2f}"))
        for b in s["budgets"]:
            print(f"限额 {b['window']:>3}: {b['used']}/{b['max']} ({b['pct']}%) "
                  f"重置 {b['reset_in_s']}s 后")
        if s["usage_source"] == "estimated":
            print("金额按 prices.json 单价估算，不是账单真值（≈）")
        return 0

    if not args.serve:
        ap.print_help()
        return 0

    # 单实例保护：Windows 下 SO_REUSEADDR 允许两个进程绑同一端口，
    # 会出现旧实例抢答、看板与插件读到旧代码的诡异现象。
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind((args.host, args.port))
    except OSError:
        print(f"端口 {args.port} 已被占用，监测器可能已在运行"
              f"（换个端口：python monitor.py --serve --port 9000）", file=sys.stderr)
        return 2
    finally:
        guard.close()

    stop = threading.Event()

    def loop():
        while not stop.is_set():
            t0 = time.time()
            if SOURCE in ("local", "both"):
                poll_local()
            if SOURCE in ("api", "both"):
                poll_once()
            s = summarize()
            spend = "—" if s['day']['spend'] is None else f"{s['day']['spend']:.3f}"
            print(f"[{local(time.time()).strftime('%H:%M:%S')}] "
                  f"req={s['h1']['req']:>2} 1h={fmt_int(s['h1']['total']):>7} "
                  f"tps={s['h1']['tps'] and round(s['h1']['tps'],1)} "
                  f"day={fmt_int(s['day']['total']):>7} spend={spend}",
                  flush=True)
            stop.wait(max(1.0, POLL_SECONDS - (time.time() - t0)))

    threading.Thread(target=loop, daemon=True).start()
    srv = _Server((args.host, args.port), Handler)
    print(f"看板: http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        print("\n已停止")


if __name__ == "__main__":
    sys.exit(main())
