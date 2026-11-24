from __future__ import annotations

import io
import sys
import threading
from bisect import bisect_right
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import tkinter as tk
except ImportError:
    tk = None
try:
    from tkinter import font as tkfont
except ImportError:
    tkfont = None

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
    PIL_OK = True
except Exception:
    PIL_OK = False
try:
    from PIL import ImageTk
    PIL_TK_OK = True
except Exception:
    PIL_TK_OK = False


_Canvas = getattr(tk, "Canvas", object)
_Frame = getattr(tk, "Frame", object)


BG0 = "#000000"
BG1 = "#09090e"
INPUT = "#14141c"
HOVER = "#1b1b26"
BORDER = "#23232e"
PURPLE = "#a855f7"
PURPLE_HI = "#c084fc"
PURPLE_DIM = "#7c3aed"
PURPLE_SOFT = "#241838"
YELLOW = "#facc15"
GREEN = "#22c55e"
GREEN_DIM = "#16a34a"
RED = "#f87171"
FG = "#f1f1f5"
FG_DIM = "#9b9bab"
FG_MUTED = "#5f5f6e"
TEXT_ON_ACCENT = "#0a0612"

FONT = "Segoe UI"
FONT_SEMI: Optional[str] = "Segoe UI Semibold"
MONO = "Consolas"


_scale = 1.0


def set_scale(value: float) -> None:
    global _scale
    try:
        _scale = max(1.0, min(3.0, float(value)))
    except (TypeError, ValueError):
        _scale = 1.0


def get_scale() -> float:
    return _scale


def S(px: float) -> int:
    return int(round(px * _scale))


def fnt(size: int, bold: bool = False) -> Tuple:
    if bold:
        return (FONT_SEMI, size) if FONT_SEMI else (FONT, size, "bold")
    return (FONT, size)


def init_fonts() -> None:
    global FONT, FONT_SEMI
    if tkfont is None or not hasattr(tkfont, "families"):
        return
    try:
        families = set(tkfont.families())
    except Exception:
        return
    for regular, semi in (("Segoe UI Variable Text", "Segoe UI Variable Text Semibold"),
                          ("Segoe UI", "Segoe UI Semibold"), ("SF Pro Text", None), ("Helvetica Neue", None),
                          ("Inter", None), ("Arial", None), ("DejaVu Sans", None)):
        if regular in families:
            FONT, FONT_SEMI = regular, (semi if semi in families else None)
            break
    _font_cache.clear()


def enable_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def apply_windows_dark_titlebar(win: Any) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                           ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


_font_cache: Dict[Tuple, Any] = {}


def measure(text: str, font: Tuple) -> int:
    if tkfont is None or not hasattr(tkfont, "Font"):
        return len(text) * 7
    f = _font_cache.get(font)
    if f is None:
        f = _font_cache[font] = tkfont.Font(font=font)
    return int(f.measure(text))


def ellipsize(text: str, font: Tuple, max_px: int) -> str:
    if max_px <= 0:
        return ""
    if measure(text, font) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "\u2026", font) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + "\u2026") if lo else "\u2026"


