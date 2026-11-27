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


# 默认浅色（白）。看板上选的颜色会经 monitor 的 /api/widget 写进
# %APPDATA%\ustc-token-monitor\widget.json 的 colors 字段，小窗每 5 秒重读一次。
LIGHT = {
    "bg": "#ffffff",
    "panel": "#ffffff",
    "border": "#d3dae5",
    "txt": "#16202c",
    "dim": "#55606f",
    "faint": "#7b8595",
    "green": "#12915a",
    "amber": "#b06a00",
    "blue": "#1f6feb",
    "red": "#cf2233",
    "track": "#e7ecf3",
    "accentbg": "#eef1f6",
}
C = dict(LIGHT)


def apply_colors(colors):
    """把看板下发的配色套上去；不认识的键忽略，缺的键回落默认浅色。"""
    C.clear()
    C.update(LIGHT)
    if isinstance(colors, dict):
        for k, v in colors.items():
            if k in LIGHT and isinstance(v, str) and v.startswith("#"):
                C[k] = v
    return C


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
    est = d.get("usage_source") == "estimated"     # local 口径：金额是估算
    dayline = f"全天 {fmt_tok(day.get('total'))}"
    if spend is not None:
        dayline += f" {'≈' if est else ''}¥{spend:.2f}"
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


# ------------------------------- 字体 ------------------------------- #
# 目标：看起来像现在的控件——比例字体、够大的字号、圆角、hover 反馈。
UI_FONTS = ("Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Tahoma")
DISP_FONTS = ("Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI",
              "Microsoft YaHei UI", "Tahoma")
_FONTS = {"ui": None, "disp": None}


def init_fonts():
    """选本机存在的第一个字体族。中文由 GDI 字体链接自动补，不用另指一家。"""
    from tkinter import font as tkfont
    try:
        fams = set(tkfont.families())
    except Exception:
        fams = set()

    def pick(cands):
        for c in cands:
            if c in fams:
                return c
        return cands[-1]

    _FONTS["ui"] = pick(UI_FONTS)
    _FONTS["disp"] = pick(DISP_FONTS)
    return _FONTS


def ui_font(px, weight="normal"):
    return (_FONTS["ui"] or "Segoe UI", -S(px), weight)


def disp_font(px, weight="bold"):
    return (_FONTS["disp"] or "Segoe UI", -S(px), weight)


def rounded_pts(x1, y1, x2, y2, r):
    return (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)


# ------------------------------- 右键菜单 ------------------------------- #
class PopupMenu:
    """自绘圆角菜单：原生 tk.Menu 在 Windows 上是一块灰底 3D 边框，太旧。"""

    ROW = 32
    SEP = 9

    def __init__(self, parent, items, x_root, y_root):
        import tkinter as tk

        self.root = parent
        self.items = items
        self._hover = -1
        self._boxes = []
        self.w = S(190)
        pad = S(6)
        self.h = pad * 2 + sum(S(self.SEP) if it["kind"] == "sep" else S(self.ROW)
                               for it in items)

        top = tk.Toplevel(parent)
        self.top = top
        top.overrideredirect(True)
        top.configure(bg=C["panel"])
        top.title("ustc-token-menu")
        self.key = None
        try:
            top.attributes("-topmost", True)
            top.attributes("-alpha", 0.98)
            top.attributes("-transparentcolor", "#010203")
            self.key = "#010203"
        except Exception:
            pass

        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = max(S(2), min(int(x_root), sw - self.w - S(6)))
        y = max(S(2), min(int(y_root), sh - self.h - S(6)))
        top.geometry(f"{self.w}x{self.h}+{x}+{y}")

        self.cv = tk.Canvas(top, width=self.w, height=self.h, highlightthickness=0, bd=0,
                            bg=self.key or C["panel"])
        self.cv.pack(fill="both", expand=True)
        if self.key:
            self.cv.create_polygon(rounded_pts(S(1), S(1), self.w - S(1), self.h - S(1), S(11)),
                                   smooth=True, fill=C["panel"], outline=C["border"], width=1)

        cy = pad
        for idx, it in enumerate(items):
            if it["kind"] == "sep":
                yy = cy + S(self.SEP) // 2
                self.cv.create_line(S(14), yy, self.w - S(14), yy, fill=C["border"], width=1)
                cy += S(self.SEP)
                self._boxes.append(None)
                continue
            x1, y1, x2, y2 = pad, cy, self.w - pad, cy + S(self.ROW)
            self._boxes.append((y1, y2, idx))
            hl = self.cv.create_polygon(rounded_pts(x1, y1, x2, y2, S(8)), smooth=True,
                                        fill=C["panel"], outline="", tags=(f"hl{idx}",))
            self.cv.itemconfigure(hl, state="hidden")
            tx = x1 + S(13)
            if it["kind"] == "check":
                self.cv.create_text(x1 + S(11), (y1 + y2) // 2, text="✓" if it.get("checked") else "",
                                    anchor="w", font=ui_font(12), fill=C["blue"])
                tx = x1 + S(28)
            self.cv.create_text(tx, (y1 + y2) // 2, text=it["label"], anchor="w",
                                font=ui_font(12.5),
                                fill=C["red"] if it.get("danger") else C["txt"])
            if it.get("hint"):
                self.cv.create_text(x2 - S(12), (y1 + y2) // 2, text=it["hint"], anchor="e",
                                    font=ui_font(12), fill=C["faint"])
            cy += S(self.ROW)

        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Button-3>", lambda e: self.close())
        top.bind("<Escape>", lambda e: self.close())
        top.bind("<FocusOut>", lambda e: self.close())
        top.focus_force()
        try:
            top.grab_set()
        except Exception:
            pass

    def _row_at(self, y):
        for b in self._boxes:
            if b and b[0] <= y <= b[1]:
                return b
        return None

    def _motion(self, e):
        box = self._row_at(e.y)
        idx = box[2] if box else -1
        if idx == self._hover:
            return
        self._hover = idx
        for b in self._boxes:
            if b:
                self.cv.itemconfigure(f"hl{b[2]}", state="normal" if b[2] == idx else "hidden",
                                      fill=C["accentbg"])
        self.cv.configure(cursor="hand2" if box else "")

    def _click(self, e):
        inside = 0 <= e.x <= self.w and 0 <= e.y <= self.h
        box = self._row_at(e.y) if inside else None
        self.close()
        if box:
            cmd = self.items[box[2]].get("cmd")
            if cmd:
                self.root.after(10, cmd)

    def close(self):
        try:
            self.top.grab_release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass


