#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USTC Token 悬浮小窗
-------------------
桌面上常驻一块无边框小卡片，实时显示 tok/s、5 分钟用量、1 小时用量、限额进度。

- 无边框 + 始终置顶，拖到屏幕任意角落；位置、迷你模式、透明度记在配置里
- 双击打开完整看板；右键菜单可切迷你/置顶/透明度/退出
- 只读本地监测器的 /api/summary，不额外请求平台接口（平台调用仍由 monitor.py 负责）
- 仅标准库：tkinter（Python 自带）

用法:
  pythonw widget.py                 # 无控制台启动（推荐）
  python widget.py                  # 带控制台，便于看报错
  python widget.py --selftest       # 离线布局自检，打印 PASS/FAIL
  python widget.py --dump           # 用真实数据渲染一次并打印所有文本，然后退出
  python widget.py --api http://127.0.0.1:9000/api/summary
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

REFRESH_MS = 5000
API_URL = "http://127.0.0.1:8790/api/summary"
DASHBOARD = "http://127.0.0.1:8790/"

# 显示缩放：96 DPI = 1.0。高 DPI 下整块等比放大，避免文字撑破固定尺寸。
SCALE = 1.0


def S(v):
    return int(round(v * SCALE))


C = {
    "bg": "#11151c",
    "panel": "#161c25",
    "txt": "#e9eff7",
    "dim": "#8695a8",
    "faint": "#5f6c7d",
    "green": "#3ddc97",
    "amber": "#ffb454",
    "blue": "#5aa9ff",
    "red": "#ff6b6b",
    "track": "#232c39",
    "accentbg": "#1d2531",
}


def cfg_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ustc-token-monitor", "widget.json")


def load_cfg():
    try:
        with open(cfg_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cfg(d):
    try:
        p = cfg_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


# ------------------------------- 格式化 ------------------------------- #
def fmt_tok(n):
    if n is None:
        return "—"
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(round(n)))


def fmt_tps(v):
    if v is None:
        return "—"
    v = float(v)
    if v >= 100:
        return f"{v:.0f}"
    return f"{v:.1f}"


def fmt_ttft(ms):
    if ms is None:
        return "首包 —"
    return f"首包 {ms / 1000:.1f}s"


