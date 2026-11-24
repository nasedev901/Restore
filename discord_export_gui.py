#!/usr/bin/env python3
from __future__ import annotations

import colorsys
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

import discord_export as dx
import restore_ui as ui
from restore_ui import (BG0, BG1, BORDER, FG, FG_DIM, FG_MUTED, GREEN, HOVER, INPUT, MONO, PURPLE, PURPLE_DIM,
                        PURPLE_HI, PURPLE_SOFT, RED, YELLOW, S, fnt)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    _TK_OK = True
except ImportError:
    _TK_OK = False


def _dm_label(c: dict) -> str:
    if c.get("type") == dx.CH_DM:
        recips = c.get("recipients") or []
        if recips:
            return dx.user_display(recips[0])
    return dx.channel_label(c)


def _full_username(u: Optional[dict]) -> str:
    if not u:
        return "Unknown"
    name = u.get("username") or u.get("global_name") or "Unknown"
    disc = u.get("discriminator")
    if disc and disc not in ("0", "0000"):
        return f"{name}#{disc}"
    return name


_CHANNEL_TYPE_PREFIX = {
    dx.CH_TEXT: "#", dx.CH_ANNOUNCE: "\U0001F4E2",
    dx.CH_VOICE: "\U0001F50A", dx.CH_STAGE: "\U0001F399",
    dx.CH_FORUM: "\U0001F9F5", dx.CH_MEDIA: "\U0001F5BC",
}


def _channel_label(c: dict) -> str:
    prefix = _CHANNEL_TYPE_PREFIX.get(c.get("type"), "#")
    return f"{prefix} {c.get('name') or c['id']}"


def _guild_icon_url(g: dict, size: int = 64) -> Optional[str]:
    icon = g.get("icon")
    if not icon or not g.get("id"):
        return None
    ext = "gif" if str(icon).startswith("a_") else "png"
    return f"{dx.CDN}/icons/{g['id']}/{icon}.{ext}?size={size}"


def _format_channels(chans: List[dict]) -> List[dict]:
    return [{"id": c["id"], "label": _channel_label(c), "type": c.get("type")} for c in chans]


def connect_account(token: str, api_base: Optional[str] = None, bot: bool = False) -> Tuple["dx.DiscordAPI", Dict[str, Any]]:
    api = dx.DiscordAPI(token, bot=bot, base=api_base or dx.API_BASE)
    with ThreadPoolExecutor(max_workers=3) as ex:
        me_f, dms_f, guilds_f = ex.submit(api.me), ex.submit(api.dm_channels), ex.submit(api.guilds)
        me = me_f.result()
        dms = sorted(dms_f.result(), key=lambda c: int(c.get("last_message_id") or 0), reverse=True)
        guild_list = guilds_f.result()
    return api, {
        "me": _full_username(me),
        "me_id": me.get("id"),
        "me_avatar_url": dx.avatar_url(me, size=64),
        "dms": [{"id": c["id"], "label": _dm_label(c),
                "avatar_url": dx.avatar_url(c["recipients"][0], size=64) if c.get("recipients") else None}
               for c in dms],
        "guilds": [{"id": g["id"], "name": g.get("name") or g["id"], "icon_url": _guild_icon_url(g),
                   "channels": None} for g in guild_list],
    }


def load_guild_channels(api: "dx.DiscordAPI", guild_id: str) -> List[dict]:
    try:
        return _format_channels(dx.guild_text_channels(api, guild_id))
    except dx.ApiError:
        return []


def fetch_channel_tree(token: str, api_base: Optional[str] = None, bot: bool = False) -> Dict[str, Any]:
    api, overview = connect_account(token, api_base, bot)
    if overview["guilds"]:
        with ThreadPoolExecutor(max_workers=min(8, len(overview["guilds"]))) as ex:
            loaded = list(ex.map(lambda g: load_guild_channels(api, g["id"]), overview["guilds"]))
        for g, chans in zip(overview["guilds"], loaded):
            g["channels"] = chans
    return overview


def fetch_avatar_bytes(url: Optional[str], timeout: float = 6.0,
                       session: Optional[requests.Session] = None) -> Optional[bytes]:
    if not url:
        return None
    s = session or requests.Session()
    try:
        r = s.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok and r.content:
            return r.content
    except requests.RequestException:
        pass
    finally:
        if session is None:
            s.close()
    return None


def fetch_avatars_concurrent(urls: Dict[str, Optional[str]], workers: int = 16) -> Dict[str, bytes]:
    items = [(k, u) for k, u in urls.items() if u]
    results: Dict[str, bytes] = {}
    if not items:
        return results
    n = min(workers, len(items))
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=n, pool_maxsize=n)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            future_to_id = {ex.submit(fetch_avatar_bytes, u, 6.0, session): k for k, u in items}
            for fut in future_to_id:
                b = fut.result()
                if b:
                    results[future_to_id[fut]] = b
    finally:
        session.close()
    return results


def fetch_preview(token: str, channel_id: str, guild_id: Optional[str] = None, limit: int = 50,
                  api_base: Optional[str] = None, bot: bool = False, api: Optional["dx.DiscordAPI"] = None,
                  guild_cache: Optional[Dict[str, Tuple[List[dict], List[dict]]]] = None) -> List[dict]:
    api = api or dx.DiscordAPI(token, bot=bot, base=api_base or dx.API_BASE)
    res = dx.Resolver(None)

    def _roles() -> List[dict]:
        try:
            return api.guild_roles(guild_id)
        except dx.ApiError:
            return []

    def _channels() -> List[dict]:
        try:
            return api.guild_channels(guild_id)
        except dx.ApiError:
            return []


    if guild_id and guild_cache is not None and guild_id in guild_cache:
        roles, chans = guild_cache[guild_id]
        for role in roles:
            res.roles[role["id"]] = role.get("name")
        for c in chans:
            res.channels[c["id"]] = c.get("name")
        raw = api.messages(channel_id, limit=limit)
    elif guild_id:
        with ThreadPoolExecutor(max_workers=3) as ex:
            roles_future = ex.submit(_roles)
            channels_future = ex.submit(_channels)
            messages_future = ex.submit(api.messages, channel_id, limit=limit)
            roles, chans = roles_future.result(), channels_future.result()
            raw = messages_future.result()
        for role in roles:
            res.roles[role["id"]] = role.get("name")
        for c in chans:
            res.channels[c["id"]] = c.get("name")
        if guild_cache is not None and (roles or chans):
            guild_cache[guild_id] = (roles, chans)
    else:
        raw = api.messages(channel_id, limit=limit)
    raw.sort(key=lambda m: int(m["id"]))
    index = {m["id"]: m for m in raw}
    for m in raw:
        res.feed(m)
    ctx = dx.CleanContext(res=res, tz=None, root=Path("__restore_preview__"), index=index)
    return [dx.clean_message(m, ctx) for m in raw]


_DATE_INPUT_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d")
_RE_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_RE_RELATIVE = re.compile(r"^\d+(\.\d+)?\s*[smhdw]$", re.I)


_RE_EXPORT_PROGRESS = re.compile(r"\[[#\-]+\]\s*(\d{1,3})%")


_RE_PROGRESS_LINE = re.compile(
    r"^\s*[A-Za-z][A-Za-z ]{0,24}?\s+(?:\[[#\-]+\]\s+\d{1,3}%\s+)?[\d,]+(?:\s+of\s+[\d,]+)?\s+(?:msgs|files)\b")
_RE_PROGRESS_DETAIL = re.compile(
    r"^\s*(?P<label>[A-Za-z][A-Za-z ]*?)\s+(?:\[[#\-]+\]\s+(?P<pct>\d{1,3})%\s+)?(?P<done>[\d,]+)"
    r"(?:\s+of\s+(?P<total>[\d,]+))?\s+(?P<unit>msgs|files)\b(?P<rest>.*)$")
_RE_ETA = re.compile(r"\bETA\s+(\d+(?::\d{2}){1,2})")


PHASES = (("Fetching", "Messages"), ("Reaction users", "Reactions"), ("Media", "Media"))
_PHASE_WEIGHT = {"Fetching": 0.6, "Reaction users": 0.15, "Media": 0.4}


def active_phases(reactors: bool, media: str) -> List[str]:
    out = ["Fetching"]
    if reactors:
        out.append("Reaction users")
    if media != "none":
        out.append("Media")
    return out


def overall_percent(phases: List[str], current: str, pct: Optional[float]) -> float:
    if current not in phases:
        return 0.0
    total = sum(_PHASE_WEIGHT[p] for p in phases)
    before = sum(_PHASE_WEIGHT[p] for p in phases[:phases.index(current)])
    within = _PHASE_WEIGHT[current] * max(0.0, min(100.0, pct or 0.0)) / 100.0
    return 100.0 * (before + within) / total


def parse_progress_detail(line: str) -> Optional[Dict[str, Any]]:
    m = _RE_PROGRESS_DETAIL.match(line)
    if not m:
        return None
    rest = m.group("rest")
    eta = _RE_ETA.search(rest)
    rate = re.search(r"([\d,]+/s)", rest)
    num = lambda x: int(x.replace(",", "")) if x else None
    return {"label": m.group("label").strip(), "pct": min(100, int(m.group("pct"))) if m.group("pct") else None,
            "done": num(m.group("done")), "total": num(m.group("total")), "unit": m.group("unit"),
            "eta": eta.group(1) if eta else None, "rate": rate.group(1) if rate else None}


def parse_progress_line(line: str) -> Optional[Tuple[Optional[int], str]]:
    if not _RE_PROGRESS_LINE.match(line):
        return None
    m = _RE_EXPORT_PROGRESS.search(line)
    pct = min(100, int(m.group(1))) if m else None
    text = re.sub(r"\s*\[[#\-]+\]\s*", " ", line).strip()
    return pct, " \u00b7 ".join(p for p in re.split(r"\s{2,}", text) if p)