def round_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> List[float]:
    r = max(0.0, min(float(r), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def thumb_geometry(first: float, last: float, track_px: int, min_px: int) -> Optional[Tuple[int, int]]:
    visible = last - first
    if track_px <= 0 or visible >= 0.9999:
        return None
    length = max(min_px, int(visible * track_px))
    length = min(length, track_px)
    avail = track_px - length
    top = int(round((first / (1.0 - visible)) * avail)) if visible < 1.0 else 0
    top = max(0, min(avail, top))
    return top, top + length


def wheel_units(event: Any) -> int:
    num = getattr(event, "num", 0)
    if num == 4:
        return -1
    if num == 5:
        return 1
    d = getattr(event, "delta", 0) or 0
    if not d:
        return 0
    step = max(1, abs(int(d)) if sys.platform == "darwin" else abs(int(d)) // 120)
    return -step if d > 0 else step


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(c1: str, c2: str, t: float) -> str:
    a, b = hex_to_rgb(c1), hex_to_rgb(c2)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


_pil_fonts: Dict[int, Any] = {}


def _resample() -> Any:
    return getattr(Image, "Resampling", Image).LANCZOS


def _initial_font(px: int) -> Any:
    f = _pil_fonts.get(px)
    if f is not None:
        return f
    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
                 "LiberationSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            f = ImageFont.truetype(name, px)
            break
        except Exception:
            f = None
    if f is None:
        try:
            f = ImageFont.load_default(px)
        except Exception:
            f = ImageFont.load_default()
    _pil_fonts[px] = f
    return f


def _shape_mask(size: int, shape: str) -> Any:
    ss = 4
    big = size * ss
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    if shape == "squircle":
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.28), fill=255)
    else:
        d.ellipse((0, 0, big - 1, big - 1), fill=255)
    return mask.resize((size, size), _resample())


def render_avatar(raw: Optional[bytes], size: int, initial: str, color: str, shape: str = "circle") -> Any:
    base = None
    if raw:
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
            base = ImageOps.fit(im.convert("RGBA"), (size, size), _resample())
        except Exception:
            base = None
    if base is None:
        base = Image.new("RGBA", (size, size), hex_to_rgb(color) + (255,))
        d = ImageDraw.Draw(base)
        font = _initial_font(max(8, int(size * 0.46)))
        text = ((initial or "?").strip()[:1] or "?").upper()
        try:
            d.text((size / 2, size / 2), text, fill=(0, 0, 0, 255), font=font, anchor="mm")
        except Exception:
            try:
                left, top, right, bottom = d.textbbox((0, 0), text, font=font)
                d.text(((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top),
                       text, fill=(0, 0, 0, 255), font=font)
            except Exception:
                pass
    alpha = ImageChops.multiply(base.getchannel("A"), _shape_mask(size, shape))
    base.putalpha(alpha)
    return base


_ui_fonts: Dict[Tuple[int, bool], Any] = {}


def _ui_font(px: int, semibold: bool = False) -> Any:
    key = (px, semibold)
    f = _ui_fonts.get(key)
    if f is not None:
        return f
    names = (("seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf")
             if semibold else ("segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf"))
    for name in names:
        try:
            f = ImageFont.truetype(name, px)
            break
        except Exception:
            f = None
    if f is None:
        try:
            f = ImageFont.load_default(px)
        except Exception:
            f = ImageFont.load_default()
    _ui_fonts[key] = f
    return f


def _draw_text(d: Any, xy: Tuple[float, float], text: str, font: Any, fill: Tuple, anchor: str) -> None:
    try:
        d.text(xy, text, font=font, fill=fill, anchor=anchor)
    except Exception:
        try:
            d.text(xy, text, font=font, fill=fill)
        except Exception:
            pass


def render_emoji(raw: Optional[bytes], size: int) -> Any:
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if raw:
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
            im = ImageOps.contain(im.convert("RGBA"), (size, size), _resample())
            out.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
        except Exception:
            pass
    return out


def render_chip(raw: Optional[bytes], fallback_text: str, count_text: str, height: int) -> Any:
    ss = 3
    h = height
    e = int(h * 0.62)
    pad, gap = int(h * 0.34), int(h * 0.22)
    font = _ui_font(max(8, int(h * 0.46 * ss)), True)
    meter = ImageDraw.Draw(Image.new("L", (1, 1)))
    try:
        tw = meter.textlength(count_text, font=font) / ss
    except Exception:
        tw = len(count_text) * h * 0.3
    w = int(pad + e + gap + tw + pad + 1)
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=int(h * ss * 0.3), fill=hex_to_rgb(INPUT) + (255,),
                        outline=hex_to_rgb(BORDER) + (255,), width=ss)
    if raw:
        em = render_emoji(raw, e * ss)
        big.alpha_composite(em, (pad * ss, (h * ss - e * ss) // 2))
    elif fallback_text:
        _draw_text(d, (pad * ss + e * ss / 2, h * ss / 2), fallback_text, font, hex_to_rgb(FG_DIM) + (255,), "mm")
    _draw_text(d, ((pad + e + gap) * ss, h * ss / 2), count_text, font, hex_to_rgb(FG_DIM) + (255,), "lm")
    return big.resize((w, h), _resample())


def can_render_images() -> bool:
    return PIL_OK and PIL_TK_OK


def make_emoji_photo(raw: Optional[bytes], size: int) -> Any:
    return ImageTk.PhotoImage(render_emoji(raw, size))


def make_chip_photo(raw: Optional[bytes], fallback_text: str, count_text: str, height: int) -> Any:
    return ImageTk.PhotoImage(render_chip(raw, fallback_text, count_text, height))


def _fallback_photo(raw: Optional[bytes], size: int, color: str) -> Any:
    if raw:
        try:
            p = tk.PhotoImage(data=raw)
            factor = max(1, p.width() // max(1, size))
            return p.subsample(factor, factor) if factor > 1 else p
        except Exception:
            pass
    img = tk.PhotoImage(width=size, height=size)
    img.put(color, to=(0, 0, size, size))
    return img


def make_photo(raw: Optional[bytes], size: int, initial: str, color: str, shape: str = "circle") -> Any:
    if PIL_OK and PIL_TK_OK:
        try:
            return ImageTk.PhotoImage(render_avatar(raw, size, initial, color, shape))
        except Exception:
            pass
    return _fallback_photo(raw, size, color)


def load_logo(path: Any, px: int) -> Optional[Any]:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception:
        return None
    if PIL_OK and PIL_TK_OK:
        try:
            return ImageTk.PhotoImage(render_avatar(data, px, "R", PURPLE, "squircle"))
        except Exception:
            pass
    try:
        return _fallback_photo(data, px, PURPLE)
    except Exception:
        return None


class ImageStore:
    def __init__(self, fetch_many: Callable[[Dict[str, Optional[str]]], Dict[str, bytes]],
                 deliver: Callable[[Dict[str, bytes]], None], max_parallel: int = 2):
        self.raw: Dict[str, bytes] = {}
        self.failed: set = set()
        self._asked: set = set()
        self._photos: Dict[Tuple, Any] = {}
        self._fetch_many = fetch_many
        self._deliver = deliver
        self._gate = threading.Semaphore(max_parallel)

    def request(self, urls: Iterable[Optional[str]]) -> int:
        new = [u for u in dict.fromkeys(urls) if u and u not in self._asked and u not in self.raw]
        if not new:
            return 0
        self._asked.update(new)
        threading.Thread(target=self._run, args=(new,), daemon=True).start()
        return len(new)

    def _run(self, urls: List[str]) -> None:
        got: Dict[str, bytes] = {}
        try:
            with self._gate:
                got = self._fetch_many({u: u for u in urls})
        except Exception:
            got = {}
        self.failed.update(u for u in urls if u not in got)
        try:
            self._deliver(got)
        except Exception:
            pass

    def is_failed(self, url: Optional[str]) -> bool:
        return bool(url) and url in self.failed

    def ingest(self, got: Dict[str, bytes]) -> None:
        if not got:
            return
        self.raw.update(got)
        for key in [k for k in self._photos if k[0] in got]:
            del self._photos[key]

    def forget_failures(self) -> None:
        self._asked = set(self.raw)
        self.failed.clear()

    def has(self, url: Optional[str]) -> bool:
        return bool(url) and url in self.raw

    def photo(self, url: Optional[str], size: int, initial: str, color: str, shape: str = "circle") -> Any:
        real = self.has(url)
        key = (url, size, shape) if real else ("", size, shape, initial, color)
        p = self._photos.get(key)
        if p is None:
            p = make_photo(self.raw.get(url) if real else None, size, initial, color, shape)
            self._photos[key] = p
        return p


def _bg_of(widget: Any, default: str = BG1) -> str:
    try:
        return str(widget.cget("bg"))
    except Exception:
        return default


class Tooltip:
    def __init__(self, widget: Any, text: str, delay: int = 450):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip: Any = None
        self._job: Any = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e: Any = None) -> None:
        self._cancel()
        if self.text:
            self._job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + S(10)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + S(6)
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tk.Label(tip, text=self.text, bg=INPUT, fg=FG, font=fnt(9), bd=0, padx=S(10), pady=S(6),
                     wraplength=S(280), justify="left", highlightthickness=1,
                     highlightbackground=BORDER).pack()
            tip.update_idletasks()
            x = max(S(4), min(x, self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - S(8)))
            tip.wm_geometry(f"+{x}+{y}")
            self.tip = tip
        except Exception:
            self.tip = None

    def _hide(self, _e: Any = None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class Button(_Canvas):
    VARIANTS: Dict[str, Dict[str, Optional[str]]] = {
        "primary":   dict(fill=PURPLE, hover=PURPLE_HI, press=PURPLE_DIM, fg=TEXT_ON_ACCENT, outline=None),
        "success":   dict(fill=GREEN, hover="#34d874", press=GREEN_DIM, fg="#03140a", outline=None),
        "secondary": dict(fill=INPUT, hover=HOVER, press=BORDER, fg=FG, outline=BORDER),
        "ghost":     dict(fill=None, hover=HOVER, press=BORDER, fg=FG_DIM, outline=None),
        "danger":    dict(fill="#2c1117", hover="#3b1820", press="#4a1d28", fg="#fca5a5", outline="#4a1d28"),
        "muted":     dict(fill=INPUT, hover=HOVER, press=BORDER, fg=FG_MUTED, outline=BORDER),
        "chip_off":  dict(fill=INPUT, hover=HOVER, press=BORDER, fg=FG_DIM, outline=BORDER),
        "chip_on":   dict(fill=PURPLE_SOFT, hover="#2e1f4a", press="#37245a", fg="#e0c8ff", outline=PURPLE_DIM),
    }

    def __init__(self, parent: Any, text: str = "", command: Optional[Callable[[], None]] = None,
                 variant: str = "secondary", height: int = 36, width: Optional[int] = None,
                 size: int = 10, bold: bool = True, radius: int = 9, bg: Optional[str] = None,
                 trigger: str = "release"):
        self._text, self._variant, self._cmd, self._trigger = text, variant, command, trigger
        self._font = fnt(size, bold)
        self._h, self._r = S(height), S(radius)
        self._bgc = bg or _bg_of(parent)
        self._enabled, self._hover, self._down, self._focus = True, False, False, False
        self._anim_from: Optional[Tuple[str, str, str]] = None
        self._anim_t, self._anim_job = 1.0, None
        self._auto_w = width is None
        w = S(width) if width else measure(text, self._font) + 2 * S(16)
        super().__init__(parent, width=w, height=self._h, bg=self._bgc, highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<Destroy>", lambda _e: self._stop_anim(), add="+")
        self._draw()


    def set_text(self, text: str) -> None:
        self._text = text
        if self._auto_w:
            self.configure(width=measure(text, self._font) + 2 * S(16))
        self._draw()

    def set_variant(self, variant: str, animate: bool = False) -> None:
        if variant == self._variant:
            return
        if animate:
            self._anim_from = self._shown_colors()
            self._variant = variant
            self._anim_t = 0.0
            self._stop_anim()
            self._anim_tick()
        else:
            self._variant = variant
            self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def is_enabled(self) -> bool:
        return self._enabled

    def set_command(self, command: Optional[Callable[[], None]]) -> None:
        self._cmd = command

    def invoke(self) -> None:
        if self._enabled and self._cmd:
            self._cmd()


    def _stop_anim(self) -> None:
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None

    def _anim_tick(self) -> None:
        self._anim_t += 0.34
        if self._anim_t >= 1.0:
            self._anim_from, self._anim_job = None, None
        else:
            self._anim_job = self.after(16, self._anim_tick)
        self._draw()


    def _on_enter(self, _e: Any = None) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _e: Any = None) -> None:
        self._hover = self._down = False
        self._draw()

    def _on_press(self, _e: Any = None) -> None:
        if not self._enabled:
            return
        if self._trigger == "press":
            self.invoke()
            return
        self._down = True
        self.focus_set()
        self._draw()

    def _on_release(self, e: Any) -> None:
        if self._trigger == "press":
            return
        was_down, self._down = self._down, False
        self._draw()
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if was_down and inside:
            self.invoke()

    def _set_focus(self, on: bool) -> None:
        self._focus = on
        self._draw()


    def _resolved(self) -> Tuple[str, str, str]:
        spec = self.VARIANTS.get(self._variant, self.VARIANTS["secondary"])
        if not self._enabled:
            return INPUT, FG_MUTED, INPUT
        fill = spec["press"] if self._down else (spec["hover"] if self._hover else spec["fill"])
        fg = spec["fg"]
        if self._hover and spec["fill"] is None:
            fg = FG
        fill = fill or self._bgc
        return fill, fg or FG, spec["outline"] or fill

    def _shown_colors(self) -> Tuple[str, str, str]:
        now = self._resolved()
        if self._anim_from is None:
            return now
        t = min(1.0, self._anim_t)
        return tuple(mix(a, b, t) for a, b in zip(self._anim_from, now))

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        w = w if w > 1 else int(float(self.cget("width")))
        h = self._h
        fill, fg, outline = self._shown_colors()
        self.create_polygon(round_rect_points(1, 1, w - 1, h - 1, self._r), smooth=True, fill=fill,
                            outline=outline, width=1)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)
        if self._focus and self._enabled:
            self.create_polygon(round_rect_points(1, 1, w - 1, h - 1, self._r), smooth=True, fill="",
                                outline=PURPLE_HI, width=1)


class Field(_Frame):
    def __init__(self, parent: Any, textvariable: Any = None, placeholder: str = "", show: str = "",
                 width: Optional[int] = None, bg: str = INPUT, font_size: int = 10):
        super().__init__(parent, bg=bg, highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=BORDER, bd=0)
        self.var = textvariable if textvariable is not None else tk.StringVar()
        self.entry = tk.Entry(self, textvariable=self.var, bg=bg, fg=FG, insertbackground=FG, relief="flat",
                              bd=0, highlightthickness=0, font=fnt(font_size), show=show,
                              selectbackground=PURPLE_DIM, selectforeground="#ffffff",
                              disabledbackground=bg, disabledforeground=FG_MUTED)
        if width:
            self.entry.configure(width=width)
        self.entry.pack(fill="x", padx=S(10), pady=S(7))
        self._ph = tk.Label(self, text=placeholder, bg=bg, fg=FG_MUTED, font=fnt(font_size), bd=0,
                            cursor="xterm", anchor="w")
        self._ph.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda _e: self.configure(highlightbackground=PURPLE, highlightcolor=PURPLE))
        self.entry.bind("<FocusOut>", lambda _e: self.configure(highlightbackground=BORDER, highlightcolor=BORDER))
        self.var.trace_add("write", lambda *_a: self._sync_placeholder())
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        if self.var.get() or not self._ph.cget("text"):
            self._ph.place_forget()
        else:
            self._ph.place(x=S(10), rely=0.5, anchor="w")

    def focus(self) -> None:
        self.entry.focus_set()

    def set_hidden(self, hidden: bool) -> None:
        self.entry.configure(show="*" if hidden else "")


class Switch(_Canvas):
    def __init__(self, parent: Any, variable: Any, bg: Optional[str] = None):
        self.var = variable
        self._sw, self._sh = S(38), S(22)
        super().__init__(parent, width=self._sw, height=self._sh, bg=bg or _bg_of(parent), highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1)
        self.bind("<Button-1>", lambda _e: self.toggle())
        self.bind("<space>", lambda _e: self.toggle())
        self.var.trace_add("write", lambda *_a: self._draw())
        self._draw()

    def toggle(self) -> None:
        self.var.set(not self.var.get())

    def _draw(self) -> None:
        self.delete("all")
        on = bool(self.var.get())
        w, h, pad = self._sw, self._sh, S(3)
        self.create_polygon(round_rect_points(0, 0, w, h, h / 2), smooth=True,
                            fill=PURPLE if on else "#2a2a37", outline="")
        d = h - 2 * pad
        x = (w - pad - d) if on else pad
        self.create_oval(x, pad, x + d, pad + d, fill="#ffffff" if on else "#9b9bab", outline="")


class ToggleRow(_Frame):
    def __init__(self, parent: Any, text: str, variable: Any, hint: str = "", bg: str = BG1):
        super().__init__(parent, bg=bg)
        self.switch = Switch(self, variable, bg=bg)
        self.switch.pack(side="left")
        self.label = tk.Label(self, text=text, bg=bg, fg=FG, font=fnt(10), cursor="hand2")
        self.label.pack(side="left", padx=(S(10), 0))
        self.label.bind("<Button-1>", lambda _e: self.switch.toggle())
        if hint:
            Tooltip(self.label, hint)
            Tooltip(self.switch, hint)


class ChipGroup(_Frame):
    def __init__(self, parent: Any, options: Sequence[Tuple[str, str]], value: Any, multi: bool = False,
                 bg: str = BG1, on_change: Optional[Callable[[], None]] = None, height: int = 32, size: int = 9):
        super().__init__(parent, bg=bg)
        self.multi, self.on_change = multi, on_change
        self.selected: List[str] = list(value) if multi else [value]
        self._buttons: Dict[str, Button] = {}
        for i, (val, label) in enumerate(options):
            b = Button(self, label, command=lambda v=val: self._clicked(v), variant=self._variant_for(val),
                       height=height, size=size, radius=8, bg=bg, width=10, trigger="press")
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else S(4), 0))
            self.grid_columnconfigure(i, weight=1, uniform="chips")
            self._buttons[val] = b

    def _variant_for(self, val: str) -> str:
        return "chip_on" if val in self.selected else "chip_off"

    def _clicked(self, val: str) -> None:
        if self.multi:
            if val in self.selected:
                if len(self.selected) == 1:
                    return
                self.selected.remove(val)
            else:
                self.selected.append(val)
        else:
            self.selected = [val]
        for v, b in self._buttons.items():
            b.set_variant(self._variant_for(v), animate=True)
        if self.on_change:
            self.on_change()

    def get(self) -> Any:
        return list(self.selected) if self.multi else self.selected[0]

    def set_label(self, val: str, text: str) -> None:
        b = self._buttons.get(val)
        if b is not None:
            b.set_text(text)

    def select(self, val: str) -> None:
        if val in self._buttons and (self.multi or self.selected != [val]):
            self._clicked(val)

    def set_enabled(self, enabled: bool) -> None:
        for b in self._buttons.values():
            b.set_enabled(enabled)