def fmt_idle(sec):
    if sec is None:
        return ""
    if sec < 90:
        return ""
    m = int(sec // 60)
    return f"{m}m 前" if m < 60 else f"{m // 60}h{m % 60}m 前"


def fmt_ago(sec):
    if sec is None:
        return "未采集"
    if sec < 90:
        return f"{sec}s 前"
    m = int(sec // 60)
    return f"{m}m 前" if m < 60 else f"{m // 60}h 前"


# ------------------------------- 取数 ------------------------------- #
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url, timeout=3.0):
    with _OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def view(d):
    """把 /api/summary 压成小窗要用的几个字段，None 安全。"""
    d = d or {}
    poll = d.get("poll") or {}
    m5a = d.get("m5a") or {}
    h1 = d.get("h1") or {}
    day = d.get("day") or {}
    budgets = d.get("budgets") or []
    b3 = next((b for b in budgets if b.get("window") == "3h"), None)
    b12 = next((b for b in budgets if b.get("window") == "12h"), None)
    spend = day.get("spend")
    dayline = f"全天 {fmt_tok(day.get('total'))}"
    if spend:
        dayline += f" ¥{spend:.2f}"
    return {
        "tps": m5a.get("tps"),
        "idle": fmt_idle(m5a.get("idle_s")),
        "m5": fmt_tok(m5a.get("tokens")),
        "h1": fmt_tok(h1.get("total")),
        "h1req": h1.get("req") or 0,
        "ttft": fmt_ttft(d.get("ttft_ms")),
        "day": fmt_tok(day.get("total")),
        "dayline": dayline,
        "err": poll.get("last_err"),
        "ago": fmt_ago(poll.get("last_ok_ago_s")),
        "b3": b3,
        "b12": b12,
        "clock": (d.get("now_local") or "")[11:19] or "--:--:--",
    }


def budget_text(b):
    if not b or b.get("pct") is None:
        return "—", 0.0
    return f"{b.get('used', 0):.1f}/{b.get('max', 0):.0f}", float(b.get("pct") or 0)


# ------------------------------- 窗口 ------------------------------- #
class Widget:
    NORMAL = (286, 148)
    MINI = (188, 58)

    def __init__(self, root, cfg):
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.cfg = cfg
        self.mini = bool(cfg.get("mini"))
        self.topmost = bool(cfg.get("topmost", True))
        self.alpha = float(cfg.get("alpha", 0.94))
        self.data = None
        self.err = None
        self._busy = False
        self._items = {}

        self.W, self.H = self.size()

        root.overrideredirect(True)
        root.configure(bg=C["bg"])
        root.title("USTC Token")
        self._attr("-topmost", self.topmost)
        self._attr("-alpha", self.alpha)
        self._attr("-toolwindow", True)       # 不占任务栏
        self._transparent_bg()

        x, y = cfg.get("x"), cfg.get("y")
        root.geometry(f"{self.W}x{self.H}+{int(x) if isinstance(x, int) else 60}"
                      f"+{int(y) if isinstance(y, int) else 60}")

        self.cv = tk.Canvas(root, width=self.W, height=self.H, bg=C["bg"], highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Button-1>", self._drag_start)
        self.cv.bind("<B1-Motion>", self._drag_move)
        self.cv.bind("<ButtonRelease-1>", self._drag_end)
        self.cv.bind("<Double-Button-1>", lambda e: webbrowser.open(DASHBOARD))
        self.cv.bind("<Button-3>", self._menu)
        self.cv.bind("<Control-MouseWheel>", self._wheel_alpha)

        self.menu = tk.Menu(root, tearoff=0, bg=C["panel"], fg=C["txt"],
                            activebackground=C["accentbg"], activeforeground=C["txt"], bd=0)
        self.menu.add_command(label="打开看板", command=lambda: webbrowser.open(DASHBOARD))
        self.menu.add_separator()
        self.menu.add_command(label="迷你模式", command=self.toggle_mini)
        self.menu.add_command(label="始终置顶", command=self.toggle_topmost)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.close)

        self.render()          # 先画骨架（占位），避免首帧空白
        self.tick()

    # ---- 窗口属性（个别平台不支持就忽略） ---- #
    def _attr(self, name, value):
        try:
            self.root.attributes(name, value)
            return True
        except Exception:
            return False

    def _transparent_bg(self):
        """用 transparentcolor 让圆角外的像素透出去，失败就退回直角。"""
        self.rounded = False
        if self._attr("-transparentcolor", "#010203"):
            self.cv_bg = "#010203"
            self.rounded = True
        else:
            self.cv_bg = C["bg"]

    # ---- 交互 ---- #
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        if not hasattr(self, "_dx"):
            return
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, _e):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_cfg(self.cfg)

    def _menu(self, e):
        self.menu.entryconfigure(2, label="退出迷你模式" if self.mini else "迷你模式")
        self.menu.entryconfigure(3, label="取消置顶" if self.topmost else "始终置顶")
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _wheel_alpha(self, e):
        self.alpha = max(0.35, min(1.0, self.alpha + (0.05 if e.delta > 0 else -0.05)))
        self._attr("-alpha", round(self.alpha, 2))
        self.cfg["alpha"] = round(self.alpha, 2)
        save_cfg(self.cfg)

    def toggle_mini(self):
        self.mini = not self.mini
        self.cfg["mini"] = self.mini
        save_cfg(self.cfg)
        self.resize()

    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.cfg["topmost"] = self.topmost
        save_cfg(self.cfg)
        self._attr("-topmost", self.topmost)

    def size(self):
        w, h = self.MINI if self.mini else self.NORMAL
        return S(w), S(h)

    def resize(self):
        self.W, self.H = self.size()
        self.root.geometry(f"{self.W}x{self.H}+{self.root.winfo_x()}+{self.root.winfo_y()}")
        self.cv.configure(width=self.W, height=self.H)
        self._items.clear()
        self.cv.delete("all")
        self.render()

    def close(self):
        save_cfg(self.cfg)
        self.root.destroy()

    # ---- 轮询（不阻塞 UI） ---- #
    def tick(self):
        if not self._busy:
            self._busy = True
            threading.Thread(target=self._work, daemon=True).start()
        self.root.after(REFRESH_MS, self.tick)

    def _work(self):
        try:
            self.data, self.err = view(fetch(API_URL)), None
        except Exception as e:
            self.err = f"监测器未运行（{API_URL}）" if isinstance(e, urllib.error.URLError) else str(e)
        self._busy = False
        try:
            self.root.after(0, self.render)
        except Exception:
            pass

    # ---- 绘制 ---- #
    def _rr(self, x1, y1, x2, y2, r, **kw):
        pts = (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def _text(self, name, x, y, txt, font, fill, anchor="w"):
        it = self._items.get(name)
        if it is None:
            it = self.cv.create_text(x, y, text=txt, font=font, fill=fill, anchor=anchor)
            self._items[name] = it
        else:
            self.cv.coords(it, x, y)
            self.cv.itemconfigure(it, text=txt, font=font, fill=fill, anchor=anchor)
        return it

    def _rect(self, name, x1, y1, x2, y2, fill, **kw):
        it = self._items.get(name)
        if it is None:
            it = self.cv.create_rectangle(x1, y1, x2, y2, fill=fill, width=0, **kw)
            self._items[name] = it
        else:
            self.cv.coords(it, x1, y1, x2, y2)
            self.cv.itemconfigure(it, fill=fill)
        return it

    def _bar(self, name, x1, y, w, pct, color):
        h = S(6)
        self._rect(name + "t", x1, y, x1 + w, y + h, C["track"])
        ww = max(0.0, min(100.0, pct)) / 100.0 * w
        if ww >= 1:
            self._rect(name + "f", x1, y, x1 + ww, y + h, color)
        else:
            self._rect(name + "f", x1, y, x1 + 1, y + h, C["bg"])

    def render(self, data=None):
        if data is None:
            data = self.data
        v = data or {}
        tk = self.tk
        W, H = self.W, self.H
        f_big = ("Cascadia Mono", -S(23), "bold")
        f_unit = ("Microsoft YaHei UI", -S(11))
        f_num = ("Microsoft YaHei UI", -S(12))
        f_lbl = ("Microsoft YaHei UI", -S(11))
        f_lbl2 = ("Microsoft YaHei UI", -S(12))
        pad = S(13)

        if self.rounded:
            self.cv.configure(bg=self.cv_bg)
            r = S(11)
            self._rr(0, 0, W, H, r, fill=C["panel"], outline=C["track"], width=1)
        else:
            self.cv.configure(bg=C["panel"])

        # 第一行：速度（左）· 5m 用量（右）
        err = v.get("err") or self.err
        dot = C["red"] if err else C["green"]
        self._rect("dot", pad, S(21), pad + S(3), S(30), dot)
        tps_txt = fmt_tps(v.get("tps")) if v else "—"
        tw = self._text("tps", pad + S(10), S(27), tps_txt, f_big, C["txt"], anchor="w")
        ux = self.cv.bbox(tw)[2] + 4
        self._text("unit", ux, S(30), "tok/s", f_unit, C["dim"])
        idle = v.get("idle") or ""
        self._text("m5", W - pad, S(27), f"5m {v.get('m5', '—')}", f_num, C["dim"], anchor="e")
        if idle:
            self._text("idle", W - pad, S(9), idle, f_lbl, C["faint"], anchor="e")

        if self.mini:
            return

        # 第二行：1h 用量 · 首包
        self._text("h1", pad, S(50), f"1h {v.get('h1', '—')}  ·  {v.get('h1req', 0)} 次", f_lbl2, C["dim"])
        self._text("ttft", W - pad, S(50), v.get("ttft", "首包 —"), f_lbl2, C["dim"], anchor="e")

        # 限额两条
        barw = W - pad - S(22) - S(66)
        for idx, key in ((0, "b3"), (1, "b12")):
            b = v.get(key)
            txt, pct = budget_text(b)
            y = S(69) + idx * S(22)
            self._text(key + "l", pad, y + S(3), "3h" if idx == 0 else "12h", f_lbl, C["faint"])
            color = C["red"] if pct > 85 else C["amber"] if pct > 60 else C["blue"]
            self._bar(key, pad + S(22), y, barw, pct, color)
            self._text(key + "v", W - pad, y + S(3), txt, f_lbl, C["dim"], anchor="e")

        # 底线：时间 / 全天 / 状态
        if err:
            msg = str(err)
            msg = msg if len(msg) <= 26 else msg[:25] + "…"
            self._text("foot", pad, H - S(15), "⚠ " + msg, f_lbl, C["red"])
        else:
            self._text("foot", pad, H - S(15), f"{v.get('clock', '--:--:--')} · {v.get('ago', '')}",
                       f_lbl, C["faint"])
            self._text("day", W - pad, H - S(15), v.get("dayline", "全天 —"), f_lbl, C["faint"], anchor="e")


# ------------------------------- 自检 ------------------------------- #
def selftest():
    import tkinter as tk

    ok = []
    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    _init_scale(root)
    wdg = Widget(root, {"x": 40, "y": 40})

    fix_full = view({
        "poll": {"last_ok_ago_s": 3, "last_err": None},
        "h1": {"total": 12_800_000, "req": 166},
        "m5a": {"tps": 58.3, "tokens": 1_234_567, "idle_s": 0},
        "day": {"total": 89_300_000},
        "ttft_ms": 2105,
        "budgets": [{"window": "3h", "used": 12.4, "max": 30, "pct": 41.3},
                    {"window": "12h", "used": 41.2, "max": 70, "pct": 58.9}],
        "now_local": "2026-11-26 13:23:05",
    })

    def texts():
        out = {}
        for name, it in wdg._items.items():
            try:
                out[name] = wdg.cv.itemcget(it, "text")
            except tk.TclError:
                out[name] = ""
        return out

    def inside():
        bad = []
        for name, it in wdg._items.items():
            b = wdg.cv.bbox(it)
            if not b:
                continue
            x1, y1, x2, y2 = b
            if x1 < -1 or y1 < -1 or x2 > wdg.W + 1 or y2 > wdg.H + 1:
                bad.append(f"{name}{b}")
        return bad

    wdg.render(fix_full)
    t = texts()
    ok.append(("正常数据 tok/s", t.get("tps") == "58.3", t.get("tps")))
    ok.append(("正常数据 5m", t.get("m5") == "5m 1.2M", t.get("m5")))
    ok.append(("正常数据 1h", "12.8M" in (t.get("h1") or ""), t.get("h1")))
    ok.append(("首包", t.get("ttft") == "首包 2.1s", t.get("ttft")))
    ok.append(("限额 3h", t.get("b3v") == "12.4/30", t.get("b3v")))
    ok.append(("限额 12h", t.get("b12v") == "41.2/70", t.get("b12v")))
    ok.append(("全天", t.get("day") == "全天 89.3M", t.get("day")))
    ok.append(("时间", t.get("foot", "").startswith("13:23:05"), t.get("foot")))
    ok.append(("正常版不出界", not inside(), ";".join(inside())))
    ok.append(("置顶生效", bool(root.attributes("-topmost")), root.attributes("-topmost")))

    # 空数据 / 缺字段
    wdg.render(view({}))
    t = texts()
    ok.append(("空数据不崩", t.get("tps") == "—" and t.get("b3v") == "—", t.get("tps")))
    ok.append(("空数据不出界", not inside(), ";".join(inside())))

    # 采集异常
    wdg.render(view({"poll": {"last_ok_ago_s": 600, "last_err": "平台返回 503（平台暂时不可用）"}}))
    t = texts()
    ok.append(("错误提示", "503" in (t.get("foot") or ""), t.get("foot")))

    # 迷你模式
    wdg.mini = True
    wdg.resize()
    wdg.render(fix_full)
    t = texts()
    ok.append(("迷你尺寸", (wdg.W, wdg.H) == Widget.MINI, f"{wdg.W}x{wdg.H}"))
    ok.append(("迷你仍显示速度", t.get("tps") == "58.3", t.get("tps")))
    ok.append(("迷你不出界", not inside(), ";".join(inside())))

    root.destroy()

    fails = [n for n, good, _ in ok if not good]
    for name, good, val in ok:
        print(f"{'ok  ' if good else 'FAIL'} {name}" + ("" if good else f"  实际={val!r}"))
    print(f"\n共 {len(ok)} 项，失败 {len(fails)} 项" + (("：" + " / ".join(fails)) if fails else ""))
    return 0 if not fails else 1


def dump():
    import tkinter as tk

    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    _init_scale(root)
    wdg = Widget(root, {"x": 40, "y": 40})
    try:
        data = view(fetch(API_URL))
    except Exception as e:
        print(f"取数失败：{e}")
        data = view({})
    wdg.render(data)
    print(f"窗口 {wdg.W}x{wdg.H}  topmost={root.attributes('-topmost')}")
    for name, it in wdg._items.items():
        kind = wdg.cv.type(it)
        txt = wdg.cv.itemcget(it, "text") if kind == "text" else ""
        b = wdg.cv.bbox(it)
        print(f"  {name:8} {kind:9} bbox={b} text={txt!r}")
    root.destroy()
    return 0


def _init_scale(root):
    """按屏幕 DPI 定缩放，并对齐 Tk 字体缩放，保证像素字号与布局同比例。"""
    global SCALE
    try:
        dpi = float(root.winfo_fpixels("1i"))
        SCALE = max(1.0, min(2.5, dpi / 96.0))
    except Exception:
        SCALE = 1.0
    try:
        root.tk.call("tk", "scaling", SCALE * 96.0 / 72.0)
    except Exception:
        pass


def _dpi_aware():
    """让 Windows 不做位图拉伸，字才不糊。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    global API_URL, DASHBOARD
    args = sys.argv[1:]
    if "--api" in args:
        API_URL = args[args.index("--api") + 1]
        DASHBOARD = API_URL.rsplit("/api/", 1)[0] + "/"
    if "--selftest" in args:
        return selftest()
    if "--dump" in args:
        return dump()

    import tkinter as tk

    cfg = load_cfg()
    if cfg.get("api"):
        API_URL = cfg["api"]
        DASHBOARD = API_URL.rsplit("/api/", 1)[0] + "/"
    _dpi_aware()
    root = tk.Tk()
    _init_scale(root)
    Widget(root, cfg)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