def normalize_date_field(text: str) -> str:
    t = text.strip()
    if not t or (t.isdigit() and len(t) >= 15) or t.lower() == "now" or _RE_RELATIVE.match(t) or _RE_ISO_DATE.match(t):
        return t
    for fmt in _DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return t


EXPORT_GREEN = "#22c55e"
EXPORT_GREEN_DIM = "#16a34a"

def build_export_argv(v: Dict[str, Any]) -> List[str]:
    argv = ["-c", str(v["channel_id"]), "-o", str(v["out_dir"]),
            "-f", ",".join(v.get("formats") or ["json", "html"]),
            "--media", v.get("media") or "attachments", "--tz", v.get("tz") or "local"]
    if v.get("limit"):
        argv += ["-n", str(int(v["limit"]))]
    if v.get("after"):
        argv += ["--after", str(v["after"])]
    if v.get("before"):
        argv += ["--before", str(v["before"])]
    if v.get("reactors"):
        argv.append("--reactors")
    if v.get("fresh"):
        argv.append("--fresh")
    if v.get("delay") is not None:
        argv += ["--delay", str(v["delay"])]
    if v.get("api_base"):
        argv += ["--api-base", str(v["api_base"])]
    return argv


ZOOM_MIN, ZOOM_MAX = -4, 8
ZOOM_FLOOR_PT = 7


def clamp_zoom(level: int) -> int:
    return max(ZOOM_MIN, min(ZOOM_MAX, level))


def zoomed_pt(base_pt: int, zoom_level: int) -> int:
    return max(ZOOM_FLOOR_PT, base_pt + zoom_level)


def zoom_percent(zoom_level: int) -> int:
    return 100 + zoom_level * 10


class _LineWriter:
    def __init__(self, on_line: Callable[[str], None]):
        self.on_line = on_line
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf or "\r" in self._buf:
            cut = min((i for i in (self._buf.find("\n"), self._buf.find("\r")) if i != -1), default=-1)
            if cut == -1:
                break
            line, self._buf = self._buf[:cut], self._buf[cut + 1:]
            if line:
                self.on_line(line)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def run_export_job(argv: List[str], token: str, on_line: Callable[[str], None]) -> int:
    os.environ["DISCORD_TOKEN"] = token
    writer = _LineWriter(on_line)
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = writer
    try:
        try:
            return dx.main(argv)
        except SystemExit as e:
            if isinstance(e.code, str):
                on_line(e.code)
                return 1
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except KeyboardInterrupt:
        on_line("Cancelled. Progress so far is saved; run the same export again to resume.")
        return 130
    except Exception:
        on_line(traceback.format_exc())
        return 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        os.environ.pop("DISCORD_TOKEN", None)


def open_in_file_manager(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def notify_export_finished(ok: bool, chat_name: str = "") -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
        return True
    except Exception:
        return False


def raise_in_thread(thread_id: Optional[int], exc_type: type) -> bool:
    if thread_id is None:
        return False
    set_exc = ctypes.pythonapi.PyThreadState_SetAsyncExc
    n = set_exc(ctypes.c_ulong(thread_id), ctypes.py_object(exc_type))
    if n > 1:
        set_exc(ctypes.c_ulong(thread_id), None)
        return False
    return n == 1


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
    return base / name


def _hue_to_hex(key: Optional[str], s: float = 0.6, v: float = 0.9) -> str:
    hue = (int(key) % 360) if key and str(key).isdigit() else 200
    red, green, blue = colorsys.hsv_to_rgb(hue / 360, s, v)
    return "#%02x%02x%02x" % (round(red * 255), round(green * 255), round(blue * 255))


EXPORT_GREEN = GREEN


_RE_RICH = re.compile(r"(https?://[^\s<>]+)|(?<![\w])(@[\w.\-]+|#[\w\-]+)")
_URL_TRAILING = ".,;:!?)]}'\""


def split_rich(text: str) -> List[Tuple[str, Optional[str]]]:
    out: List[Tuple[str, Optional[str]]] = []
    pos = 0
    for m in _RE_RICH.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], None))
        seg = m.group(0)
        if m.group(1):
            url = seg.rstrip(_URL_TRAILING)
            out.append((url, "link"))
            if len(url) < len(seg):
                out.append((seg[len(url):], None))
        else:
            out.append((seg, "mention"))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], None))
    return out


TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/"
_RE_EMOJI = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2}"
    "|(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u231A\u231B\u23E9-\u23F3\u23F8-\u23FA]"
    "[\U0001F3FB-\U0001F3FF]?\uFE0F?"
    "(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55][\U0001F3FB-\U0001F3FF]?\uFE0F?)*))")


def twemoji_name(seq: str) -> str:
    cps = [ord(c) for c in seq]
    if 0x200D not in cps:
        cps = [c for c in cps if c != 0xFE0F]
    return "-".join(f"{c:x}" for c in cps)


def twemoji_url(seq: str) -> str:
    return f"{TWEMOJI_BASE}{twemoji_name(seq)}.png"


def reaction_emoji_url(e: dict) -> Optional[str]:
    if e.get("id"):
        return dx.emoji_url(e["id"], bool(e.get("animated")), size=48)
    return twemoji_url(e["name"]) if e.get("name") else None