class ProgressBar(_Canvas):
    def __init__(self, parent: Any, height: int = 6, bg: Optional[str] = None, trough: str = INPUT,
                 color: str = PURPLE, rounded: bool = True):
        self._h = S(height)
        self._trough, self._color, self._rounded = trough, color, rounded
        self._mode, self._value, self._pos, self._job = "idle", 0.0, 0.0, None
        super().__init__(parent, width=S(20), height=self._h, bg=bg or _bg_of(parent), highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Destroy>", lambda _e: self._stop_anim(), add="+")

    def set_color(self, color: str) -> None:
        self._color = color
        self._draw()

    def set_idle(self) -> None:
        self._stop_anim()
        self._mode, self._value = "idle", 0.0
        self._draw()

    def set_determinate(self, pct: float) -> None:
        self._stop_anim()
        self._mode, self._value = "det", max(0.0, min(100.0, float(pct)))
        self._draw()

    def set_indeterminate(self) -> None:
        if self._mode == "ind":
            return
        self._mode, self._pos = "ind", 0.0
        self._tick()

    def _stop_anim(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self) -> None:
        if self._mode != "ind":
            return
        self._pos = (self._pos + 0.022) % 1.35
        self._draw()
        self._job = self.after(30, self._tick)

    def _rect(self, x1: float, x2: float, fill: str) -> None:
        if x2 - x1 < 1:
            return
        if self._rounded:
            self.create_polygon(round_rect_points(x1, 0, x2, self._h, self._h / 2), smooth=True, fill=fill, outline="")
        else:
            self.create_rectangle(x1, 0, x2, self._h, fill=fill, outline="")

    def _draw(self) -> None:
        self.delete("all")
        w = max(2, self.winfo_width())
        self._rect(0, w, self._trough)
        if self._mode == "det" and self._value > 0:
            self._rect(0, max(self._h, w * self._value / 100.0), self._color)
        elif self._mode == "ind":
            seg = w * 0.3
            x2 = self._pos * w
            self._rect(max(0, x2 - seg), min(w, x2), self._color)


class SlimScrollbar(_Canvas):
    def __init__(self, parent: Any, command: Callable[..., Any], bg: str = BG1, width: int = 10):
        self.command = command
        self._first, self._last = 0.0, 1.0
        self._drag_off: Optional[int] = None
        self._hover = False
        super().__init__(parent, width=S(width), bg=bg, highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag_off", None))

    def set(self, first: Any, last: Any) -> None:
        self._first, self._last = float(first), float(last)
        self._draw()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw()

    def _geom(self) -> Optional[Tuple[int, int]]:
        return thumb_geometry(self._first, self._last, self.winfo_height(), S(28))

    def _press(self, e: Any) -> None:
        g = self._geom()
        if g is None:
            return
        if g[0] <= e.y <= g[1]:
            self._drag_off = e.y - g[0]
        else:
            self.command("scroll", -1 if e.y < g[0] else 1, "pages")

    def _motion(self, e: Any) -> None:
        g = self._geom()
        if g is None or self._drag_off is None:
            return
        track, length = self.winfo_height(), g[1] - g[0]
        avail = max(1, track - length)
        visible = self._last - self._first
        frac = (e.y - self._drag_off) / avail * (1.0 - visible)
        self.command("moveto", max(0.0, min(1.0 - visible, frac)))

    def _draw(self) -> None:
        self.delete("all")
        g = self._geom()
        if g is None:
            return
        w = max(2, self.winfo_width())
        inset = S(2)
        self.create_polygon(round_rect_points(inset, g[0], w - inset, g[1], (w - 2 * inset) / 2), smooth=True,
                            fill=PURPLE_DIM if self._hover else "#2d2d3a", outline="")


class SidebarList(_Canvas):
    H_PAD, H_DM, H_GUILD, H_CHAN, H_NOTE, H_LOADING = 6, 50, 46, 34, 70, 32

    def __init__(self, parent: Any, images: ImageStore, on_pick: Callable[[str, dict], None], bg: str = BG1,
                 on_need_channels: Optional[Callable[[str], None]] = None,
                 on_counts: Optional[Callable[[int, int], None]] = None):
        super().__init__(parent, width=S(100), height=S(100), bg=bg, highlightthickness=0, bd=0,
                         yscrollincrement=S(24))
        self.images, self.on_pick, self._bg = images, on_pick, bg
        self.on_need_channels, self.on_counts = on_need_channels, on_counts
        self._mode = "dm"
        self._dms: List[dict] = []
        self._guilds: List[dict] = []
        self._filter = ""
        self._expanded: set = set()
        self._rows: List[dict] = []
        self._ys: List[int] = []
        self._total = 0
        self._sel: Optional[Tuple[str, str]] = None
        self._hover: Optional[int] = None
        self._pending = False
        self.dm_note = "Connect to load your chats."
        self.server_note = "Connect to load your servers."
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", self._on_wheel)
        self.bind("<Button-5>", self._on_wheel)


    def set_data(self, dms: List[dict], guilds: List[dict]) -> None:
        self._dms, self._guilds = list(dms), list(guilds)
        self._rebuild()
        self.redraw()

    def set_notes(self, dm_note: str, server_note: str) -> None:
        self.dm_note, self.server_note = dm_note, server_note
        self._rebuild()
        self.redraw()

    def set_mode(self, mode: str) -> None:
        if mode in ("dm", "server") and mode != self._mode:
            self._mode = mode
            self._hover = None
            self._rebuild()
            self.yview_moveto(0)
            self.redraw()

    def set_filter(self, text: str) -> None:
        text = (text or "").strip().lower()
        if text != self._filter:
            self._filter = text
            self._rebuild()
            self.yview_moveto(0)
            self.redraw()

    def select(self, kind: Optional[str], ident: Optional[str]) -> None:
        self._sel = (kind, ident) if kind and ident else None
        self.redraw()

    def add_dm(self, dm: dict) -> None:
        if not any(d["id"] == dm["id"] for d in self._dms):
            self._dms.insert(0, dm)
            self._rebuild()
            self.yview_moveto(0)
        self.redraw()

    def set_guild_channels(self, gid: str, channels: List[dict]) -> None:
        for g in self._guilds:
            if g["id"] == gid:
                g["channels"] = channels
                break
        self._rebuild()
        self.redraw()


    def _guild_visible(self, g: dict, q: str) -> Tuple[bool, List[dict]]:
        chans = g.get("channels") or []
        if not q:
            return True, chans
        if q in g["name"].lower():
            return True, chans
        matched = [c for c in chans if q in c["label"].lower()]
        return bool(matched), matched

    def _rebuild(self) -> None:
        q = self._filter
        rows: List[dict] = [{"kind": "pad", "h": S(self.H_PAD)}]

        def add(kind: str, h: int, **data: Any) -> None:
            rows.append({"kind": kind, "h": S(h), **data})

        dm_hits = [d for d in self._dms if not q or q in d["label"].lower()]
        guild_hits = [(g, ch) for g in self._guilds for ok, ch in [self._guild_visible(g, q)] if ok]
        if self.on_counts:
            self.on_counts(len(dm_hits), len(guild_hits))

        if self._mode == "dm":
            if dm_hits:
                for d in dm_hits:
                    add("dm", self.H_DM, dm=d)
            else:
                add("note", self.H_NOTE, text=self.dm_note if not self._dms else "No chats match your search.")
        else:
            if not guild_hits:
                add("note", self.H_NOTE,
                    text=self.server_note if not self._guilds else "No servers match your search.")
            for g, visible in guild_hits:
                expanded = bool(q) or g["id"] in self._expanded
                add("guild", self.H_GUILD, guild=g, expanded=expanded, n=len(g.get("channels") or []))
                if not expanded:
                    continue
                if g.get("channels") is None:
                    add("loading", self.H_LOADING, text="Loading channels\u2026")
                elif not visible:
                    add("loading", self.H_LOADING, text="No text channels you can see")
                else:
                    for c in visible:
                        add("chan", self.H_CHAN, chan=c, guild=g)

        ys, y = [], 0
        for r in rows:
            ys.append(y)
            y += r["h"]
        self._rows, self._ys, self._total = rows, ys, y
        if self._hover is not None and self._hover >= len(rows):
            self._hover = None

    def row_at(self, widget_y: float) -> Optional[int]:
        cy = self.canvasy(widget_y)
        i = bisect_right(self._ys, cy) - 1
        if 0 <= i < len(self._rows) and cy < self._ys[i] + self._rows[i]["h"]:
            return i
        return None


    def _clickable(self, i: Optional[int]) -> bool:
        return i is not None and self._rows[i]["kind"] in ("dm", "guild", "chan")

    def _on_motion(self, e: Any) -> None:
        i = self.row_at(e.y)
        if i != self._hover:
            self._hover = i
            self.configure(cursor="hand2" if self._clickable(i) else "")
            self.redraw()

    def _on_leave(self, _e: Any = None) -> None:
        if self._hover is not None:
            self._hover = None
            self.redraw()

    def _on_wheel(self, e: Any) -> None:
        units = wheel_units(e)
        if units:
            self.yview_scroll(units, "units")
            self.redraw()

    def scrollbar_command(self, *args: Any) -> None:
        self.yview(*args)
        self.redraw()

    def _on_click(self, e: Any) -> None:
        i = self.row_at(e.y)
        if not self._clickable(i):
            return
        row = self._rows[i]
        kind = row["kind"]
        if kind == "guild":
            g = row["guild"]
            if not self._filter:
                self._expanded.symmetric_difference_update({g["id"]})
            if g.get("channels") is None and self.on_need_channels:
                self.on_need_channels(g["id"])
            self._rebuild()
            self.redraw()
        elif kind == "dm":
            self._sel = ("dm", row["dm"]["id"])
            self.redraw()
            self.on_pick("dm", row["dm"])
        else:
            self._sel = ("chan", row["chan"]["id"])
            self.redraw()
            self.on_pick("chan", {**row["chan"], "guild_id": row["guild"]["id"], "guild_name": row["guild"]["name"],
                                  "guild_icon": row["guild"].get("icon_url")})


    def redraw(self) -> None:
        if self._pending:
            return
        self._pending = True
        self.after_idle(self._draw)

    def _is_selected(self, row: dict) -> bool:
        if not self._sel:
            return False
        kind, ident = self._sel
        return (row["kind"] == "dm" and kind == "dm" and row["dm"]["id"] == ident) or \
               (row["kind"] == "chan" and kind == "chan" and row["chan"]["id"] == ident)

    def _draw(self) -> None:
        self._pending = False
        self.delete("all")
        w = max(self.winfo_width(), S(60))
        h = max(self.winfo_height(), 1)
        self.configure(scrollregion=(0, 0, w, max(self._total, h)))
        if not self._rows:
            return
        top = int(self.canvasy(0))
        bottom = top + h
        first = max(0, bisect_right(self._ys, top) - 1)
        wanted: List[str] = []
        for i in range(first, len(self._rows)):
            y = self._ys[i]
            if y > bottom:
                break
            self._paint(i, self._rows[i], y, w, wanted)
        if wanted:
            self.images.request(wanted)

    def _paint(self, i: int, row: dict, y: int, w: int, wanted: List[str]) -> None:
        kind, h = row["kind"], row["h"]
        pad, mid = S(8), y + h / 2
        if kind == "pad":
            return
        if kind == "note":
            self.create_text(w / 2, mid, text=row["text"], fill=FG_MUTED, font=fnt(10), justify="center",
                             width=max(S(60), w - S(56)))
            return
        if kind == "loading":
            self.create_text(S(58), mid, text=row["text"], anchor="w", fill=FG_MUTED, font=fnt(9))
            return
        selected = self._is_selected(row)
        if selected or i == self._hover:
            self.create_polygon(round_rect_points(pad, y + S(1), w - pad, y + h - S(1), S(9)), smooth=True,
                                fill=PURPLE_SOFT if selected else HOVER, outline="")
        if kind == "dm":
            dm = row["dm"]
            size = S(34)
            url = dm.get("avatar_url")
            if url and not self.images.has(url):
                wanted.append(url)
            initial = (dm["label"] or "?").strip()[:1].upper()
            photo = self.images.photo(url, size, initial, _hue(dm["id"]))
            self.create_image(S(18), mid, image=photo, anchor="w")
            x = S(18) + size + S(12)
            font = fnt(10, selected)
            self.create_text(x, mid, text=ellipsize(dm["label"], font, w - x - S(16)), anchor="w",
                             fill="#ffffff" if selected else FG, font=font)
        elif kind == "guild":
            g = row["guild"]
            self.create_text(S(16), mid, text="\u25BE" if row["expanded"] else "\u25B8", anchor="w",
                             fill=FG_MUTED, font=fnt(9))
            size = S(30)
            url = g.get("icon_url")
            if url and not self.images.has(url):
                wanted.append(url)
            initial = (g["name"] or "?").strip()[:1].upper()
            photo = self.images.photo(url, size, initial, _hue(g["id"]), "squircle")
            self.create_image(S(32), mid, image=photo, anchor="w")
            x = S(32) + size + S(12)
            font = fnt(10, True)
            right = S(16)
            if g.get("channels") is not None:
                self.create_text(w - S(16), mid, text=str(row["n"]), anchor="e", fill=FG_MUTED, font=fnt(9))
                right = S(44)
            self.create_text(x, mid, text=ellipsize(g["name"], font, w - x - right), anchor="w", fill=FG, font=font)
        elif kind == "chan":
            c = row["chan"]
            symbol, _, name = c["label"].partition(" ")
            name = name or symbol
            self.create_text(S(58), mid, text=symbol, anchor="w", fill=FG_MUTED, font=fnt(10))
            x = S(58) + S(24)
            font = fnt(10)
            self.create_text(x, mid, text=ellipsize(name, font, w - x - S(14)), anchor="w",
                             fill="#ffffff" if selected else FG_DIM, font=font)


def _hue(key: Optional[str]) -> str:
    import colorsys
    hue = (int(key) % 360) if key and str(key).isdigit() else 200
    r, g, b = colorsys.hsv_to_rgb(hue / 360, 0.6, 0.9)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def center_over(parent: Any, win: Any, width: int, height: int) -> None:
    try:
        parent.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
    except Exception:
        win.geometry(f"{width}x{height}")


def ask_dialog(parent: Any, title: str, message: str, buttons: Sequence[Tuple[str, str, str]],
               width: int = 430) -> str:
    result = {"v": ""}
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=BG1)
    win.transient(parent)
    win.resizable(False, False)
    tk.Label(win, text=title, bg=BG1, fg=FG, font=fnt(13, True), anchor="w").pack(
        fill="x", padx=S(22), pady=(S(20), S(6)))
    tk.Label(win, text=message, bg=BG1, fg=FG_DIM, font=fnt(10), wraplength=S(width) - S(44),
             justify="left", anchor="w").pack(fill="x", padx=S(22))
    row = tk.Frame(win, bg=BG1)
    row.pack(fill="x", padx=S(22), pady=S(20))

    def choose(v: str) -> None:
        result["v"] = v
        win.destroy()

    for value, label, variant in reversed(list(buttons)):
        Button(row, label, command=lambda v=value: choose(v), variant=variant, height=34, bg=BG1).pack(
            side="right", padx=(S(8), 0))
    win.bind("<Escape>", lambda _e: choose(""))
    win.protocol("WM_DELETE_WINDOW", lambda: choose(""))
    win.update_idletasks()
    center_over(parent, win, S(width), win.winfo_reqheight())
    apply_windows_dark_titlebar(win)
    win.grab_set()
    win.focus_set()
    parent.wait_window(win)
    return result["v"]