# ------------------------------- 窗口 ------------------------------- #
class Widget:
    NORMAL = (300, 158)
    MINI = (200, 62)

    def __init__(self, root, cfg, autostart=True):
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
        self._hit = {}
        self._hover_btn = None
        self._popup = None
        self._colors = cfg.get("colors")
        apply_colors(self._colors)      # 看板选过的配色，开机即用
        init_fonts()

        self.W, self.H = self.size()

        root.overrideredirect(True)
        root.configure(bg=C["panel"])
        root.title("USTC Token")
        self._attr("-topmost", self.topmost)
        self._attr("-alpha", self.alpha)
        self._attr("-toolwindow", True)       # 不占任务栏
        self._transparent_bg()

        x, y = cfg.get("x"), cfg.get("y")
        root.geometry(f"{self.W}x{self.H}+{int(x) if isinstance(x, int) else 60}"
                      f"+{int(y) if isinstance(y, int) else 60}")

        self.cv = tk.Canvas(root, width=self.W, height=self.H, bg=C["panel"],
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag_move)
        self.cv.bind("<ButtonRelease-1>", self._drag_end)
        self.cv.bind("<Double-Button-1>", lambda e: open_dashboard())
        self.cv.bind("<Button-3>", self._menu)
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", self._leave)
        self.cv.bind("<Control-MouseWheel>", self._wheel_alpha)

        self.render()          # 先画骨架（占位），避免首帧空白
        if autostart:
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
    def _in_box(self, name, x, y):
        b = self._hit.get(name)
        return bool(b) and b[0] <= x <= b[2] and b[1] <= y <= b[3]

    def _press(self, e):
        if self._in_box("close", e.x, e.y):
            self.close()
            return
        if self._in_box("board", e.x, e.y):
            open_dashboard()
            return
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        if not hasattr(self, "_dx"):
            return
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, _e):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_cfg(self.cfg)

    def _motion(self, e):
        btn = "close" if self._in_box("close", e.x, e.y) else (
            "board" if self._in_box("board", e.x, e.y) else None)
        if btn == self._hover_btn:
            return
        self._hover_btn = btn
        self.cv.configure(cursor="hand2" if btn else "")
        self._paint_buttons()

    def _leave(self, _e):
        if self._hover_btn is None:
            return
        self._hover_btn = None
        self.cv.configure(cursor="")
        self._paint_buttons()

    def _paint_buttons(self):
        """hover 时：× 变红、看板变蓝。"""
        hot_x = self._hover_btn == "close"
        hot_b = self._hover_btn == "board"
        self.cv.itemconfigure("xbtn", fill=C["red"] if hot_x else C["faint"],
                              width=2 if hot_x else 1)
        self.cv.itemconfigure("boardbtn", fill=C["blue"] if hot_b else C["dim"])

    def _menu(self, e):
        items = [
            {"kind": "item", "label": "打开看板", "hint": "↗", "cmd": open_dashboard},
            {"kind": "sep"},
            {"kind": "check", "label": "迷你模式", "checked": self.mini, "cmd": self.toggle_mini},
            {"kind": "check", "label": "始终置顶", "checked": self.topmost, "cmd": self.toggle_topmost},
            {"kind": "sep"},
            {"kind": "item", "label": "退出", "cmd": self.close, "danger": True},
        ]
        self._popup = PopupMenu(self.root, items, e.x_root, e.y_root)

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
        self._hit.clear()
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
        # 看板选的配色随时可能变，每轮都重读配置（文件很小）
        try:
            cols = load_cfg().get("colors")
        except Exception:
            cols = None
        if cols != self._colors:
            self._colors = cols
            apply_colors(cols)
            self._repaint_bg()
        self._busy = False
        try:
            self.root.after(0, self.render)
        except Exception:
            pass

    def _repaint_bg(self):
        """换配色时把窗口/画布底色跟上（圆角透明键不变）。"""
        try:
            self.root.configure(bg=C["panel"])
            self.cv.configure(bg=self.cv_bg if self.rounded else C["panel"])
        except Exception:
            pass
        self._items.clear()
        self.cv.delete("all")

    # ---- 绘制 ---- #
    def _rr(self, x1, y1, x2, y2, r, **kw):
        """底板多边形：复用同一个 item 并永远压在最底层。

        早期版本每次刷新都 create_polygon 一个新的，新底板叠在文字上面，
        于是卡片刷新一次就变成一块纯色（文字被盖住）。
        """
        pts = rounded_pts(x1, y1, x2, y2, r)
        it = self._items.get("panel")
        if it is None:
            it = self.cv.create_polygon(pts, smooth=True, **kw)
            self._items["panel"] = it
        else:
            self.cv.coords(it, *pts)
            self.cv.itemconfigure(it, **kw)
        self.cv.tag_lower(it)
        return it

    def _text(self, name, x, y, txt, font, fill, anchor="w", tag=None):
        it = self._items.get(name)
        kw = {}
        if tag:
            kw["tags"] = (tag,)
        if it is None:
            it = self.cv.create_text(x, y, text=txt, font=font, fill=fill, anchor=anchor, **kw)
            self._items[name] = it
        else:
            self.cv.coords(it, x, y)
            self.cv.itemconfigure(it, text=txt, font=font, fill=fill, anchor=anchor)
        return it

    def _drop(self, *names):
        """删掉某个元素（连同 _bar/_pill 派生出来的格子）——用于"这次不该有它"。
        光不画是不够的：上一帧画过的元素会留在画布上。"""
        for n in names:
            for key in (n, n + "t", n + "f", n + "bg"):
                it = self._items.pop(key, None)
                if it is not None:
                    try:
                        self.cv.delete(it)
                    except Exception:
                        pass

    def _rect(self, name, x1, y1, x2, y2, fill, **kw):
        it = self._items.get(name)
        if it is None:
            it = self.cv.create_rectangle(x1, y1, x2, y2, fill=fill, width=0, **kw)
            self._items[name] = it
        else:
            self.cv.coords(it, x1, y1, x2, y2)
            self.cv.itemconfigure(it, fill=fill)
        return it

    def _pill(self, name, x1, y1, x2, y2, r, fill):
        it = self._items.get(name)
        h = max(1, y2 - y1)
        pts = rounded_pts(x1, y1, x2, y2, min(r, h / 2.0))
        if it is None:
            it = self.cv.create_polygon(pts, smooth=True, fill=fill, outline="")
            self._items[name] = it
        else:
            self.cv.coords(it, *pts)
            self.cv.itemconfigure(it, fill=fill)
        return it

    def _bar(self, name, x1, y, w, pct, color):
        h = S(7)
        self._pill(name + "t", x1, y, x1 + w, y + h, h / 2.0, C["track"])
        ww = max(0.0, min(100.0, pct)) / 100.0 * w
        if ww >= 1:
            self._pill(name + "f", x1, y, x1 + max(h, ww), y + h, h / 2.0, color)
        else:
            self._pill(name + "f", x1, y, x1, y + h, 0.5, C["panel"])

    def _close_x(self, cx, cy, k):
        """关闭按钮：两条线画的 ×，不依赖字体里有没有 ✕。"""
        if "close1" not in self._items:
            self._items["close1"] = self.cv.create_line(cx - k, cy - k, cx + k, cy + k,
                                                        fill=C["faint"], width=1, tags=("xbtn",))
            self._items["close2"] = self.cv.create_line(cx - k, cy + k, cx + k, cy - k,
                                                        fill=C["faint"], width=1, tags=("xbtn",))
        else:
            self.cv.coords(self._items["close1"], cx - k, cy - k, cx + k, cy + k)
            self.cv.coords(self._items["close2"], cx - k, cy + k, cx + k, cy - k)

    def render(self, data=None):
        if data is None:
            data = self.data
        v = data or {}
        W, H = self.W, self.H
        mini = self.mini
        pad = S(14)
        f_hero = disp_font(24 if mini else 30, "bold")
        f_unit = ui_font(12)
        f_lbl = ui_font(12)
        f_foot = ui_font(11)
        f_btn = ui_font(11.5)
        hero_y = S(42) if mini else S(43)
        unit_y = hero_y + S(3)

        if self.rounded:
            self.cv.configure(bg=self.cv_bg)
            # 内缩 1px：描边完整落在画布内，外圈 1px 是透明色键（干净的圆角边）
            self._rr(S(1), S(1), W - S(1), H - S(1), S(12),
                     fill=C["panel"], outline=C["border"], width=1)
        else:
            self.cv.configure(bg=C["panel"])

        # ---- 顶栏：看板入口 + 关闭 ----
        cy = S(13)
        board = self._text("board", W - S(36), cy, "看板 ↗", f_btn, C["dim"], anchor="e",
                           tag="boardbtn")
        bb = self.cv.bbox(board)
        self._hit["board"] = (bb[0] - S(6), bb[1] - S(4), bb[2] + S(5), bb[3] + S(4))
        cx = W - S(15)
        self._close_x(cx, cy, S(4))
        self._hit["close"] = (cx - S(12), cy - S(12), cx + S(12), cy + S(12))

        # ---- 第一行：速度 ----
        err = v.get("err") or self.err
        dot = C["red"] if err else C["green"]
        self._rect("dot", pad, hero_y - S(8), pad + S(4), hero_y + S(3), dot)
        tps_txt = fmt_tps(v.get("tps")) if v else "—"
        tw = self._text("tps", pad + S(10), hero_y, tps_txt, f_hero, C["txt"], anchor="w")
        ux = self.cv.bbox(tw)[2] + S(4)
        uw = self._text("unit", ux, unit_y, "tok/s", f_unit, C["dim"])
        idle = v.get("idle") or ""
        self._text("idle", self.cv.bbox(uw)[2] + S(6), unit_y, ("· " + idle) if idle else "",
                   f_btn, C["faint"])
        self._text("m5", W - pad, hero_y, f"5m {v.get('m5', '—')}", f_lbl, C["dim"], anchor="e")

        if self.mini:
            self._paint_buttons()
            return

        # ---- 第二行：1h 用量 · 首包 ----
        self._text("h1", pad, S(74), f"1h {v.get('h1', '—')}  ·  {v.get('h1req', 0)} 次",
                   f_lbl, C["dim"])
        self._text("ttft", W - pad, S(74), v.get("ttft", "首包 —"), f_lbl, C["dim"], anchor="e")

        # ---- 限额两条（没有额度数据时整行不画，别显示 0） ----
        barw = W - pad - S(26) - S(58)
        if v.get("b3") or v.get("b12"):
            self._drop("nolimit")
            for idx, key in ((0, "b3"), (1, "b12")):
                b = v.get(key)
                txt, pct = budget_text(b)
                y = S(96) + idx * S(23)
                self._text(key + "l", pad, y + S(3), "3h" if idx == 0 else "12h", f_btn, C["faint"])
                color = C["red"] if pct > 85 else C["amber"] if pct > 60 else C["blue"]
                self._bar(key, pad + S(26), y, barw, pct, color)
                self._text(key + "v", W - pad, y + S(3), txt, f_btn, C["dim"], anchor="e")
        else:
            self._drop("b3l", "b3v", "b3", "b12l", "b12v", "b12")
            self._text("nolimit", pad, S(99), "本机日志模式 · 无额度信息", f_btn, C["faint"])

        # ---- 底线：时间 / 全天 ----
        if err:
            msg = str(err)
            msg = msg if len(msg) <= 26 else msg[:25] + "…"
            self._text("foot", pad, H - S(13), "⚠ " + msg, f_foot, C["red"])
            self._text("day", W - pad, H - S(13), "", f_foot, C["faint"], anchor="e")
        else:
            self._text("foot", pad, H - S(13),
                       f"{v.get('clock', '--:--:--')} · {v.get('ago', '')}", f_foot, C["faint"])
            self._text("day", W - pad, H - S(13), v.get("dayline", "全天 —"), f_foot, C["faint"],
                       anchor="e")
        self._paint_buttons()