def split_emoji(text: str) -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    pos = 0
    for m in _RE_EMOJI.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def msg_minutes(local_time: str) -> Optional[int]:
    try:
        d = datetime.strptime(local_time[:10] + " " + local_time[11:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    return d.toordinal() * 1440 + d.hour * 60 + d.minute


def pretty_day(local_time: str) -> str:
    try:
        d = datetime.strptime(local_time[:10], "%Y-%m-%d")
        return f"{d.strftime('%A, %B')} {d.day}, {d.year}"
    except (ValueError, TypeError):
        return local_time[:10]


TOKEN_HELP_URL = "https://youtu.be/dBjdGnwBHBY?si=bEeHm7nmdT3Gepvk"

_RE_TOKENISH = re.compile(r"[A-Za-z0-9._\-]{50,}")


def looks_like_token(text: str) -> bool:
    return bool(_RE_TOKENISH.fullmatch((text or "").strip()))


FORMAT_FILES = {"json": "chat.json", "html": "chat.html", "jsonl": "chat.jsonl", "csv": "chat.csv", "txt": "chat.txt"}
MEDIA_CAPTIONS = {"none": "Text only", "attachments": "Images and files", "all": "Everything, incl. emoji"}


GROUP_WINDOW_MINUTES = 7


_TkFrameBase = tk.Frame if _TK_OK else object


class App(_TkFrameBase):
    FORMAT_OPTIONS = [("json", "JSON"), ("html", "HTML"), ("jsonl", "JSONL"), ("csv", "CSV"), ("txt", "TXT")]
    MEDIA_OPTIONS = [("none", "None"), ("attachments", "Attachments"), ("all", "Everything")]
    NO_CHAT_LABEL = "Select a chat"
    PREVIEW_LIMIT = 50
    PREVIEW_CACHE_MAX = 25
    LOG_MAX_LINES = 1500
    SIDEBAR_W = 292
    EXPORT_W = 340
    AVATAR_PX = 36

    def __init__(self, master: "tk.Tk"):
        super().__init__(master, bg=BG0)
        self.master = master
        master.title("Restore")
        master.configure(bg=BG0)
        self._size_window(master)
        master.protocol("WM_DELETE_WINDOW", self._on_close)
        master.report_callback_exception = self._on_tk_error
        self._set_icon(master)
        ui.apply_windows_dark_titlebar(master)

        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.images = ui.ImageStore(fetch_avatars_concurrent, lambda got: self.queue.put(("images", got)))
        self._token: Optional[str] = None
        self._api: Any = None
        self._guild_meta: Dict[str, Tuple[List[dict], List[dict]]] = {}
        self._chan_pool: Any = None
        self._chan_state: set = set()
        self._chan_done: set = set()
        self._autoconnect_job: Any = None
        self._failed_token: Optional[str] = None
        self._status_state = "idle"
        self._me_spec: Optional[Tuple] = None
        self._me_photo: Any = None
        self._chat_subtitle = ""
        self._slots: List[dict] = []
        self._slot_photos: Dict[str, Any] = {}
        self._seps: List[Tuple[Any, str]] = []
        self._chip_cache: Dict[Tuple, Any] = {}
        self._phases: List[str] = []
        self._overall_max = 0.0
        self._connecting = False
        self._conn_gen = 0
        self._preview_gen = 0
        self._preview_cache: "OrderedDict[str, List[dict]]" = OrderedDict()
        self._preview_msgs: Optional[List[dict]] = None
        self._preview_urls: List[str] = []
        self._preview_photos: List[Any] = []
        self._author_tags: set = set()
        self._current: Dict[str, Optional[str]] = {"id": None, "guild_id": None}
        self._chat_summary = ""
        self._header_spec: Optional[Tuple] = None
        self._header_photo: Any = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._cancelling = False
        self._last_out_dir = ""
        self._export_chat_label = ""
        self._busy: set = set()
        self._zoom = 0
        self._fullscreen = False
        self._toast_job: Any = None
        self._in_tk_error = False
        self._orig_streams = (sys.stdout, sys.stderr)

        self._build()
        self._apply_zoom()
        self._bind_shortcuts()
        self.after(60, self._poll)


    def _size_window(self, master: "tk.Tk") -> None:
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        w, h = min(S(1240), sw - S(40)), min(S(820), sh - S(90))
        master.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        master.minsize(min(S(1040), w), min(S(700), h))

    def _set_icon(self, master: "tk.Tk") -> None:
        try:
            path = resource_path("Restore.png")
            if path.exists():
                self._icon_img = tk.PhotoImage(file=str(path))
                master.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _on_tk_error(self, exc: Any, val: Any, tb: Any) -> None:
        if self._in_tk_error:
            return
        self._in_tk_error = True
        try:
            text = "".join(traceback.format_exception(exc, val, tb))
            self._append_log_lines(["[internal error]"] + text.rstrip().split("\n"))
            self._toast("Something went wrong - details are in the log below.", "error")
        except Exception:
            pass
        finally:
            self._in_tk_error = False


    def _build(self) -> None:
        self.pack(fill="both", expand=True)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._build_topbar()
        self.top_progress = ui.ProgressBar(self, height=2, bg=BG0, trough=BG0, color=PURPLE, rounded=False)
        self.top_progress.grid(row=1, column=0, sticky="ew")
        body = tk.Frame(self, bg=BG0)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(2, weight=1)
        self._build_sidebar(body).grid(row=0, column=0, sticky="ns")
        tk.Frame(body, bg=BORDER, width=1).grid(row=0, column=1, sticky="ns")
        self._build_center(body).grid(row=0, column=2, sticky="nsew")
        tk.Frame(body, bg=BORDER, width=1).grid(row=0, column=3, sticky="ns")
        self._build_export_panel(body).grid(row=0, column=4, sticky="ns")
        self._toast_label = tk.Label(self, text="", bg=INPUT, fg=FG, font=fnt(10), padx=S(16), pady=S(10),
                                     highlightthickness=1, highlightbackground=BORDER)

    def _build_topbar(self) -> None:
        top = tk.Frame(self, bg=BG1)
        top.grid(row=0, column=0, sticky="ew")
        bar = tk.Frame(top, bg=BG1)
        bar.pack(fill="x", padx=S(18), pady=S(12))

        help_btn = ui.Button(bar, "?", command=self._show_help, variant="secondary", width=36, height=36, bg=BG1)
        help_btn.pack(side="right")
        ui.Tooltip(help_btn, "How does this work?")

        chip = tk.Frame(bar, bg=BG1)
        chip.pack(side="right", padx=(S(16), S(14)))
        self.me_avatar = tk.Label(chip, bg=BG1, bd=0)
        self.status_dot = tk.Canvas(chip, width=S(10), height=S(10), bg=BG1, highlightthickness=0, bd=0)
        self._dot_item = self.status_dot.create_oval(1, 1, S(10) - 1, S(10) - 1, fill=FG_MUTED, outline="")
        self.conn_status = tk.Label(chip, text="Not connected", bg=BG1, fg=FG_DIM, font=fnt(10), anchor="w")
        self.conn_status.pack(side="left")
        self.status_dot.pack(side="left", padx=(0, S(8)), before=self.conn_status)

        self.connect_btn = ui.Button(bar, "Connect", command=self._on_connect, variant="primary", width=108,
                                     height=36, bg=BG1)
        self.connect_btn.pack(side="right")
        ui.Tooltip(self.connect_btn, "Log in with the token and load your DMs and servers. "
                                     "This also happens automatically when you paste a token.")
        self.paste_btn = ui.Button(bar, "Paste", command=self._paste_token, variant="secondary", width=64,
                                   height=36, bg=BG1)
        self.paste_btn.pack(side="right", padx=(S(8), S(8)))
        ui.Tooltip(self.paste_btn, "Paste the token from your clipboard and connect.")
        self.help_token_btn = ui.Button(bar, "How to get token", command=self._open_token_help,
                                        variant="secondary", width=128, height=36, bg=BG1)
        self.help_token_btn.pack(side="right")
        ui.Tooltip(self.help_token_btn, "Opens a short video in your browser showing how to find your token "
                                        "using Chrome's developer tools - no scripts or console commands needed.")
        self.show_btn = ui.Button(bar, "Show", command=self._toggle_show, variant="secondary", width=60,
                                  height=36, bg=BG1)
        self.show_btn.pack(side="right", padx=(0, S(8)))
        self._token_hidden = True

        self.token_var = tk.StringVar()
        self.token_field = ui.Field(bar, self.token_var, placeholder="Paste your Discord token", show="*", width=10)
        self.token_field.pack(side="left", fill="x", expand=True, padx=(0, S(10)))
        self.token_field.entry.bind("<Return>", lambda _e: self._on_connect())
        self.token_var.trace_add("write", lambda *_a: self._on_token_edited())
        ui.Tooltip(self.token_field, "Your own Discord user token. It's only ever sent to Discord's API "
                                     "and is never written to disk. It connects by itself when pasted.")

    def _build_sidebar(self, parent: Any) -> Any:
        side = tk.Frame(parent, bg=BG1, width=S(self.SIDEBAR_W))
        side.pack_propagate(False)

        tabs = tk.Frame(side, bg=BG1)
        tabs.pack(side="top", fill="x", padx=S(12), pady=(S(14), S(8)))
        self.tab_chips = ui.ChipGroup(tabs, [("dm", "DMs"), ("server", "Servers")], "dm", bg=BG1, height=34, size=10,
                                      on_change=lambda: self.sidebar.set_mode(self.tab_chips.get()))
        self.tab_chips.pack(fill="x")
        top = tk.Frame(side, bg=BG1)
        top.pack(side="top", fill="x", padx=S(12), pady=(0, S(6)))
        self.search_var = tk.StringVar()
        self.search_field = ui.Field(top, self.search_var, placeholder="Search     Ctrl+F")
        self.search_field.pack(fill="x")

        add = tk.Frame(side, bg=BG1)
        add.pack(side="bottom", fill="x", padx=S(12), pady=S(12))
        tk.Frame(side, bg=BORDER, height=1).pack(side="bottom", fill="x")
        self.manual_id_var = tk.StringVar()
        self.manual_field = ui.Field(add, self.manual_id_var, placeholder="Add a chat by channel ID", width=10)
        add_btn = ui.Button(add, "Add", command=self._on_add_manual_chat, variant="secondary", width=54,
                            height=34, bg=BG1)
        add_btn.pack(side="right", padx=(S(8), 0))
        self.manual_field.pack(side="left", fill="x", expand=True)
        self.manual_field.entry.bind("<Return>", lambda _e: self._on_add_manual_chat())
        ui.Tooltip(add_btn, "Discord's API only lists DMs that are open in your client, so a closed or "
                            "archived one won't appear on its own. Paste its channel ID here instead "
                            "(Developer Mode on, right-click the chat, Copy Channel ID).")

        holder = tk.Frame(side, bg=BG1)
        holder.pack(side="top", fill="both", expand=True)
        self.sidebar = ui.SidebarList(holder, self.images, self._on_sidebar_pick, bg=BG1,
                                      on_need_channels=self._request_channels_now, on_counts=self._on_counts)
        sb = ui.SlimScrollbar(holder, command=self.sidebar.scrollbar_command, bg=BG1)
        self.sidebar.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, S(2)))
        self.sidebar.pack(side="left", fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_a: self.sidebar.set_filter(self.search_var.get()))
        return side

    def _on_counts(self, dm_n: int, server_n: int) -> None:
        loaded = bool(self.sidebar._dms or self.sidebar._guilds)
        self.tab_chips.set_label("dm", f"DMs  {dm_n}" if loaded else "DMs")
        self.tab_chips.set_label("server", f"Servers  {server_n}" if loaded else "Servers")
        if self.search_var.get().strip():
            mode = self.tab_chips.get()
            if mode == "dm" and dm_n == 0 and server_n > 0:
                self.after_idle(lambda: self.tab_chips.select("server"))
            elif mode == "server" and server_n == 0 and dm_n > 0:
                self.after_idle(lambda: self.tab_chips.select("dm"))

    def _build_center(self, parent: Any) -> Any:
        center = tk.Frame(parent, bg=BG0)

        header = tk.Frame(center, bg=BG0)
        header.pack(side="top", fill="x", padx=S(20), pady=(S(14), S(12)))
        self.header_avatar = tk.Label(header, bg=BG0, bd=0)
        self.header_avatar.pack(side="left", padx=(0, S(12)))
        titles = tk.Frame(header, bg=BG0)
        titles.pack(side="left", fill="x", expand=True)
        self.chat_title_var = tk.StringVar(value=self.NO_CHAT_LABEL)
        self.chat_sub_var = tk.StringVar(value="Your messages will preview here")
        tk.Label(titles, textvariable=self.chat_title_var, bg=BG0, fg=FG, font=fnt(14, True),
                 anchor="w").pack(fill="x")
        tk.Label(titles, textvariable=self.chat_sub_var, bg=BG0, fg=FG_MUTED, font=fnt(9), anchor="w").pack(fill="x")

        refresh = ui.Button(header, "Refresh", command=self._on_refresh_preview, variant="secondary", width=76,
                            height=32, size=9, bg=BG0)
        refresh.pack(side="right")
        ui.Tooltip(refresh, "Reload this chat's preview from Discord.")
        zoom = tk.Frame(header, bg=BG0)
        zoom.pack(side="right", padx=(0, S(12)))
        ui.Button(zoom, "\u2212", command=lambda: self._adjust_zoom(-1), variant="ghost", width=30, height=30,
                  size=11, bg=BG0).pack(side="left")
        self.zoom_var = tk.StringVar(value="100%")
        tk.Label(zoom, textvariable=self.zoom_var, bg=BG0, fg=FG_MUTED, font=fnt(9), width=5).pack(side="left")
        ui.Button(zoom, "+", command=lambda: self._adjust_zoom(1), variant="ghost", width=30, height=30,
                  size=11, bg=BG0).pack(side="left")
        ui.Tooltip(zoom, "Make the preview text bigger or smaller.\nShortcuts: Ctrl +, Ctrl -, Ctrl 0 to reset.")

        tk.Frame(center, bg=BORDER, height=1).pack(side="top", fill="x")


        drawer = tk.Frame(center, bg=BG1)
        drawer.pack(side="bottom", fill="x")
        tk.Frame(drawer, bg=BORDER, height=1).pack(side="top", fill="x")
        bar = tk.Frame(drawer, bg=BG1, cursor="hand2")
        bar.pack(side="top", fill="x")
        self._log_open = False
        self.log_chevron = tk.Label(bar, text="\u25B8", bg=BG1, fg=FG_MUTED, font=fnt(9), cursor="hand2")
        self.log_chevron.pack(side="left", padx=(S(18), S(6)), pady=S(8))
        log_title = tk.Label(bar, text="ACTIVITY LOG", bg=BG1, fg=PURPLE, font=fnt(8, True), cursor="hand2")
        log_title.pack(side="left")
        self.log_summary_var = tk.StringVar(value="")
        log_sum = tk.Label(bar, textvariable=self.log_summary_var, bg=BG1, fg=FG_MUTED, font=fnt(9), anchor="w",
                           cursor="hand2")
        log_sum.pack(side="left", fill="x", expand=True, padx=(S(12), S(18)))
        for w in (bar, self.log_chevron, log_title, log_sum):
            w.bind("<Button-1>", lambda _e: self._toggle_log())
        self.log_body = tk.Frame(drawer, bg=BG0)
        self.log = tk.Text(self.log_body, font=(MONO, 9), state="disabled", wrap="word", bg=BG0, fg=FG_DIM,
                           relief="flat", bd=0, highlightthickness=0, width=10, height=9, padx=S(18), pady=S(8),
                           selectbackground=PURPLE_DIM)
        lsb = ui.SlimScrollbar(self.log_body, command=self.log.yview, bg=BG0)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        pframe = tk.Frame(center, bg=BG0)
        pframe.pack(side="top", fill="both", expand=True)
        self.preview = tk.Text(pframe, bg=BG0, fg=FG, relief="flat", bd=0, highlightthickness=0, wrap="word",
                               state="disabled", padx=0, pady=S(6), cursor="arrow", font=fnt(10), takefocus=0,
                               width=10, height=5,
                               selectbackground=PURPLE_DIM, inactiveselectbackground=PURPLE_DIM)
        psb = ui.SlimScrollbar(pframe, command=self.preview.yview, bg=BG0)
        self.preview.configure(yscrollcommand=psb.set)
        psb.pack(side="right", fill="y", padx=(0, S(2)))
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.bind("<Control-MouseWheel>", self._on_preview_ctrl_wheel)
        self.preview.bind("<Configure>", self._on_preview_resize, add="+")
        self._configure_preview_tags()
        self._build_empty_state(pframe)
        return center

    def _build_empty_state(self, parent: Any) -> None:
        es = tk.Frame(parent, bg=BG0)
        self.empty_state = es
        tk.Label(es, text="Select a chat", bg=BG0, fg=FG, font=fnt(16, True)).pack()
        tk.Label(es, text="Choose a DM or a server channel on the left to preview it here.\n"
                          "Previews are read-only - nothing is ever sent to Discord.", bg=BG0, fg=FG_MUTED,
                 font=fnt(10), justify="center").pack(pady=(S(8), 0))
        es.place(relx=0.5, rely=0.45, anchor="center")

    def _section(self, parent: Any, text: str, caption_var: Optional[Any] = None, first: bool = False) -> None:
        row = tk.Frame(parent, bg=BG1)
        row.pack(fill="x", pady=(0 if first else S(13), S(5)))
        tk.Label(row, text=text, bg=BG1, fg=PURPLE, font=fnt(8, True), anchor="w").pack(side="left")
        if caption_var is not None:
            tk.Label(row, textvariable=caption_var, bg=BG1, fg=FG_MUTED, font=fnt(8), anchor="e").pack(side="right")

    def _build_export_panel(self, parent: Any) -> Any:
        panel = tk.Frame(parent, bg=BG1, width=S(self.EXPORT_W))
        panel.pack_propagate(False)
        inner = tk.Frame(panel, bg=BG1)
        inner.pack(fill="both", expand=True, padx=S(18), pady=S(16))


        bottom = tk.Frame(inner, bg=BG1)
        bottom.pack(side="bottom", fill="x")
        self.start_btn = ui.Button(bottom, "Start export", command=self._on_start_or_cancel, variant="muted",
                                   height=44, size=11, radius=12, bg=BG1)
        self.start_btn.pack(fill="x")
        ui.Tooltip(self.start_btn, "Exports the chat into the folder above. Running it again later only "
                                   "fetches what's new, unless \u201cRe-download everything\u201d is on.")
        prow = tk.Frame(bottom, bg=BG1)
        prow.pack(fill="x", pady=(S(14), S(6)))
        self.pct_var = tk.StringVar(value="")
        tk.Label(prow, textvariable=self.pct_var, bg=BG1, fg=FG, font=fnt(9, True), width=5, anchor="e").pack(
            side="right")
        self.export_bar = ui.ProgressBar(prow, height=6, bg=BG1, trough=INPUT, color=GREEN)
        self.export_bar.pack(side="left", fill="x", expand=True)
        self.export_status_var = tk.StringVar(value="Pick a chat, choose a folder, then start.")
        self.export_status = tk.Label(bottom, textvariable=self.export_status_var, bg=BG1, fg=FG_DIM,
                                      font=fnt(9), anchor="w", justify="left", wraplength=S(self.EXPORT_W) - S(40))
        self.export_status.pack(fill="x")
        self.open_btn = ui.Button(bottom, "Open output folder", command=self._open_folder, variant="secondary",
                                  height=34, size=9, bg=BG1)


        card = tk.Frame(inner, bg=INPUT, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")
        self.channel_var = tk.StringVar()
        self.export_title_var = tk.StringVar(value="No chat selected")
        self.export_sub_var = tk.StringVar(value="Pick one on the left")
        tk.Label(card, textvariable=self.export_title_var, bg=INPUT, fg=FG, font=fnt(11, True), anchor="w").pack(
            fill="x", padx=S(14), pady=(S(10), 0))
        tk.Label(card, textvariable=self.export_sub_var, bg=INPUT, fg=FG_MUTED, font=fnt(9), anchor="w").pack(
            fill="x", padx=S(14), pady=(0, S(10)))

        self._section(inner, "RANGE")
        rng = tk.Frame(inner, bg=BG1)
        rng.pack(fill="x")
        self.limit_var, self.after_var, self.before_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for i, (label, var, ph, tip) in enumerate((
                ("Latest N", self.limit_var, "all", "Only the most recent N messages. Blank = the whole history."),
                ("After", self.after_var, "7/10/2026", "Only messages after this date (also accepts 7d, 12h or a message ID)."),
                ("Before", self.before_var, "now", "Only messages before this date."))):
            col = tk.Frame(rng, bg=BG1)
            col.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else S(8), 0))
            rng.grid_columnconfigure(i, weight=1, uniform="range")
            tk.Label(col, text=label, bg=BG1, fg=FG_DIM, font=fnt(9), anchor="w").pack(fill="x", pady=(0, S(3)))
            f = ui.Field(col, var, placeholder=ph, width=4)
            f.pack(fill="x")
            ui.Tooltip(f, tip)

        self._section(inner, "SAVE TO")
        frow = tk.Frame(inner, bg=BG1)
        frow.pack(fill="x")
        self.out_var = tk.StringVar(value=str(Path.home() / "Discord Exports"))
        browse = ui.Button(frow, "Browse", command=self._browse, variant="secondary", width=70, height=34, size=9,
                           bg=BG1)
        browse.pack(side="right", padx=(S(8), 0))
        ui.Field(frow, self.out_var, placeholder="Choose a folder", width=4).pack(side="left", fill="x", expand=True)

        self.format_caption_var, self.media_caption_var = tk.StringVar(), tk.StringVar()
        self._section(inner, "FORMAT", self.format_caption_var)
        self.format_chips = ui.ChipGroup(inner, self.FORMAT_OPTIONS, ["json", "html"], multi=True, bg=BG1,
                                         on_change=self._update_captions)
        self.format_chips.pack(fill="x")
        ui.Tooltip(self.format_chips, "JSON keeps everything and is easiest to reprocess later; HTML is a "
                                      "standalone viewer you can open in any browser. Pick as many as you like.")

        self._section(inner, "MEDIA", self.media_caption_var)
        self.media_chips = ui.ChipGroup(inner, self.MEDIA_OPTIONS, "attachments", bg=BG1,
                                        on_change=self._update_captions)
        self.media_chips.pack(fill="x")
        ui.Tooltip(self.media_chips, "None: text only. Attachments: images and files from messages. "
                                     "Everything: also embed images, stickers, emoji and avatars.")
        self._update_captions()

        tg = tk.Frame(inner, bg=BG1)
        tg.pack(fill="x", pady=(S(16), 0))
        self.reactors_var, self.fresh_var = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        ui.ToggleRow(tg, "Include who reacted", self.reactors_var, bg=BG1,
                     hint="Also records WHO reacted with each emoji, not just how many people did. Off by "
                          "default because it's slower - one extra request per reaction.").pack(anchor="w")
        ui.ToggleRow(tg, "Re-download everything", self.fresh_var, bg=BG1,
                     hint="Re-running an export normally only fetches what's new since last time (fast). "
                          "Turn this on to re-fetch from scratch, which also picks up edits and deletions "
                          "made to old messages.").pack(anchor="w", pady=(S(10), 0))

        self.channel_var.trace_add("write", lambda *_a: self._update_export_readiness())
        self.out_var.trace_add("write", lambda *_a: self._update_export_readiness())
        self._update_export_readiness()
        return panel

    def _update_captions(self) -> None:
        self.format_caption_var.set(" + ".join(FORMAT_FILES[f] for f in self.format_chips.get()))
        self.media_caption_var.set(MEDIA_CAPTIONS[self.media_chips.get()])


    def _toast(self, text: str, kind: str = "info", ms: int = 5000) -> None:
        color = {"error": RED, "ok": GREEN}.get(kind, PURPLE_HI)
        lbl = self._toast_label
        lbl.configure(text=text, fg=color, highlightbackground=color if kind != "info" else BORDER)
        lbl.place(relx=0.5, rely=1.0, y=-S(28), anchor="s")
        lbl.lift()
        if self._toast_job is not None:
            try:
                self.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.after(ms, lbl.place_forget)

    def _set_status(self, state: str, text: str) -> None:
        self._status_state = state
        self.conn_status.pack_forget()
        self.me_avatar.pack_forget()
        self.status_dot.pack_forget()
        if state == "ok" and self._me_photo is not None:
            self.me_avatar.configure(image=self._me_photo)
            self.me_avatar.pack(side="left", padx=(0, S(10)))
        else:
            color = {"busy": YELLOW, "error": RED}.get(state, FG_MUTED)
            self.status_dot.itemconfigure(self._dot_item, fill=color)
            self.status_dot.pack(side="left", padx=(0, S(8)))
        self.conn_status.configure(text=ui.ellipsize(text, fnt(10), S(210)), fg=FG if state == "ok" else FG_DIM)
        self.conn_status.pack(side="left")

    def _set_me_avatar(self, url: Optional[str], name: str, uid: Optional[str]) -> None:
        self._me_spec = (url, name, uid)
        self.images.request([url])
        self._me_photo = self.images.photo(url, S(30), (name or "?")[:1].upper(), _hue_to_hex(uid))
        if self._status_state == "ok":
            self.me_avatar.configure(image=self._me_photo)

    def _busy_on(self, key: str) -> None:
        self._busy.add(key)
        self.top_progress.set_indeterminate()

    def _busy_off(self, key: str) -> None:
        self._busy.discard(key)
        if not self._busy:
            self.top_progress.set_idle()

    def _active_token(self) -> str:
        return self._token or self.token_var.get().strip()

    def _on_token_edited(self) -> None:
        tok = self.token_var.get().strip()
        if self._autoconnect_job is not None:
            try:
                self.after_cancel(self._autoconnect_job)
            except Exception:
                pass
            self._autoconnect_job = None
        if self._token and tok != self._token and not self._connecting:
            self._set_status("idle", "Token changed - press Connect")
        if looks_like_token(tok) and tok != self._token and tok != self._failed_token and not self._connecting:
            self._autoconnect_job = self.after(450, self._autoconnect_fire)

    def _autoconnect_fire(self) -> None:
        self._autoconnect_job = None
        tok = self.token_var.get().strip()
        if looks_like_token(tok) and tok != self._token and not self._connecting:
            self._on_connect()

    def _toggle_show(self) -> None:
        self._token_hidden = not self._token_hidden
        self.token_field.set_hidden(self._token_hidden)
        self.show_btn.set_text("Show" if self._token_hidden else "Hide")

    def _paste_token(self) -> None:
        try:
            text = self.master.clipboard_get()
        except tk.TclError:
            self._toast("Your clipboard is empty.", "error")
            return
        self.token_var.set(text.strip())
        self._flash(self.paste_btn, "Pasted", "Paste")
        self._on_connect()

    def _open_token_help(self) -> None:
        try:
            opened = webbrowser.open(TOKEN_HELP_URL)
        except Exception:
            opened = False
        if not opened:
            self._toast(f"Couldn't open your browser. The video is at {TOKEN_HELP_URL}", "error", ms=9000)

    def _flash(self, button: Any, text: str, back: str, ms: int = 1200) -> None:
        button.set_text(text)
        self.after(ms, lambda: button.set_text(back))

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get().strip() or str(Path.home()))
        if d:
            self.out_var.set(d)

    def _toggle_log(self, open_: Optional[bool] = None) -> None:
        self._log_open = (not self._log_open) if open_ is None else open_
        if self._log_open:
            self.log_body.pack(side="top", fill="x")
            self.log_chevron.configure(text="\u25BE")
            self.log.see("end")
        else:
            self.log_body.pack_forget()
            self.log_chevron.configure(text="\u25B8")

    def _append_log_lines(self, lines: List[str]) -> None:
        if not lines:
            return
        t = self.log
        t.configure(state="normal")
        t.insert("end", "\n".join(lines) + "\n")
        total = int(t.index("end-1c").split(".")[0])
        if total > self.LOG_MAX_LINES:
            t.delete("1.0", f"{total - self.LOG_MAX_LINES + 200}.0")
        t.see("end")
        t.configure(state="disabled")
        self.log_summary_var.set(lines[-1].strip()[:140])

    def _set_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        if text:
            self.log.insert("end", text)
        self.log.configure(state="disabled")
        self.log_summary_var.set("")


    def _show_help(self) -> None:
        win = tk.Toplevel(self.master)
        win.title("How to use Restore")
        win.configure(bg=BG1)
        win.transient(self.master)
        ui.center_over(self.master, win, S(580), S(560))
        ui.apply_windows_dark_titlebar(win)
        tk.Label(win, text="How to use Restore", bg=BG1, fg=FG, font=fnt(15, True), anchor="w").pack(
            fill="x", padx=S(22), pady=(S(18), S(6)))
        body = tk.Frame(win, bg=BG1)
        ui.Button(win, "Close", command=win.destroy, variant="primary", width=96, height=34, bg=BG1).pack(
            side="bottom", pady=S(16))
        body.pack(fill="both", expand=True, padx=S(22), pady=(0, S(4)))
        text = tk.Text(body, bg=BG1, fg=FG_DIM, wrap="word", relief="flat", bd=0, highlightthickness=0,
                       font=fnt(10), padx=0, pady=S(4), spacing1=S(2), selectbackground=PURPLE_DIM)
        sb = ui.SlimScrollbar(body, command=text.yview, bg=BG1)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("h", foreground=PURPLE_HI, font=fnt(11, True), spacing1=S(14), spacing3=S(2))
        sections = [
            ("What this is", "Restore saves a copy of a Discord chat or DM's full history to your own computer - "
                             "messages, replies, reactions and attachments - so you still have it if the chat is "
                             "deleted or a link expires."),
            ("1. Connect", "Paste your own Discord account token at the top and press Connect (or Enter). It's "
                           "only ever sent to Discord's own API from this app - never written to a file and never "
                           "sent anywhere else. Not sure where to find it? Click \u201cHow to get token\u201d next "
                           "to the box for a short video walkthrough."),
            ("2. Pick a chat", "Your DMs and servers appear on the left; type in the search box (Ctrl+F) to filter. "
                               "Click a chat to preview its recent messages. If a DM you want isn't listed, Discord "
                               "only reports ones currently open in your client - paste its channel ID into "
                               "\u201cAdd a chat by channel ID\u201d under the list."),
            ("3. Set up the export", "On the right: choose a folder, how far back to go (blank means the whole "
                                     "history), which file types to save, and whether to grab attachments too."),
            ("4. Start export", "The button turns green once there's a chat and a folder. Progress shows as it "
                                "runs and you'll hear a sound when it finishes. Press it again while running to "
                                "cancel - what's already fetched is kept, and running it again later resumes."),
            ("Activity log", "The bar at the bottom of the preview shows what's happening during an export, "
                             "including any errors. Click it to expand."),
            ("Zoom & fullscreen", "Use +/- next to the chat title (or Ctrl +/-) to resize the preview text. F11 or "
                                  "F12 toggles fullscreen, Escape leaves it."),
            ("Re-running an export", "Running the same export again only fetches what's new, unless "
                                     "\u201cRe-download everything\u201d is switched on."),
        ]
        for heading, para in sections:
            text.insert("end", heading + "\n", ("h",))
            text.insert("end", para + "\n")
        text.configure(state="disabled")


    def _bind_shortcuts(self) -> None:
        m = self.master
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>", "<Control-Shift-plus>",
                    "<Control-Shift-equal>"):
            m.bind_all(seq, lambda _e: self._adjust_zoom(1))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            m.bind_all(seq, lambda _e: self._adjust_zoom(-1))
        for seq in ("<Control-0>", "<Control-KP_0>"):
            m.bind_all(seq, lambda _e: self._adjust_zoom(reset=True))
        for seq in ("<F11>", "<F12>"):
            m.bind_all(seq, lambda _e: self._toggle_fullscreen())
        m.bind_all("<Escape>", lambda _e: self._on_escape())
        for seq in ("<Control-f>", "<Control-F>"):
            m.bind_all(seq, lambda _e: self._focus_search())

    def _focus_search(self) -> None:
        self.search_field.focus()
        self.search_field.entry.select_range(0, "end")

    def _on_escape(self) -> None:
        if self._fullscreen:
            self._toggle_fullscreen()
        elif self.search_var.get():
            self.search_var.set("")

    def _toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        try:
            self.master.attributes("-fullscreen", self._fullscreen)
        except tk.TclError:
            self._fullscreen = False

    def _on_preview_ctrl_wheel(self, event: Any) -> str:
        self._adjust_zoom(1 if event.delta > 0 else -1)
        return "break"

    def _adjust_zoom(self, step: int = 0, reset: bool = False) -> None:
        new_zoom = 0 if reset else clamp_zoom(self._zoom + step)
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        self._configure_preview_tags()
        self.zoom_var.set(f"{zoom_percent(self._zoom)}%")
        if self._preview_msgs:
            self._render_preview(self._preview_msgs, keep_scroll=True)


    def _configure_preview_tags(self) -> None:
        t, z = self.preview, self._zoom
        body_pt, small_pt = zoomed_pt(10, z), zoomed_pt(9, z)
        left = S(22)
        text_left = left + S(self.AVATAR_PX) + 2 * S(4)
        right = S(32)
        t.configure(font=fnt(body_pt))
        t.tag_configure("hdr", lmargin1=left, lmargin2=left, rmargin=right, spacing1=S(18), spacing3=S(2))
        t.tag_configure("hdr_tight", spacing1=S(3))
        t.tag_configure("hdr_sep", spacing1=S(4))
        t.tag_configure("sepwin", lmargin1=left, lmargin2=left, spacing1=S(14), spacing3=S(2))
        t.tag_configure("rxline", spacing1=S(6))
        t.tag_configure("body", lmargin1=text_left, lmargin2=text_left, rmargin=right, spacing1=S(1), spacing3=S(1),
                        foreground="#e4e4ec", font=fnt(body_pt))
        t.tag_configure("gap", spacing1=S(6))
        t.tag_configure("time", foreground=FG_MUTED, font=fnt(small_pt - 1))
        t.tag_configure("reply", lmargin1=text_left, lmargin2=text_left, rmargin=right, spacing1=S(18),
                        foreground=FG_MUTED, font=fnt(small_pt))
        t.tag_configure("sys", justify="center", lmargin1=left, lmargin2=left, rmargin=right, spacing1=S(12),
                        foreground=FG_MUTED, font=fnt(small_pt))
        t.tag_configure("note", justify="center", foreground=FG_MUTED, spacing1=S(16), spacing3=S(8),
                        font=fnt(small_pt), lmargin1=left, lmargin2=left, rmargin=right)
        t.tag_configure("err", foreground=RED)
        t.tag_configure("attach", foreground=PURPLE_HI, background=INPUT, font=fnt(small_pt))
        t.tag_configure("embed", foreground=FG_DIM, font=fnt(small_pt))
        t.tag_configure("embed_title", foreground=FG, font=fnt(small_pt, True))
        t.tag_configure("bar", background=PURPLE_DIM)
        t.tag_configure("link", foreground="#8ab4ff", underline=True)
        t.tag_configure("mention", foreground="#e0c8ff", background=PURPLE_SOFT)
        t.tag_configure("react", foreground=FG_DIM, background=INPUT, font=fnt(small_pt))
        for tagname in self._author_tags:
            t.tag_configure(tagname, font=fnt(body_pt, True))
        t.tag_raise("sel")
        t.tag_bind("link", "<Button-1>", self._on_link_click)
        t.tag_bind("link", "<Enter>", lambda _e: self.preview.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda _e: self.preview.configure(cursor="arrow"))

    def _on_link_click(self, event: Any) -> None:
        t = self.preview
        idx = t.index(f"@{event.x},{event.y}")
        rng = t.tag_prevrange("link", f"{idx}+1c")
        if not rng:
            return
        url = t.get(*rng)
        if url.lower().startswith(("http://", "https://")):
            try:
                webbrowser.open(url)
            except Exception:
                pass


    def _on_connect(self) -> None:
        if self._connecting:
            return
        if self._autoconnect_job is not None:
            try:
                self.after_cancel(self._autoconnect_job)
            except Exception:
                pass
            self._autoconnect_job = None
        token = self.token_var.get().strip()
        if not token:
            self._toast("Paste your Discord token first.", "error")
            self.token_field.focus()
            return
        self._connecting = True
        self._conn_gen += 1
        gen = self._conn_gen
        self.connect_btn.set_enabled(False)
        self.connect_btn.set_text("Connecting\u2026")
        self._set_status("busy", "Connecting\u2026")
        self._busy_on("connect")
        self.images.forget_failures()

        def worker() -> None:
            try:
                api, overview = connect_account(token)
            except dx.AuthError:
                self.queue.put(("tree_error", gen, token, "Discord rejected that token (401). Double-check you "
                                                          "copied the whole thing."))
                return
            except dx.ApiError as e:
                self.queue.put(("tree_error", gen, token, f"Could not reach Discord: {e}"))
                return
            except Exception as e:
                self.queue.put(("tree_error", gen, token, f"Couldn't connect ({e.__class__.__name__}). Check your "
                                                          f"internet connection and try again."))
                return
            self.queue.put(("tree", gen, token, api, overview))

        threading.Thread(target=worker, daemon=True).start()

    def _on_tree(self, gen: int, token: str, api: Any, tree: Dict[str, Any]) -> None:
        if gen != self._conn_gen:
            return
        self._connecting = False
        self._token, self._api, self._failed_token = token, api, None
        self._guild_meta = {}
        self._chan_state, self._chan_done = set(), set()
        self.sidebar.set_notes("No open DMs found. Discord only lists DMs that are open in your client - add one "
                               "by its channel ID below.", "You're not in any servers.")
        self.sidebar.set_data(tree["dms"], tree["guilds"])
        self._set_me_avatar(tree.get("me_avatar_url"), tree["me"], tree.get("me_id"))
        self._busy_off("connect")
        self._set_status("ok", tree["me"])
        self.connect_btn.set_enabled(True)
        self.connect_btn.set_text("Reconnect")
        self.connect_btn.set_variant("secondary")
        self._start_channel_loading(gen, [g["id"] for g in tree["guilds"]])


    def _start_channel_loading(self, gen: int, guild_ids: List[str]) -> None:
        if self._chan_pool is not None:
            try:
                self._chan_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self._chan_pool = ThreadPoolExecutor(max_workers=3)
        for gid in guild_ids:
            self._chan_state.add(gid)
            self._chan_pool.submit(self._channels_job, gen, self._api, gid)

    def _channels_job(self, gen: int, api: Any, gid: str) -> None:
        if gen != self._conn_gen or gid in self._chan_done:
            return
        self.queue.put(("channels", gen, gid, load_guild_channels(api, gid)))

    def _request_channels_now(self, gid: str) -> None:
        if gid in self._chan_done or self._api is None:
            return
        threading.Thread(target=self._channels_job, args=(self._conn_gen, self._api, gid), daemon=True).start()

    def _on_channels(self, gen: int, gid: str, chans: List[dict]) -> None:
        if gen != self._conn_gen:
            return
        self._chan_done.add(gid)
        self.sidebar.set_guild_channels(gid, chans)

    def _on_tree_error(self, gen: int, token: str, message: str) -> None:
        if gen != self._conn_gen:
            return
        self._connecting = False
        self._failed_token = token
        self._busy_off("connect")
        self._set_status("error", "Not connected")
        self.connect_btn.set_enabled(True)
        self.connect_btn.set_text("Connect")
        self._toast(message, "error", ms=8000)

    def _on_sidebar_pick(self, kind: str, item: dict) -> None:
        if kind == "dm":
            label = item["label"]
            self._select_chat(item["id"], None, label, "Direct message", item.get("avatar_url"),
                              (label or "?")[:1].upper(), _hue_to_hex(item["id"]), "circle")
        else:
            self._select_chat(item["id"], item.get("guild_id"), item["label"], item.get("guild_name") or "Server",
                              item.get("guild_icon"), (item.get("guild_name") or "?")[:1].upper(),
                              _hue_to_hex(item.get("guild_id")), "squircle")

    def _select_chat(self, cid: str, gid: Optional[str], title: str, subtitle: str, avatar_url: Optional[str],
                     initial: str, color: str, shape: str) -> None:
        self._current = {"id": cid, "guild_id": gid}
        self._chat_summary = title
        self._chat_subtitle = subtitle
        self.channel_var.set(cid)
        self.chat_title_var.set(title)
        self.chat_sub_var.set(subtitle)
        self._set_header_avatar(avatar_url, initial, color, shape)
        self.export_title_var.set(ui.ellipsize(title, fnt(11, True), S(self.EXPORT_W) - S(80)))
        self.export_sub_var.set(ui.ellipsize(subtitle, fnt(9), S(self.EXPORT_W) - S(80)))
        self.empty_state.place_forget()
        self._load_preview(cid, gid)

    def _set_header_avatar(self, url: Optional[str], initial: str, color: str, shape: str = "circle") -> None:
        self._header_spec = (url, initial, color, shape)
        self.images.request([url])
        self._header_photo = self.images.photo(url, S(40), initial, color, shape)
        self.header_avatar.configure(image=self._header_photo)

    def _on_add_manual_chat(self) -> None:
        cid = self.manual_id_var.get().strip()
        if not cid.isdigit():
            self._toast("That doesn't look like a channel ID (it should be all digits).", "error", ms=7000)
            return
        dm = {"id": cid, "label": f"Chat {cid}", "avatar_url": None}
        self.sidebar.add_dm(dm)
        self.manual_id_var.set("")
        self.sidebar.select("dm", cid)
        self._on_sidebar_pick("dm", dm)


    def _on_refresh_preview(self) -> None:
        cid = self._current.get("id")
        if cid:
            self._load_preview(cid, self._current.get("guild_id"), force=True)

    def _clear_preview(self) -> None:
        t = self.preview
        try:
            if self._slots:
                t.mark_unset(*[sl["mark"] for sl in self._slots])
        except tk.TclError:
            pass
        t.delete("1.0", "end")
        for canvas, _label in self._seps:
            try:
                canvas.destroy()
            except tk.TclError:
                pass
        self._slots, self._slot_photos, self._seps = [], {}, []

    def _show_note(self, text: str, tag: str = "note") -> None:
        t = self.preview
        t.configure(state="normal")
        self._clear_preview()
        t.insert("end", text, (tag,))
        t.configure(state="disabled")
        self._preview_msgs = None

    def _load_preview(self, channel_id: str, guild_id: Optional[str], force: bool = False) -> None:
        self._preview_gen += 1
        gen = self._preview_gen
        if not force and channel_id in self._preview_cache:
            self._preview_cache.move_to_end(channel_id)
            self._busy_off("preview")
            self._render_preview(self._preview_cache[channel_id])
            return
        token = self._active_token()
        if not token:
            self._busy_off("preview")
            self._show_note("Paste your token at the top and press Connect to preview chats.")
            return
        self._show_note("Loading messages\u2026")
        self._busy_on("preview")
        api = self._api

        def worker() -> None:
            try:
                msgs = fetch_preview(token, channel_id, guild_id=guild_id, limit=self.PREVIEW_LIMIT, api=api,
                                     guild_cache=self._guild_meta)
            except dx.ApiError as e:
                self.queue.put(("preview_error", gen, channel_id, str(e)))
                return
            except Exception as e:
                self.queue.put(("preview_error", gen, channel_id, f"{e.__class__.__name__}: {e}"))
                return
            self.queue.put(("preview", gen, channel_id, msgs))

        threading.Thread(target=worker, daemon=True).start()

    def _on_preview(self, gen: int, cid: str, msgs: List[dict]) -> None:
        self._preview_cache[cid] = msgs
        self._preview_cache.move_to_end(cid)
        while len(self._preview_cache) > self.PREVIEW_CACHE_MAX:
            self._preview_cache.popitem(last=False)
        if gen == self._preview_gen:
            self._busy_off("preview")
            self._render_preview(msgs)

    def _on_preview_error(self, gen: int, cid: str, err: str) -> None:
        if gen == self._preview_gen:
            self._busy_off("preview")
            self._show_note(f"Couldn't load a preview: {err}", "err")


    def _insert_picture(self, build: Any, urls: List[Optional[str]], tags: Tuple[str, ...], padx: int = 0,
                        char: Optional[str] = None) -> None:
        t = self.preview
        pos = t.index("end-1c")
        photo = build()
        t.image_create(pos, image=photo, align="center", padx=padx)
        for tg in tags:
            t.tag_add(tg, pos, f"{pos}+1c")
        mark = f"slot{len(self._slots)}"
        t.mark_set(mark, pos)
        t.mark_gravity(mark, "left")
        real = [u for u in urls if u]
        self._slot_photos[mark] = photo
        self._slots.append({"mark": mark, "urls": real, "build": build, "char": char, "tags": tags,
                            "done": all(self.images.has(u) for u in real)})

    def _refresh_slots(self) -> None:
        t = self.preview
        for sl in self._slots:
            if sl["done"]:
                continue
            urls = sl["urls"]
            if not all(self.images.has(u) or self.images.is_failed(u) for u in urls):
                continue
            sl["done"] = True
            try:
                if sl["char"] and not any(self.images.has(u) for u in urls):
                    t.configure(state="normal")
                    t.delete(sl["mark"], f'{sl["mark"]}+1c')
                    t.insert(sl["mark"], sl["char"], sl["tags"])
                    t.configure(state="disabled")
                else:
                    photo = sl["build"]()
                    self._slot_photos[sl["mark"]] = photo
                    t.image_configure(sl["mark"], image=photo)
            except tk.TclError:
                pass

    def _zoom_factor(self) -> float:
        return zoom_percent(self._zoom) / 100.0

    def _emoji_photo(self, url: str) -> Any:
        return ui.make_emoji_photo(self.images.raw.get(url), S(round(20 * self._zoom_factor())))

    def _chip_photo(self, emoji: dict, count: int) -> Any:
        url = reaction_emoji_url(emoji)
        raw = self.images.raw.get(url) if url else None
        failed = bool(url) and self.images.is_failed(url)
        key = (url, count, bool(raw), failed, self._zoom)
        photo = self._chip_cache.get(key)
        if photo is None:
            if len(self._chip_cache) > 400:
                self._chip_cache.clear()
            photo = self._chip_cache[key] = ui.make_chip_photo(
                raw, (emoji.get("display") or "") if failed or not url else "", f"{count:,}",
                S(round(26 * self._zoom_factor())))
        return photo


    def _sep_width(self) -> int:
        w = self.preview.winfo_width()
        return max(S(240), (w if w > 50 else S(640)) - S(22) - S(32))

    def _draw_sep(self, canvas: Any, label: str) -> None:
        canvas.delete("all")
        w, h = int(float(canvas.cget("width"))), int(float(canvas.cget("height")))
        y, font = h // 2, fnt(zoomed_pt(9, self._zoom), True)
        canvas.create_line(0, y, w, y, fill=BORDER)
        half = ui.measure(label, font) / 2 + S(14)
        canvas.create_rectangle(w / 2 - half, 0, w / 2 + half, h, fill=BG0, outline="")
        canvas.create_text(w / 2, y, text=label, fill=FG_DIM, font=font)

    def _insert_divider(self, label: str) -> None:
        t = self.preview
        canvas = tk.Canvas(t, width=self._sep_width(), height=S(28), bg=BG0, highlightthickness=0, bd=0)
        self._draw_sep(canvas, label)
        pos = t.index("end-1c")
        t.window_create(pos, window=canvas, align="center")
        t.tag_add("sepwin", pos, f"{pos}+1c")
        t.insert("end", "\n", ("sepnl",))
        self._seps.append((canvas, label))

    def _on_preview_resize(self, _event: Any = None) -> None:
        w = self._sep_width()
        for canvas, label in self._seps:
            if abs(int(float(canvas.cget("width"))) - w) > 2:
                canvas.configure(width=w)
                self._draw_sep(canvas, label)

    def _render_preview(self, msgs: List[dict], keep_scroll: bool = False) -> None:
        t = self.preview
        view = t.yview() if keep_scroll else None
        self._preview_msgs = msgs
        t.configure(state="normal")
        self._clear_preview()
        urls: List[str] = []
        av = S(self.AVATAR_PX)
        pictures = ui.can_render_images()
        emoji_px = S(round(20 * self._zoom_factor()))
        if not msgs:
            t.insert("end", "No messages here yet - or none this account can see.", ("note",))
        prev_author: Optional[str] = None
        prev_min: Optional[int] = None
        prev_day = ""
        after_divider = False
        for m in msgs:
            day = m["local_time"][:10]
            if day != prev_day:
                self._insert_divider(pretty_day(m["local_time"]))
                prev_day, prev_author, after_divider = day, None, True
            stamp = m["local_time"][11:16]
            if m["system_text"] and m["type"] not in dx.CONTENT_TYPES:
                t.insert("end", f'{m["system_text"]}   {stamp}\n', ("sys",))
                prev_author, after_divider = None, False
                continue
            author = m["author"]
            aid = author["id"]
            minutes = msg_minutes(m["local_time"])
            new_group = (m["reply_to"] is not None or aid != prev_author or minutes is None or prev_min is None
                         or minutes - prev_min > GROUP_WINDOW_MINUTES)
            if new_group:
                tagname = f"author_{aid or 'x'}"
                if tagname not in self._author_tags:
                    t.tag_configure(tagname, foreground=_hue_to_hex(aid), font=fnt(zoomed_pt(10, self._zoom), True))
                    self._author_tags.add(tagname)
                if m["reply_to"]:
                    r = m["reply_to"]
                    prev = "[deleted]" if r["deleted"] else (r["content_preview"] or "")[:90]
                    t.insert("end", f'Replying to {r["author"] or "?"}:  {prev}\n', ("reply",))
                url = author.get("avatar_url")
                if url:
                    urls.append(url)
                name = author["display_name"] or "?"
                color = _hue_to_hex(aid)
                hdr_tags = ("hdr",) + (("hdr_tight",) if m["reply_to"] else ()) + (("hdr_sep",) if after_divider else ())
                self._insert_picture(lambda u=url, n=name, c=color: self.images.photo(u, av, n[:1].upper(), c),
                                     [url], hdr_tags, padx=S(4))
                t.insert("end", name, hdr_tags + (tagname,))
                t.insert("end", f"   {stamp}", hdr_tags + ("time",))
                t.insert("end", "\n", hdr_tags)
            after_divider = False
            start = t.index("end-1c")
            if m["content_text"]:
                parts = split_rich(m["content_text"])
                if parts and parts[0][1] == "mention":
                    t.insert("end", "\u200a", ("body",))
                for seg, kind in parts:
                    tags = ("body", kind) if kind else ("body",)
                    for piece, is_emoji in (split_emoji(seg) if pictures and not kind else [(seg, False)]):
                        if is_emoji:
                            eurl = twemoji_url(piece)
                            urls.append(eurl)
                            self._insert_picture(lambda u=eurl: self._emoji_photo(u), [eurl], ("body",), char=piece)
                        else:
                            t.insert("end", piece, tags)
                t.insert("end", "\n", ("body",))
            if not new_group:
                t.tag_add("gap", start, f"{start}+1c")
            for a in m["attachments"]:
                t.insert("end", "\u200a", ("body",))
                t.insert("end", f' {a["filename"]} ', ("body", "attach"))
                t.insert("end", "\n", ("body",))
            for e in m["embeds"]:
                title, desc = e.get("title"), e.get("description")
                if title or desc:
                    t.insert("end", "\u200a", ("body",))
                    t.insert("end", " ", ("body", "bar"))
                    t.insert("end", "  ", ("body", "embed"))
                    if title:
                        t.insert("end", title[:120], ("body", "embed", "embed_title"))
                    if desc:
                        t.insert("end", ("   " if title else "") + desc[:200].replace("\n", " "), ("body", "embed"))
                    t.insert("end", "\n", ("body",))
            for s_ in m["stickers"]:
                t.insert("end", "\u200a", ("body",))
                t.insert("end", f' sticker: {s_["name"]} ', ("body", "attach"))
                t.insert("end", "\n", ("body",))
            if m["poll"]:
                t.insert("end", "\u200a", ("body",))
                t.insert("end", f' poll: {m["poll"]["question"]} ', ("body", "attach"))
                t.insert("end", "\n", ("body",))
            if m["reactions"]:
                for i, r in enumerate(m["reactions"]):
                    e, n = r["emoji"], r["count"]
                    if pictures:
                        eurl = reaction_emoji_url(e)
                        if eurl:
                            urls.append(eurl)
                        self._insert_picture(lambda e=e, n=n: self._chip_photo(e, n), [eurl],
                                             ("body", "rxline") if i == 0 else ("body",), padx=S(2))
                        t.insert("end", " ", ("body",))
                    else:
                        if i == 0:
                            t.insert("end", "\u200a", ("body",))
                        t.insert("end", f' {e["display"]} {n} ', ("body", "react"))
                        t.insert("end", " ", ("body",))
                t.insert("end", "\n", ("body",))
            prev_author, prev_min = aid, minutes
        t.configure(state="disabled")
        if msgs:
            self.chat_sub_var.set(f"{self._chat_subtitle}   \u00b7   latest {len(msgs)} messages")
        self._preview_urls = list(dict.fromkeys(urls))
        if keep_scroll and view and view[1] < 0.999:
            t.yview_moveto(view[0])
        else:
            t.see("end")
        self.images.request(self._preview_urls)
        self._refresh_slots()


    def _on_channel_var_changed(self) -> None:
        self._update_export_readiness()

    def _export_ready(self) -> bool:
        return bool(self.channel_var.get().strip()) and bool(self.out_var.get().strip())

    def _update_export_readiness(self) -> None:
        if self._running:
            return
        self.start_btn.set_variant("success" if self._export_ready() else "muted")

    def _export_values(self) -> Dict[str, Any]:
        if not self._active_token():
            raise ValueError("Paste your Discord token first.")
        channel = self.channel_var.get().strip()
        if not channel:
            raise ValueError("Pick a chat on the left, or paste a channel ID.")
        limit = self.limit_var.get().strip()
        if limit and not limit.isdigit():
            raise ValueError("\u201cLatest N\u201d must be a whole number.")
        after = normalize_date_field(self.after_var.get().strip())
        before = normalize_date_field(self.before_var.get().strip())
        for field_label, val in (("After", after), ("Before", before)):
            if val:
                try:
                    dx.parse_when(val, None)
                except ValueError:
                    raise ValueError(f"Couldn't understand the \u201c{field_label}\u201d date: {val!r}. Try "
                                     f"MM/DD/YYYY, YYYY-MM-DD, a relative value like 7d, or a message ID.")
        out_dir = self.out_var.get().strip() or str(Path.home() / "Discord Exports")
        return {"channel_id": channel, "out_dir": out_dir, "formats": self.format_chips.get(), "limit": limit,
                "after": after, "before": before, "media": self.media_chips.get(),
                "reactors": self.reactors_var.get(), "fresh": self.fresh_var.get(), "tz": "local"}

    def _on_start_or_cancel(self) -> None:
        if self._running:
            self._cancel_export()
        else:
            self._on_start()

    def _on_start(self) -> None:
        try:
            values = self._export_values()
        except ValueError as e:
            self._toast(str(e), "error", ms=7000)
            return
        token = self._active_token()
        argv = build_export_argv(values)

        self._set_log("")
        self._phases = active_phases(values["reactors"], values["media"])
        self._overall_max = 0.0
        self.pct_var.set("")
        self._running, self._cancelling = True, False
        self._last_out_dir = values["out_dir"]
        self._export_chat_label = self._chat_summary
        self.open_btn.pack_forget()
        self.start_btn.set_variant("danger")
        self.start_btn.set_text("Cancel export")
        self.export_bar.set_color(GREEN)
        self.export_bar.set_indeterminate()
        self.export_status_var.set("Starting\u2026")

        def on_line(line: str) -> None:
            if not line.strip():
                return
            detail = parse_progress_detail(line)
            if detail:
                self.queue.put(("progress", detail))
            else:
                self.queue.put(("log", line.rstrip()))

        def worker() -> None:
            rc = 1
            try:
                rc = run_export_job(argv, token, on_line=on_line)
            except BaseException:
                rc = 130
            finally:
                sys.stdout, sys.stderr = self._orig_streams
                os.environ.pop("DISCORD_TOKEN", None)
                self.queue.put(("done", rc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _cancel_export(self) -> None:
        if not self._running or self._cancelling or self._thread is None:
            return
        self._cancelling = True
        self.start_btn.set_enabled(False)
        self.start_btn.set_text("Cancelling\u2026")
        self.export_status_var.set("Cancelling - finishing the current request, progress is kept\u2026")
        if not raise_in_thread(self._thread.ident, KeyboardInterrupt):
            self._cancelling = False
            self.start_btn.set_enabled(True)
            self.start_btn.set_text("Cancel export")

    def _on_progress(self, d: Dict[str, Any]) -> None:
        if not self._running:
            return
        label, pct = d["label"], d["pct"]
        phases = self._phases or [label]
        if label in phases:
            overall = max(self._overall_max, overall_percent(phases, label, pct))
            self._overall_max = overall
            if pct is None and overall <= 0:
                self.export_bar.set_indeterminate()
                self.pct_var.set("")
            else:
                self.export_bar.set_determinate(overall)
                self.pct_var.set(f"{int(overall)}%")
        names = dict(PHASES)
        parts = []
        if len(phases) > 1 and label in phases:
            parts.append(f"Step {phases.index(label) + 1} of {len(phases)}: {names.get(label, label)}")
        else:
            parts.append(names.get(label, label))
        counts = f"{d['done']:,} of {d['total']:,}" if d["total"] else f"{d['done']:,}"
        parts.append(f"{counts} {d['unit']}")
        if pct is not None:
            parts.append(f"about {d['eta']} left" if d["eta"] else "estimating time left\u2026")
        if not self._cancelling:
            self.export_status_var.set("  \u00b7  ".join(parts))

    def _on_export_done(self, rc: int) -> None:
        cancelled = self._cancelling or rc == 130
        self._running = self._cancelling = False
        self.start_btn.set_enabled(True)
        self.start_btn.set_text("Start export")
        self._update_export_readiness()
        if rc == 0:
            self.export_bar.set_color(GREEN)
            self.export_bar.set_determinate(100)
            self.pct_var.set("100%")
            self.export_status_var.set("Export complete.")
            self._toast("Export complete.", "ok")
        elif cancelled:
            self.export_bar.set_idle()
            self.pct_var.set("")
            self.export_status_var.set("Cancelled. Progress is saved - run it again to pick up where it left off.")
        else:
            self.export_bar.set_color(RED)
            self.export_bar.set_determinate(100)
            self.pct_var.set("")
            self.export_status_var.set(f"Finished with errors (code {rc}). See the activity log.")
            self._toggle_log(True)
        if self._last_out_dir and Path(self._last_out_dir).exists():
            self.open_btn.pack(fill="x", pady=(S(10), 0))
        if not cancelled:
            if not notify_export_finished(rc == 0, self._export_chat_label):
                try:
                    self.master.bell()
                except Exception:
                    pass

    def _open_folder(self) -> None:
        d = self._last_out_dir or self.out_var.get().strip()
        if not d or not Path(d).exists():
            self._toast("That folder doesn't exist yet.", "error")
            return
        try:
            open_in_file_manager(d)
        except Exception as e:
            self._toast(f"Couldn't open the folder: {e}", "error")


    def _handle(self, msg: tuple, log_lines: List[str]) -> None:
        tag = msg[0]
        if tag == "log":
            log_lines.append(msg[1])
        elif tag == "progress":
            self._on_progress(msg[1])
        elif tag == "tree":
            self._on_tree(msg[1], msg[2], msg[3], msg[4])
        elif tag == "tree_error":
            self._on_tree_error(msg[1], msg[2], msg[3])
        elif tag == "channels":
            self._on_channels(msg[1], msg[2], msg[3])
        elif tag == "preview":
            self._on_preview(msg[1], msg[2], msg[3])
        elif tag == "preview_error":
            self._on_preview_error(msg[1], msg[2], msg[3])
        elif tag == "images":
            got = msg[1]
            self.images.ingest(got)
            self.sidebar.redraw()
            if self._header_spec and self._header_spec[0] in got:
                self._set_header_avatar(*self._header_spec)
            if self._me_spec and self._me_spec[0] in got:
                self._set_me_avatar(*self._me_spec)
            self._refresh_slots()
        elif tag == "done":
            self._on_export_done(msg[1])

    def _poll(self) -> None:
        log_lines: List[str] = []
        try:
            for _ in range(300):
                try:
                    msg = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg, log_lines)
                except Exception:
                    log_lines.append("[internal error while handling " + str(msg[0]) + "]")
                    log_lines.extend(traceback.format_exc().rstrip().split("\n"))
            if log_lines:
                self._append_log_lines(log_lines)
        except Exception:
            pass
        finally:
            self.after(60, self._poll)

    def _on_close(self) -> None:
        if self._running:
            choice = ui.ask_dialog(
                self.master, "Quit while exporting?",
                "An export is still running. You can quit safely - what's already fetched is saved and running "
                "it again later picks up where this left off - but it will stop partway through.",
                [("quit", "Quit anyway", "danger"), ("stay", "Keep exporting", "secondary")])
            if choice != "quit":
                return
        self.master.destroy()


def main() -> None:
    if not _TK_OK:
        sys.exit("This GUI needs Tkinter, which normally ships with Python.\n"
                 "Windows/macOS: reinstall Python from python.org with 'tcl/tk and IDLE' checked.\n"
                 "Linux: sudo apt install python3-tk")
    ui.enable_dpi_awareness()
    root = tk.Tk()
    try:
        ui.set_scale(root.winfo_fpixels("1i") / 96.0)
    except Exception:
        pass
    ui.init_fonts()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