def open_dashboard():
    """让监测器去开看板：已经有看板窗口就抬到最前，不会每次都新开一个。
    监测器没跑（或者旧版本没有这个接口）才退回自己开浏览器。"""
    url = DASHBOARD + "api/dashboard/open"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with _OPENER.open(req, timeout=3) as r:
            r.read()
        return
    except Exception:
        pass
    webbrowser.open(DASHBOARD)


# ------------------------------- 自检 ------------------------------- #
def selftest():
    import tkinter as tk

    ok = []
    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    _init_scale(root)
    wdg = Widget(root, {"x": 40, "y": 40}, autostart=False)

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
    # 没有额度数据时不再画 0，而是给一行说明（local 模式）
    ok.append(("空数据不崩", t.get("tps") == "—" and t.get("b3v") is None
               and "本机日志模式" in (t.get("nolimit") or ""),
               f"{t.get('tps')} / {t.get('nolimit')}"))
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

    # 配色：看板下发的 colors 要能生效，非法键要忽略
    apply_colors({"panel": "#fbf6ec", "txt": "#2a2418", "border": "#e3d9c6", "bogus": "#000000"})
    wdg.mini = False
    wdg.resize()
    wdg.render(fix_full)
    t = texts()
    panel_fill = wdg.cv.itemcget(wdg._items["panel"], "fill")
    ok.append(("换配色后文字仍在", t.get("tps") == "58.3", t.get("tps")))
    ok.append(("配色生效（面板）", panel_fill == "#fbf6ec", panel_fill))
    ok.append(("非法配色键被忽略", "bogus" not in C, C.get("bogus")))
    apply_colors(None)                              # 复位默认浅色
    ok.append(("复位为白底", C["panel"] == "#ffffff", C["panel"]))

    # 按钮命中区：× 在右上角、看板在它左边、其余地方点下去是拖动
    W_, H_ = wdg.W, wdg.H
    ok.append(("× 可点", wdg._in_box("close", W_ - S(15), S(13)), wdg._hit.get("close")))
    ok.append(("看板可点", wdg._in_box("board", W_ - S(46), S(13)), wdg._hit.get("board")))
    ok.append(("空白处不是按钮", not wdg._in_box("close", S(40), H_ - S(20)), ""))

    # 自绘菜单：行高、宽度、分隔不占行
    pm = PopupMenu(root, [
        {"kind": "item", "label": "打开看板", "hint": "↗", "cmd": None},
        {"kind": "sep"},
        {"kind": "check", "label": "迷你模式", "checked": True, "cmd": None},
        {"kind": "item", "label": "退出", "danger": True, "cmd": None},
    ], S(20), S(20))
    ok.append(("菜单可建", pm.h > S(40) and len(pm._boxes) == 4, f"{pm.w}x{pm.h}"))
    pm.close()

    root.destroy()

    fails = [n for n, good, _ in ok if not good]
    for name, good, val in ok:
        print(f"{'ok  ' if good else 'FAIL'} {name}" + ("" if good else f"  实际={val!r}"))
    print(f"\n共 {len(ok)} 项，失败 {len(fails)} 项" + (("：" + " / ".join(fails)) if fails else ""))
    return 0 if not fails else 1


def demo_menu():
    """把浮窗和右键菜单都摆出来（截图核对用）。"""
    import tkinter as tk

    _dpi_aware()
    root = tk.Tk()
    _init_scale(root)
    root.withdraw()
    wdg = Widget(root, {"x": 80, "y": 80, "theme": "paper"})
    wdg.root.deiconify()
    wdg.root.geometry(f"+{S(80)}+{S(80)}")

    def show():
        wdg._menu(type("E", (), {"x_root": S(80) + S(306), "y_root": S(80) + S(52)})())
        root.after(9000, root.destroy)

    root.after(600, show)
    root.mainloop()
    return 0


def dump():
    import tkinter as tk

    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    _init_scale(root)
    wdg = Widget(root, {"x": 40, "y": 40}, autostart=False)
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
    if "--demo-menu" in args:
        return demo_menu()

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
