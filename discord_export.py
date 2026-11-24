#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote, urlparse

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package:  pip install requests")

__version__ = "2.0.0"

API_BASE = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
DISCORD_EPOCH = 1_420_070_400_000

log = logging.getLogger("discord_export")

MESSAGE_TYPES = {
    0: "DEFAULT", 1: "RECIPIENT_ADD", 2: "RECIPIENT_REMOVE", 3: "CALL",
    4: "CHANNEL_NAME_CHANGE", 5: "CHANNEL_ICON_CHANGE", 6: "CHANNEL_PINNED_MESSAGE",
    7: "USER_JOIN", 8: "GUILD_BOOST", 9: "GUILD_BOOST_TIER_1", 10: "GUILD_BOOST_TIER_2",
    11: "GUILD_BOOST_TIER_3", 12: "CHANNEL_FOLLOW_ADD", 14: "GUILD_DISCOVERY_DISQUALIFIED",
    15: "GUILD_DISCOVERY_REQUALIFIED", 16: "GUILD_DISCOVERY_GRACE_PERIOD_INITIAL_WARNING",
    17: "GUILD_DISCOVERY_GRACE_PERIOD_FINAL_WARNING", 18: "THREAD_CREATED", 19: "REPLY",
    20: "CHAT_INPUT_COMMAND", 21: "THREAD_STARTER_MESSAGE", 22: "GUILD_INVITE_REMINDER",
    23: "CONTEXT_MENU_COMMAND", 24: "AUTO_MODERATION_ACTION", 25: "ROLE_SUBSCRIPTION_PURCHASE",
    26: "INTERACTION_PREMIUM_UPSELL", 27: "STAGE_START", 28: "STAGE_END", 29: "STAGE_SPEAKER",
    31: "STAGE_TOPIC", 32: "GUILD_APPLICATION_PREMIUM_SUBSCRIPTION",
    36: "GUILD_INCIDENT_ALERT_MODE_ENABLED", 37: "GUILD_INCIDENT_ALERT_MODE_DISABLED",
    38: "GUILD_INCIDENT_REPORT_RAID", 39: "GUILD_INCIDENT_REPORT_FALSE_ALARM",
    44: "PURCHASE_NOTIFICATION", 46: "POLL_RESULT",
}

CONTENT_TYPES = {0, 19, 20, 21, 23}


CH_TEXT, CH_DM, CH_VOICE, CH_GROUP_DM, CH_ANNOUNCE = 0, 1, 2, 3, 5
CH_ANN_THREAD, CH_PUB_THREAD, CH_PRIV_THREAD, CH_STAGE, CH_FORUM, CH_MEDIA = 10, 11, 12, 13, 15, 16
THREAD_TYPES = {CH_ANN_THREAD, CH_PUB_THREAD, CH_PRIV_THREAD}
TEXTLIKE_GUILD_TYPES = {CH_TEXT, CH_VOICE, CH_ANNOUNCE, CH_STAGE}
THREAD_PARENT_TYPES = {CH_TEXT, CH_ANNOUNCE, CH_FORUM, CH_MEDIA}


def snowflake_to_datetime(sf: Any) -> datetime:
    ms = (int(sf) >> 22) + DISCORD_EPOCH
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def datetime_to_snowflake(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return max(0, (ms - DISCORD_EPOCH) << 22)


_RE_FRAC = re.compile(r"\.(\d+)")


def parse_iso(s: str, default_tz: Optional[tzinfo] = timezone.utc) -> datetime:
    s = s.strip().replace("Z", "+00:00").replace("z", "+00:00")
    s = _RE_FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone() if default_tz is None else dt.replace(tzinfo=default_tz)
    return dt


_RE_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])(?:\s*ago)?$", re.I)
_REL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_when(text: str, tz: Optional[tzinfo]) -> int:
    t = text.strip()
    if t.isdigit() and len(t) >= 15:
        return int(t)
    if t.lower() == "now":
        return datetime_to_snowflake(datetime.now(timezone.utc))
    m = _RE_REL.match(t)
    if m:
        secs = float(m.group(1)) * _REL_UNITS[m.group(2).lower()]
        return datetime_to_snowflake(datetime.now(timezone.utc) - timedelta(seconds=secs))
    try:
        return datetime_to_snowflake(parse_iso(t, tz))
    except ValueError:
        raise ValueError(
            f"Can't understand date {text!r}. Use YYYY-MM-DD, 'YYYY-MM-DD HH:MM', a relative "
            f"value like 7d / 12h / 3w, or a message ID."
        )


def get_tz(name: str) -> Optional[tzinfo]:
    n = (name or "local").strip()
    if n.lower() == "local":
        return None
    if n.lower() == "utc":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(n)
    except Exception:
        log.warning("Unknown timezone %r (on Windows try 'pip install tzdata'); using UTC", n)
        return timezone.utc


def tz_label(tz: Optional[tzinfo]) -> str:
    now = datetime.now(timezone.utc)
    off = now.astimezone(tz).utcoffset() if tz is not None else now.astimezone().utcoffset()
    mins = int(round((off or timedelta(0)).total_seconds() / 60))
    if mins == 0:
        return "UTC"
    sign = "+" if mins > 0 else "-"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: Optional[str], default: str = "file", maxlen: int = 100) -> str:
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name or "").strip(" .")
    if not name:
        name = default
    stem, ext = os.path.splitext(name)
    ext = ext[:12]
    stem = stem[: max(1, maxlen - len(ext))]
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    return stem + ext


def human_size(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(secs: float) -> str:
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def atomic_write(path: Path, data: str | bytes, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding=encoding)
    os.replace(tmp, path)


def user_display(u: Optional[dict]) -> str:
    u = u or {}
    return u.get("global_name") or u.get("username") or "Unknown"


def say(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


class RedactFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "[REDACTED]")
        record.msg, record.args = msg, ()
        return True


def setup_logging(log_file: Optional[Path], verbose: bool, secrets: Iterable[str]) -> None:
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    redact = RedactFilter(secrets)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.addFilter(redact)
    log.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        fh.addFilter(redact)
        log.addHandler(fh)


def read_dotenv(key: str, path: str = ".env") -> Optional[str]:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip().removeprefix("export ").strip() == key:
                return v.strip().strip("'\"")
    except OSError:
        pass
    return None


def get_token() -> str:
    tok = os.environ.get("DISCORD_TOKEN") or read_dotenv("DISCORD_TOKEN")
    if not tok:
        if not sys.stdin.isatty():
            sys.exit("No token found. Set the DISCORD_TOKEN environment variable "
                     "(or add DISCORD_TOKEN=... to a .env file).")
        tok = getpass.getpass("Discord token (input hidden): ")
    tok = tok.strip().strip("'\"")
    if tok.lower().startswith("bot "):
        tok = tok[4:]
    if not tok:
        sys.exit("Empty token.")
    return tok


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: Optional[int] = None):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status, self.message, self.code = status, message, code


class AuthError(ApiError):
    pass


class ForbiddenError(ApiError):
    pass


class NotFoundError(ApiError):
    pass


class DiscordAPI:
    def __init__(self, token: str, *, bot: bool = False, base: str = API_BASE,
                 timeout: float = 30.0, max_retries: int = 6):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {token}" if bot else token,
            "User-Agent": f"DiscordBot (https://example.invalid, {__version__})" if bot else "Mozilla/5.0",
            "Accept": "application/json",
        })
        self._lock = threading.Lock()
        self._next_ok: Dict[str, float] = {}
        self.requests_made = 0
        self.rate_limited = 0


    @staticmethod
    def _route_key(path: str) -> str:
        parts = path.strip("/").split("/")
        out = []
        for i, p in enumerate(parts):
            if p.isdigit() and not (i > 0 and parts[i - 1] in ("channels", "guilds")):
                out.append(":id")
            else:
                out.append(p)
        if "reactions" in out:
            out = out[: out.index("reactions") + 1]
        return "/".join(out)

    def _wait_route(self, route: str) -> None:
        with self._lock:
            wait = self._next_ok.get(route, 0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _note_headers(self, route: str, r: requests.Response) -> None:
        try:
            if r.headers.get("X-RateLimit-Remaining") == "0":
                reset = float(r.headers.get("X-RateLimit-Reset-After", "0"))
                with self._lock:
                    self._next_ok[route] = time.monotonic() + reset + 0.05
        except ValueError:
            pass

    @staticmethod
    def _retry_after(r: requests.Response) -> float:
        try:
            return float(r.json().get("retry_after"))
        except Exception:
            pass
        try:
            return float(r.headers.get("Retry-After", 5))
        except ValueError:
            return 5.0

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(60.0, 2 ** attempt) * (0.75 + random.random() * 0.5)


    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                json_body: Any = None, no_retry: bool = False) -> Any:
        url = self.base + path
        route = self._route_key(path)
        attempt = 0
        rl_hits = 0
        while True:
            self._wait_route(route)
            try:
                with self._lock:
                    self.requests_made += 1
                r = self.session.request(method, url, params=params, json=json_body,
                                         timeout=(10, self.timeout))
            except (requests.ConnectionError, requests.Timeout) as e:
                attempt += 1
                if no_retry or attempt > self.max_retries:
                    raise ApiError(0, f"network error after {self.max_retries} retries: {e.__class__.__name__}")
                delay = self._backoff(attempt)
                log.warning("Network error (%s); retry %d/%d in %.1fs", e.__class__.__name__,
                            attempt, self.max_retries, delay)
                time.sleep(delay)
                continue

            self._note_headers(route, r)

            if r.status_code == 429:
                self.rate_limited += 1
                rl_hits += 1
                wait = self._retry_after(r)
                if no_retry:
                    raise ApiError(429, "rate limited")
                if rl_hits > 25:
                    raise ApiError(429, "rate limited too many times in a row; try a larger --delay")
                if wait > 900:
                    raise ApiError(429, f"Discord asked us to wait {wait:.0f}s; stopping to be safe. "
                                        f"Try again later with a larger --delay")
                log.warning("Rate limited on %s, sleeping %.1fs", route, wait)
                time.sleep(wait + 0.25)
                continue

            if r.status_code >= 500:
                attempt += 1
                if no_retry or attempt > self.max_retries:
                    raise ApiError(r.status_code, "server error, giving up after retries")
                delay = self._backoff(attempt)
                log.warning("Server error %s; retry %d/%d in %.1fs", r.status_code, attempt,
                            self.max_retries, delay)
                time.sleep(delay)
                continue

            if r.ok:
                if not r.content:
                    return None
                try:
                    return r.json()
                except ValueError:
                    raise ApiError(r.status_code, "response was not valid JSON")

            msg, code = r.reason or "error", None
            try:
                body = r.json()
                msg, code = body.get("message", msg), body.get("code")
            except Exception:
                pass
            if r.status_code == 401:
                raise AuthError(401, msg, code)
            if r.status_code == 403:
                raise ForbiddenError(403, msg, code)
            if r.status_code == 404:
                raise NotFoundError(404, msg, code)
            raise ApiError(r.status_code, msg, code)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def count_messages(self, cid: str, guild_id: Optional[str] = None, *, min_id: Any = None,
                       max_id: Any = None) -> Optional[int]:
        path = f"/guilds/{guild_id}/messages/search" if guild_id else f"/channels/{cid}/messages/search"
        params = {"limit": 1, "min_id": str(min_id) if min_id else None, "max_id": str(max_id) if max_id else None,
                  "channel_id": cid if guild_id else None}
        try:
            data = self.request("GET", path, params={k: v for k, v in params.items() if v is not None},
                                no_retry=True)
        except ApiError:
            return None
        total = data.get("total_results") if isinstance(data, dict) else None
        return total if isinstance(total, int) and total >= 0 else None


    def me(self) -> dict:
        return self.get("/users/@me")

    def dm_channels(self) -> List[dict]:
        return self.get("/users/@me/channels") or []

    def guilds(self) -> List[dict]:
        out: List[dict] = []
        after = None
        while True:
            page = self.get("/users/@me/guilds", limit=200, after=after) or []
            out.extend(page)
            if len(page) < 200:
                return out
            after = page[-1]["id"]

    def guild(self, gid: str) -> dict:
        return self.get(f"/guilds/{gid}")

    def guild_channels(self, gid: str) -> List[dict]:
        return self.get(f"/guilds/{gid}/channels") or []

    def guild_roles(self, gid: str) -> List[dict]:
        return self.get(f"/guilds/{gid}/roles") or []

    def channel(self, cid: str) -> dict:
        return self.get(f"/channels/{cid}")

    def active_threads(self, gid: str) -> List[dict]:
        return (self.get(f"/guilds/{gid}/threads/active") or {}).get("threads", [])

    def archived_threads(self, cid: str) -> List[dict]:
        out: List[dict] = []
        before = None
        while True:
            data = self.get(f"/channels/{cid}/threads/archived/public", limit=100, before=before) or {}
            threads = data.get("threads", [])
            out.extend(threads)
            if not data.get("has_more") or not threads:
                return out
            before = (threads[-1].get("thread_metadata") or {}).get("archive_timestamp")
            if not before:
                return out

    def messages(self, cid: str, *, before: Any = None, after: Any = None, limit: int = 100) -> List[dict]:
        return self.get(f"/channels/{cid}/messages", limit=limit,
                        before=str(before) if before else None,
                        after=str(after) if after else None) or []

    def reactors(self, cid: str, mid: str, emoji_key: str) -> List[dict]:
        out: List[dict] = []
        after = None
        while True:
            page = self.get(f"/channels/{cid}/messages/{mid}/reactions/{quote(emoji_key, safe=':')}",
                            limit=100, after=after) or []
            out.extend(page)
            if len(page) < 100:
                return out
            after = page[-1]["id"]

    def refresh_url(self, url: str) -> Optional[str]:
        try:
            data = self.request("POST", "/attachments/refresh-urls", json_body={"attachment_urls": [url]})
            for item in (data or {}).get("refreshed_urls", []):
                if item.get("original") == url:
                    return item.get("refreshed")
        except ApiError as e:
            log.debug("refresh-urls failed: %s", e)
        return None


RE_USER = re.compile(r"<@!?(\d+)>")
RE_ROLE = re.compile(r"<@&(\d+)>")
RE_CHAN = re.compile(r"<#(\d+)>")
RE_EMOJI = re.compile(r"<(a?):(\w+):(\d+)>")
RE_TIME = re.compile(r"<t:(-?\d+)(?::([tTdDfFR]))?>")


class Resolver:
    def __init__(self, tz: Optional[tzinfo]):
        self.tz = tz
        self.users: Dict[str, dict] = {}
        self.nicks: Dict[str, str] = {}
        self.roles: Dict[str, str] = {}
        self.channels: Dict[str, str] = {}

    def add_user(self, u: Optional[dict], member: Optional[dict] = None) -> None:
        if not u or not u.get("id"):
            return
        cur = self.users.setdefault(u["id"], {"id": u["id"]})
        for k in ("username", "global_name", "avatar", "bot"):
            if k in u:
                cur[k] = u[k]
        nick = (member or {}).get("nick")
        if nick:
            self.nicks[u["id"]] = nick

    def feed(self, m: dict, depth: int = 0) -> None:
        self.add_user(m.get("author"), m.get("member"))
        for u in m.get("mentions") or []:
            self.add_user(u, u.get("member"))
        for c in m.get("mention_channels") or []:
            if c.get("id") and c.get("name"):
                self.channels.setdefault(c["id"], c["name"])
        rm = m.get("referenced_message")
        if rm and depth < 1:
            self.feed(rm, depth + 1)

    def user_name(self, uid: Optional[str]) -> str:
        if not uid:
            return "Unknown"
        u = self.users.get(uid, {})
        return self.nicks.get(uid) or u.get("global_name") or u.get("username") or f"user {uid}"

    def role_name(self, rid: str) -> str:
        return self.roles.get(rid) or "unknown-role"

    def channel_name(self, cid: str) -> str:
        return self.channels.get(cid) or "unknown-channel"


def format_discord_time(ts: int, style: Optional[str], tz: Optional[tzinfo]) -> str:
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return "<invalid time>"
    fmt = {"t": "%H:%M", "T": "%H:%M:%S", "d": "%Y-%m-%d", "D": "%d %B %Y",
           "f": "%d %B %Y %H:%M", "F": "%A, %d %B %Y %H:%M"}.get(style or "f", "%Y-%m-%d %H:%M")
    return dt.strftime(fmt)


def to_text(content: str, res: Resolver) -> str:
    if not content:
        return ""
    s = RE_USER.sub(lambda m: "@" + res.user_name(m.group(1)), content)
    s = RE_ROLE.sub(lambda m: "@" + res.role_name(m.group(1)), s)
    s = RE_CHAN.sub(lambda m: "#" + res.channel_name(m.group(1)), s)
    s = RE_EMOJI.sub(lambda m: f":{m.group(2)}:", s)
    s = RE_TIME.sub(lambda m: format_discord_time(int(m.group(1)), m.group(2), res.tz), s)
    return s


_PH = re.compile("\ue000(\\d+)\ue001")
_RE_CODEBLOCK = re.compile(r"```(?:[\w+-]+\n)?(.*?)```", re.S)
_RE_INLINECODE = re.compile(r"`([^`\n]+)`")
_RE_MASKED = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_RE_BRACKET_URL = re.compile(r"&lt;(https?://(?:(?!&gt;)\S)+)&gt;")
_RE_URL = re.compile(r"https?://(?:(?!&lt;|&gt;|&quot;|&#x27;)[^\s\ue000\ue001])+")
_RE_SPOILER = re.compile(r"\|\|(.+?)\|\|", re.S)
_RE_BOLDITAL = re.compile(r"\*\*\*(.+?)\*\*\*", re.S)
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_RE_UNDER = re.compile(r"__(.+?)__", re.S)
_RE_ITAL_STAR = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_RE_ITAL_UND = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")
_RE_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_RE_HEADER = re.compile(r"^(#{1,3}) (.+)$")
_RE_SUBTEXT = re.compile(r"^-# (.+)$")
_URL_TAIL_ENTITIES = ("&gt;", "&lt;", "&quot;", "&#x27;")


class _Stash:
    def __init__(self) -> None:
        self.items: List[str] = []

    def put(self, markup: str) -> str:
        self.items.append(markup)
        return f"\ue000{len(self.items) - 1}\ue001"

    def restore(self, s: str) -> str:
        for _ in range(6):
            new = _PH.sub(lambda m: self.items[int(m.group(1))], s)
            if new == s:
                break
            s = new
        return s


def _link(url_escaped: str) -> str:
    url, tail = url_escaped, ""
    while True:
        for ent in _URL_TAIL_ENTITIES:
            if url.endswith(ent):
                url, tail = url[: -len(ent)], ent + tail
                break
        else:
            if (url and url[-1] in ".,!?:'\"") or (url.endswith(")") and url.count("(") < url.count(")")):
                url, tail = url[:-1], url[-1] + tail
                continue
            break
    if not url:
        return url_escaped
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{url}</a>{tail}'


def render_html(content: str, res: Resolver, emoji_src=None) -> str:
    if not content:
        return ""
    esc = html.escape
    st = _Stash()
    s = content.replace("\ue000", "").replace("\ue001", "")
    s = _RE_CODEBLOCK.sub(lambda m: st.put(f"<pre><code>{esc(m.group(1).strip(chr(10)))}</code></pre>"), s)
    s = _RE_INLINECODE.sub(lambda m: st.put(f"<code>{esc(m.group(1))}</code>"), s)
    s = esc(s)

    s = _RE_MASKED.sub(lambda m: st.put(
        f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer nofollow">{m.group(1)}</a>'), s)
    s = _RE_BRACKET_URL.sub(lambda m: st.put(_link(m.group(1))), s)
    s = _RE_URL.sub(lambda m: st.put(_link(m.group(0))), s)

    s = re.sub(r"&lt;@!?(\d+)&gt;",
               lambda m: st.put(f'<span class="mention">@{esc(res.user_name(m.group(1)))}</span>'), s)
    s = re.sub(r"&lt;@&amp;(\d+)&gt;",
               lambda m: st.put(f'<span class="mention">@{esc(res.role_name(m.group(1)))}</span>'), s)
    s = re.sub(r"&lt;#(\d+)&gt;",
               lambda m: st.put(f'<span class="mention">#{esc(res.channel_name(m.group(1)))}</span>'), s)

    def emoji(m: "re.Match[str]") -> str:
        animated, name, eid = bool(m.group(1)), m.group(2), m.group(3)
        src = emoji_src(eid, animated) if emoji_src else f"{CDN}/emojis/{eid}.{'gif' if animated else 'png'}?size=48"
        return st.put(f'<img class="emoji" src="{esc(src)}" alt=":{name}:" title=":{name}:" loading="lazy">')
    s = re.sub(r"&lt;(a?):(\w+):(\d+)&gt;", emoji, s)

    s = re.sub(r"&lt;t:(-?\d+)(?::([tTdDfFR]))?&gt;",
               lambda m: st.put(f'<span class="mention">{esc(format_discord_time(int(m.group(1)), m.group(2), res.tz))}</span>'),
               s)
    s = re.sub(r"@(everyone|here)\b", lambda m: st.put(f'<span class="mention">@{m.group(1)}</span>'), s)

    s = _RE_SPOILER.sub(r'<span class="spoiler">\1</span>', s)
    s = _RE_BOLDITAL.sub(r"<strong><em>\1</em></strong>", s)
    s = _RE_BOLD.sub(r"<strong>\1</strong>", s)
    s = _RE_UNDER.sub(r"<u>\1</u>", s)
    s = _RE_ITAL_STAR.sub(r"<em>\1</em>", s)
    s = _RE_ITAL_UND.sub(r"<em>\1</em>", s)
    s = _RE_STRIKE.sub(r"<s>\1</s>", s)


    parts: List[Tuple[str, str]] = []
    quote_buf: List[str] = []
    lines = s.split("\n")

    def flush_quote() -> None:
        if quote_buf:
            parts.append(("block", "<blockquote>" + "<br>".join(quote_buf) + "</blockquote>"))
            quote_buf.clear()

    for i, ln in enumerate(lines):
        if ln.startswith("&gt;&gt;&gt; "):
            flush_quote()
            rest = [ln[len("&gt;&gt;&gt; "):]] + lines[i + 1:]
            parts.append(("block", "<blockquote>" + "<br>".join(rest) + "</blockquote>"))
            break
        if ln.startswith("&gt; "):
            quote_buf.append(ln[len("&gt; "):])
            continue
        flush_quote()
        h = _RE_HEADER.match(ln)
        sub = _RE_SUBTEXT.match(ln)
        if h:
            parts.append(("block", f'<div class="h h{len(h.group(1))}">{h.group(2)}</div>'))
        elif sub:
            parts.append(("block", f'<div class="sub">{sub.group(1)}</div>'))
        else:
            parts.append(("text", ln))
    flush_quote()

    out: List[str] = []
    prev_kind = None
    for kind, markup in parts:
        if out and kind == "text" and prev_kind == "text":
            out.append("<br>")
        out.append(markup)
        prev_kind = kind
    return st.restore("".join(out))


def attachment_rel(mid: str, att: dict) -> str:
    return f"media/attachments/{mid}_{att.get('id', '0')}_{safe_filename(att.get('filename'))}"


def embed_img_url(x: Optional[dict]) -> Optional[str]:
    return (x or {}).get("url") or (x or {}).get("proxy_url")


def embed_img_rel(mid: str, url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".bin"
    return f"media/embeds/{mid}_{hashlib.sha1(url.encode()).hexdigest()[:10]}{ext}"


def sticker_ext(fmt: Optional[int]) -> str:
    return {1: "png", 2: "png", 3: "json", 4: "gif"}.get(fmt or 1, "png")


def sticker_url(st: dict) -> str:
    ext = sticker_ext(st.get("format_type"))
    host = CDN if ext == "json" else "https://media.discordapp.net"
    return f"{host}/stickers/{st['id']}.{ext}"


def sticker_rel(st: dict) -> str:
    return f"media/stickers/{st['id']}.{sticker_ext(st.get('format_type'))}"


def avatar_ext(u: dict) -> str:
    return "gif" if str(u.get("avatar") or "").startswith("a_") else "png"


def avatar_url(u: dict, size: int = 64) -> Optional[str]:
    if not u.get("avatar") or not u.get("id"):
        return None
    return f"{CDN}/avatars/{u['id']}/{u['avatar']}.{avatar_ext(u)}?size={size}"


def avatar_rel(u: dict) -> Optional[str]:
    if not u.get("avatar") or not u.get("id"):
        return None
    return f"media/avatars/{u['id']}_{u['avatar']}.{avatar_ext(u)}"


def emoji_url(eid: str, animated: bool, size: int = 48) -> str:
    return f"{CDN}/emojis/{eid}.{'gif' if animated else 'png'}?size={size}"


def emoji_rel(eid: str, animated: bool) -> str:
    return f"media/emoji/{eid}.{'gif' if animated else 'png'}"


def iter_attachments(m: dict) -> Iterator[dict]:
    yield from m.get("attachments") or []
    for snap in m.get("message_snapshots") or []:
        yield from (snap.get("message") or {}).get("attachments") or []


def iter_embeds(m: dict) -> Iterator[dict]:
    yield from m.get("embeds") or []
    for snap in m.get("message_snapshots") or []:
        yield from (snap.get("message") or {}).get("embeds") or []


def system_text(m: dict, res: Resolver) -> Optional[str]:
    t = m.get("type", 0)
    if t in CONTENT_TYPES:
        return None
    who = res.user_name((m.get("author") or {}).get("id"))
    mentions = m.get("mentions") or []
    target = res.user_name(mentions[0]["id"]) if mentions else "someone"
    content = to_text(m.get("content") or "", res)
    if t == 3:
        call = m.get("call") or {}
        ended = call.get("ended_timestamp")
        started_by_author = (m.get("author") or {}).get("id") in (call.get("participants") or [])
        if not started_by_author:
            return f"{who} started a call (missed)"
        if ended:
            try:
                dur = (parse_iso(ended) - parse_iso(m["timestamp"])).total_seconds()
                return f"{who} started a call that lasted {human_duration(dur)}"
            except (ValueError, KeyError):
                pass
        return f"{who} started a call"
    table = {
        1: f"{who} added {target} to the group",
        2: f"{who} removed {target} from the group",
        4: f"{who} changed the channel name: {content}".rstrip(": "),
        5: f"{who} changed the group icon",
        6: f"{who} pinned a message to this channel",
        7: f"{who} joined the server",
        8: f"{who} boosted the server",
        9: f"{who} boosted the server (now Level 1)",
        10: f"{who} boosted the server (now Level 2)",
        11: f"{who} boosted the server (now Level 3)",
        12: f"{who} added a followed channel: {content}".rstrip(": "),
        18: f"{who} started a thread: {content}".rstrip(": "),
        22: "Invite reminder",
        27: f"{who} started a stage: {content}".rstrip(": "),
        28: f"{who} ended the stage: {content}".rstrip(": "),
        29: f"{who} is now a speaker",
        31: f"{who} changed the stage topic: {content}".rstrip(": "),
        46: "A poll has ended",
    }
    return table.get(t) or f"[{MESSAGE_TYPES.get(t, f'UNKNOWN_{t}')}] {content}".strip()


def emoji_key(emoji: dict) -> str:
    if emoji.get("id"):
        return f"{emoji.get('name') or '_'}:{emoji['id']}"
    return emoji.get("name") or ""


@dataclass
class CleanContext:
    res: Resolver
    tz: Optional[tzinfo]
    root: Path
    index: Dict[str, dict]
    reactors: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def local(self, rel: Optional[str]) -> Optional[str]:
        if rel and (self.root / rel).is_file():
            return rel
        return None


def _clean_attachment(mid: str, a: dict, ctx: CleanContext) -> dict:
    return {
        "id": a.get("id"), "filename": a.get("filename"), "url": a.get("url"),
        "content_type": a.get("content_type"), "size": a.get("size"),
        "width": a.get("width"), "height": a.get("height"),
        "description": a.get("description"), "duration_secs": a.get("duration_secs"),
        "local_path": ctx.local(attachment_rel(mid, a)),
    }


def _clean_embed_media(mid: str, x: Optional[dict], ctx: CleanContext) -> Optional[dict]:
    url = embed_img_url(x)
    if not x or not url:
        return None
    return {"url": x.get("url"), "proxy_url": x.get("proxy_url"), "width": x.get("width"),
            "height": x.get("height"), "local_path": ctx.local(embed_img_rel(mid, url))}


def _clean_embed(mid: str, e: dict, ctx: CleanContext) -> dict:
    return {
        "type": e.get("type"), "title": e.get("title"), "description": e.get("description"),
        "url": e.get("url"), "timestamp": e.get("timestamp"), "color": e.get("color"),
        "provider": e.get("provider"), "author": e.get("author"), "footer": e.get("footer"),
        "image": _clean_embed_media(mid, e.get("image"), ctx),
        "thumbnail": _clean_embed_media(mid, e.get("thumbnail"), ctx),
        "video": e.get("video"),
        "fields": [{"name": f.get("name"), "value": f.get("value"), "inline": bool(f.get("inline"))}
                   for f in e.get("fields") or []],
    }


def _clean_poll(p: dict) -> dict:
    counts = {c.get("id"): c.get("count", 0) for c in (p.get("results") or {}).get("answer_counts", [])}
    return {
        "question": (p.get("question") or {}).get("text"),
        "multiselect": bool(p.get("allow_multiselect")),
        "expiry": p.get("expiry"),
        "finalized": bool((p.get("results") or {}).get("is_finalized")),
        "answers": [{
            "id": a.get("answer_id"),
            "text": (a.get("poll_media") or {}).get("text"),
            "emoji": ((a.get("poll_media") or {}).get("emoji") or {}).get("name"),
            "votes": counts.get(a.get("answer_id")),
        } for a in p.get("answers") or []],
    }


def clean_message(m: dict, ctx: CleanContext) -> dict:
    res = ctx.res
    mid = m["id"]
    a = m.get("author") or {}
    uid = a.get("id")
    t = m.get("type", 0)
    dt_utc = parse_iso(m["timestamp"])
    dt_loc = dt_utc.astimezone(ctx.tz)
    content = m.get("content") or ""

    reply = None
    ref = m.get("message_reference") or {}
    if ref.get("message_id") and ref.get("type", 0) == 0 and t in (0, 19, 20, 23) \
            and ref.get("channel_id", m.get("channel_id")) == m.get("channel_id"):
        rm = m.get("referenced_message") or ctx.index.get(ref["message_id"])
        deleted = rm is None and "referenced_message" in m
        ra = (rm or {}).get("author") or {}
        preview = to_text((rm or {}).get("content") or "", res)
        if rm and not preview:
            preview = ("[attachment]" if rm.get("attachments") else "[embed]" if rm.get("embeds")
                       else "[forwarded message]" if rm.get("message_snapshots")
                       else "[sticker]" if (rm.get("sticker_items") or rm.get("stickers"))
                       else "[poll]" if rm.get("poll") else "")
        reply = {
            "message_id": ref["message_id"], "channel_id": ref.get("channel_id"),
            "author_id": ra.get("id"), "author": res.user_name(ra.get("id")) if ra.get("id") else None,
            "content_preview": preview[:300] if rm else None,
            "deleted": bool(deleted),
        }

    forwarded = None
    if ref.get("type") == 1 or m.get("message_snapshots"):
        snap = ((m.get("message_snapshots") or [{}])[0]).get("message") or {}
        forwarded = {
            "content": snap.get("content") or "",
            "content_text": to_text(snap.get("content") or "", res),
            "timestamp": snap.get("timestamp"),
            "attachments": [_clean_attachment(mid, x, ctx) for x in snap.get("attachments") or []],
            "embeds": [_clean_embed(mid, x, ctx) for x in snap.get("embeds") or []],
            "source": {"message_id": ref.get("message_id"), "channel_id": ref.get("channel_id"),
                       "guild_id": ref.get("guild_id")},
        }

    stickers = []
    for st in (m.get("sticker_items") or m.get("stickers") or []):
        if not st.get("id"):
            continue
        stickers.append({"id": st["id"], "name": st.get("name"), "format_type": st.get("format_type"),
                         "url": sticker_url(st), "local_path": ctx.local(sticker_rel(st))})

    reactions = []
    for r in m.get("reactions") or []:
        e = r.get("emoji") or {}
        cached = (ctx.reactors.get(mid) or {}).get(emoji_key(e)) or {}
        item = {
            "emoji": {"id": e.get("id"), "name": e.get("name"), "animated": bool(e.get("animated")),
                      "display": f":{e.get('name')}:" if e.get("id") else e.get("name"),
                      "local_path": ctx.local(emoji_rel(e["id"], bool(e.get("animated")))) if e.get("id") else None},
            "count": r.get("count", 0), "me": bool(r.get("me")),
        }
        if r.get("count_details"):
            item["count_details"] = r["count_details"]
        if cached.get("users") is not None:
            item["users"] = cached["users"]
        reactions.append(item)

    nick = (m.get("member") or {}).get("nick")
    sys_txt = system_text(m, res)
    out = {
        "id": mid,
        "channel_id": m.get("channel_id"),
        "timestamp": m["timestamp"],
        "local_time": dt_loc.isoformat(timespec="seconds"),
        "edited_timestamp": m.get("edited_timestamp"),
        "type": t,
        "type_name": MESSAGE_TYPES.get(t, f"UNKNOWN_{t}"),
        "system_text": sys_txt,
        "pinned": bool(m.get("pinned")),
        "author": {
            "id": uid, "username": a.get("username"), "global_name": a.get("global_name"),
            "nick": nick, "display_name": res.user_name(uid), "bot": bool(a.get("bot")),
            "avatar_url": avatar_url(a), "avatar_local": ctx.local(avatar_rel({**a})),
        },
        "content": content,
        "content_text": to_text(content, res),
        "mentions": [{"id": u.get("id"), "username": u.get("username"), "display_name": res.user_name(u.get("id"))}
                     for u in m.get("mentions") or []],
        "mention_roles": [{"id": r, "name": res.role_name(r)} for r in m.get("mention_roles") or []],
        "mention_everyone": bool(m.get("mention_everyone")),
        "reply_to": reply,
        "forwarded": forwarded,
        "attachments": [_clean_attachment(mid, x, ctx) for x in m.get("attachments") or []],
        "embeds": [_clean_embed(mid, x, ctx) for x in m.get("embeds") or []],
        "stickers": stickers,
        "reactions": reactions,
        "poll": _clean_poll(m["poll"]) if m.get("poll") else None,
        "thread": ({"id": m["thread"].get("id"), "name": m["thread"].get("name")} if m.get("thread") else None),
        "webhook_id": m.get("webhook_id"),
        "application_id": m.get("application_id"),
        "interaction": (m.get("interaction") or {}).get("name") or (m.get("interaction_metadata") or {}).get("name"),
        "flags": m.get("flags", 0),
    }
    return out


class Progress:
    def __init__(self, label: str, total: Optional[int] = None, unit: str = "msgs"):
        self.label, self.total, self.unit = label, total, unit
        self.start = time.monotonic()
        self.last_draw = 0.0
        self.tty = sys.stderr.isatty()
        self.n = 0
        self._drew = False
        self._f_max = 0.0
        self._f0: Optional[float] = None
        self._t0 = 0.0
        self._eta: Optional[float] = None
        self._eta_t = 0.0

    def _estimate_eta(self, now: float, fraction: float) -> Optional[float]:
        if self._f0 is None:
            self._f0, self._t0 = fraction, now
            return None
        gained, elapsed = fraction - self._f0, now - self._t0
        if gained < 0.003 or elapsed < 3.0:
            return None
        new = elapsed * (1.0 - fraction) / gained
        if self._eta is None:
            self._eta = new
        else:
            self._eta = 0.7 * max(0.0, self._eta - (now - self._eta_t)) + 0.3 * new
        self._eta_t = now
        return self._eta

    def update(self, n: int, fraction: Optional[float] = None, extra: str = "",
               of: Optional[Tuple[int, int]] = None) -> None:
        self.n = n
        now = time.monotonic()
        if self.tty and now - self.last_draw < 0.15:
            return
        if not self.tty and now - self.last_draw < 5:
            return
        self.last_draw = now
        elapsed = max(now - self.start, 1e-6)
        rate = n / elapsed
        if fraction is None and self.total:
            fraction = min(1.0, n / self.total)
        count = f"{of[0]:,} of {of[1]:,}" if of else f"{n:,}"
        if fraction is not None:
            fraction = self._f_max = max(self._f_max, max(0.0, min(1.0, fraction)))
            filled = int(24 * fraction)
            bar = "#" * filled + "-" * (24 - filled)
            eta_s = self._estimate_eta(now, fraction)
            eta = f" ETA {human_duration(eta_s)}" if eta_s is not None else ""
            line = f"{self.label} [{bar}] {fraction * 100:3.0f}%  {count} {self.unit}  {rate:,.0f}/s{eta} {extra}"
        else:
            line = f"{self.label} {count} {self.unit}  {rate:,.0f}/s  {human_duration(elapsed)} {extra}"
        if self.tty:
            sys.stderr.write("\r" + line.ljust(100)[:100])
            sys.stderr.flush()
            self._drew = True
        else:
            say(line)

    def done(self, msg: str = "") -> None:
        if self.tty and self._drew:
            sys.stderr.write("\r" + " " * 100 + "\r")
        say(f"{self.label}: {msg or f'{self.n:,} {self.unit}'} in {human_duration(time.monotonic() - self.start)}")


class ChannelStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.raw_path = directory / "raw.jsonl"
        self.state_path = directory / "state.json"
        self.messages: Dict[str, dict] = {}
        self.complete_backwards = False
        self._min: Optional[int] = None
        self._max: Optional[int] = None

    @property
    def oldest_id(self) -> Optional[int]:
        return self._min

    @property
    def newest_id(self) -> Optional[int]:
        return self._max

    def _track(self, mid: int) -> None:
        self._min = mid if self._min is None else min(self._min, mid)
        self._max = mid if self._max is None else max(self._max, mid)

    def load(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        dirty = False
        if self.raw_path.exists():
            with open(self.raw_path, "rb") as f:
                data = f.read()
            if data and not data.endswith(b"\n"):
                dirty = True
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                    self.messages[m["id"]] = m
                    self._track(int(m["id"]))
                except (ValueError, KeyError, TypeError):
                    log.warning("Dropping corrupt line %d of %s (likely an interrupted write)", n, self.raw_path)
                    dirty = True
        if dirty:
            self._rewrite()
        if self.state_path.exists():
            try:
                self.complete_backwards = bool(json.loads(self.state_path.read_text("utf-8")).get("complete_backwards"))
            except (ValueError, OSError):
                pass

    def _rewrite(self) -> None:
        body = "".join(json.dumps(self.messages[k], ensure_ascii=False, separators=(",", ":")) + "\n"
                       for k in sorted(self.messages, key=int, reverse=True))
        atomic_write(self.raw_path, body)

    def append(self, batch: List[dict]) -> List[dict]:
        new = [m for m in batch if m["id"] not in self.messages]
        if not new:
            return []
        with open(self.raw_path, "a", encoding="utf-8") as f:
            for m in new:
                f.write(json.dumps(m, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        for m in new:
            self.messages[m["id"]] = m
            self._track(int(m["id"]))
        return new

    def save_state(self) -> None:
        atomic_write(self.state_path, json.dumps({
            "complete_backwards": self.complete_backwards,
            "count": len(self.messages),
            "oldest_id": str(self._min) if self._min else None,
            "newest_id": str(self._max) if self._max else None,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))

    def reset(self) -> None:
        for p in (self.raw_path, self.state_path):
            p.unlink(missing_ok=True)
        self.messages.clear()
        self.complete_backwards = False
        self._min = self._max = None


@dataclass
class SyncOptions:
    limit: int = 0
    after_id: Optional[int] = None
    before_id: Optional[int] = None
    delay: float = 0.3
    total_hint: Optional[int] = None


def sync_channel(api: DiscordAPI, channel_id: str, store: ChannelStore, opt: SyncOptions,
                 progress: Progress) -> int:
    fetched = 0
    after_id, before_id, limit = opt.after_id, opt.before_id, opt.limit

    def in_range(mid: int) -> bool:
        return (after_id is None or mid > after_id) and (before_id is None or mid < before_id)

    n_range = sum(1 for k in store.messages if in_range(int(k)))
    head_ts = time.time()
    low_ts = (snowflake_to_datetime(after_id).timestamp() if after_id
              else snowflake_to_datetime(channel_id).timestamp())

    totals = [t for t in (opt.total_hint, limit) if t]
    total = min(totals) if totals else None
    cap = 1.0 if (limit and total == limit) else 0.99

    def shown() -> Optional[Tuple[int, int]]:
        return (min(n_range, total), total) if total else None

    def backward_fraction() -> Optional[float]:
        if total:
            return min(cap, n_range / total)
        if store.oldest_id is None:
            return None
        span = head_ts - low_ts
        return (head_ts - snowflake_to_datetime(store.oldest_id).timestamp()) / span if span > 0 else None

    def pace() -> None:
        if opt.delay > 0:
            time.sleep(opt.delay)

    try:
        if store.messages and (before_id is None or store.newest_id < before_id):
            cursor = store.newest_id
            while True:
                batch = api.messages(channel_id, after=cursor)
                if not batch:
                    break
                batch.sort(key=lambda m: int(m["id"]))
                keep = [m for m in batch if before_id is None or int(m["id"]) < before_id]
                added = store.append(keep)
                fetched += len(added)
                n_range += sum(1 for m in added if in_range(int(m["id"])))
                progress.update(fetched, None, "(new messages)")
                if len(keep) < len(batch):
                    break
                cursor = int(batch[-1]["id"])
                pace()


        if not store.complete_backwards:
            cursor = store.oldest_id if store.messages else before_id
            while True:
                if limit and n_range >= limit:
                    break
                if after_id is not None and store.oldest_id is not None and store.oldest_id <= after_id:
                    break
                batch = api.messages(channel_id, before=cursor)
                if not batch:
                    store.complete_backwards = True
                    break
                batch.sort(key=lambda m: int(m["id"]), reverse=True)
                added = store.append(batch)
                fetched += len(added)
                n_range += sum(1 for m in added if in_range(int(m["id"])))
                cursor = int(batch[-1]["id"])
                progress.update(fetched, backward_fraction(), of=shown())
                if store.oldest_id is not None and len(added) == 0:
                    break
                pace()
    finally:
        store.save_state()
    return fetched


def fetch_reactors(api: DiscordAPI, channel_id: str, msgs: List[dict], cache_path: Path,
                   delay: float) -> Dict[str, Dict[str, Any]]:
    cache: Dict[str, Dict[str, Any]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text("utf-8"))
        except (ValueError, OSError):
            cache = {}
    todo = [m for m in msgs if m.get("reactions")]
    prog = Progress("Reaction users", len(todo), "msgs")
    try:
        for i, m in enumerate(todo, 1):
            entry = cache.setdefault(m["id"], {})
            for r in m["reactions"]:
                key = emoji_key(r.get("emoji") or {})
                if not key:
                    continue
                have = entry.get(key)
                if have and have.get("count") == r.get("count"):
                    continue
                try:
                    users = api.reactors(channel_id, m["id"], key)
                except (ForbiddenError, NotFoundError) as e:
                    log.warning("Can't list reactors for message %s (%s): %s", m["id"], key, e)
                    entry[key] = {"count": r.get("count"), "users": None}
                    continue
                entry[key] = {"count": r.get("count"), "users": [
                    {"id": u.get("id"), "username": u.get("username"), "global_name": u.get("global_name")}
                    for u in users]}
                if delay > 0:
                    time.sleep(delay)
            prog.update(i)
            if i % 25 == 0:
                atomic_write(cache_path, json.dumps(cache, ensure_ascii=False))
    finally:
        atomic_write(cache_path, json.dumps(cache, ensure_ascii=False))
    prog.done()
    return cache


@dataclass
class MediaItem:
    rel: str
    urls: List[str]
    size: Optional[int] = None


def collect_media(msgs: List[dict], level: str, res: Resolver) -> Dict[str, MediaItem]:
    items: Dict[str, MediaItem] = {}

    def add(rel: Optional[str], urls: Iterable[Optional[str]], size: Optional[int] = None) -> None:
        urls = [u for u in urls if u]
        if rel and urls and rel not in items:
            items[rel] = MediaItem(rel, urls, size)

    emoji_seen: Dict[str, bool] = {}
    for m in msgs:
        mid = m["id"]
        for a in iter_attachments(m):
            add(attachment_rel(mid, a), [a.get("url"), a.get("proxy_url")], a.get("size"))
        if level != "all":
            continue
        for e in iter_embeds(m):
            for key in ("image", "thumbnail"):
                x = e.get(key)
                url = embed_img_url(x)
                if url:
                    add(embed_img_rel(mid, url), [(x or {}).get("proxy_url"), (x or {}).get("url")])
        for st in m.get("sticker_items") or m.get("stickers") or []:
            if st.get("id") and st.get("format_type") != 3:
                add(sticker_rel(st), [sticker_url(st)])
        for r in m.get("reactions") or []:
            e = r.get("emoji") or {}
            if e.get("id"):
                emoji_seen[e["id"]] = bool(e.get("animated"))
        for mm in RE_EMOJI.finditer(m.get("content") or ""):
            emoji_seen[mm.group(3)] = bool(mm.group(1))
    for eid, animated in emoji_seen.items():
        add(emoji_rel(eid, animated), [emoji_url(eid, animated)])
    if level == "all":
        for u in res.users.values():
            add(avatar_rel(u), [avatar_url(u)])
    return items


class _TooBig(Exception):
    pass


def _is_discord_cdn(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("discordapp.com") or host.endswith("discordapp.net") or host.endswith("discord.com")


def _scrub(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


class MediaDownloader:
    def __init__(self, root: Path, api: Optional[DiscordAPI], workers: int = 6,
                 max_bytes: Optional[int] = None):
        self.root, self.api, self.workers, self.max_bytes = root, api, max(1, workers), max_bytes
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"

    def _get_to_file(self, url: str, dest: Path) -> Any:
        tmp = dest.with_name(dest.name + ".part")
        last: Any = "no attempt"
        for attempt in range(3):
            try:
                with self.session.get(url, stream=True, timeout=(10, 60)) as r:
                    if r.status_code in (403, 404, 410):
                        return r.status_code
                    if r.status_code == 429:
                        try:
                            time.sleep(min(30.0, float(r.headers.get("Retry-After", 2))))
                        except ValueError:
                            time.sleep(2)
                        last = 429
                        continue
                    if r.status_code >= 500:
                        last = r.status_code
                        time.sleep(2 ** attempt)
                        continue
                    if not r.ok:
                        return r.status_code
                    cl = r.headers.get("Content-Length")
                    if self.max_bytes and cl and cl.isdigit() and int(cl) > self.max_bytes:
                        return "too_big"
                    size = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(65536):
                            size += len(chunk)
                            if self.max_bytes and size > self.max_bytes:
                                raise _TooBig()
                            f.write(chunk)
                os.replace(tmp, dest)
                return "ok"
            except _TooBig:
                tmp.unlink(missing_ok=True)
                return "too_big"
            except (requests.RequestException, OSError) as e:
                last = e.__class__.__name__
                tmp.unlink(missing_ok=True)
                time.sleep(2 ** attempt)
        return last

    def _download_one(self, item: MediaItem) -> str:
        dest = self.root / item.rel
        if dest.exists() and dest.stat().st_size > 0:
            return "skipped"
        if item.size and self.max_bytes and item.size > self.max_bytes:
            return "too_big"
        dest.parent.mkdir(parents=True, exist_ok=True)
        res: Any = "no urls"
        for url in item.urls:
            res = self._get_to_file(url, dest)
            if res in ("ok", "too_big"):
                return res
            if res in (403, 404, 410) and self.api and _is_discord_cdn(url):
                fresh = self.api.refresh_url(url)
                if fresh and fresh != url:
                    res = self._get_to_file(fresh, dest)
                    if res in ("ok", "too_big"):
                        return res
        log.warning("Could not download %s (%s)", _scrub(item.urls[0]), res)
        return "failed"

    def run(self, items: Dict[str, MediaItem]) -> Counter:
        stats: Counter = Counter()
        if not items:
            return stats
        prog = Progress("Media", len(items), "files")
        ex = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futs = [ex.submit(self._download_one, it) for it in items.values()]
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    stats[fut.result()] += 1
                except Exception as e:
                    log.warning("Download error: %s", e)
                    stats["failed"] += 1
                prog.update(i)
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        ex.shutdown(wait=True)
        prog.done(f"{stats['ok']} new, {stats['skipped']} already had, {stats['too_big']} too big, "
                  f"{stats['failed']} failed")
        return stats


_STOPWORDS = set("""the and for are but not you your yours with this that have has had was were been from they them
their then than there here what when who how why will would could should can just its it's i'm im dont don't
about into out all one get got like yes yeah okay ok lol also very too only some any more most much such
bir ve bu da de ne mi mu ben sen var yok için ile çok gibi daha ama""".split())
_RE_WORD = re.compile(r"[^\W\d_]{3,}", re.U)
_RE_URL_PLAIN = re.compile(r"https?://\S+")
_RE_UNICODE_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F2FF]")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def compute_stats(msgs: List[dict]) -> dict:
    by_author: Counter = Counter()
    names: Dict[str, str] = {}
    by_day: Counter = Counter()
    by_hour = [0] * 24
    by_wd = [0] * 7
    words: Counter = Counter()
    emoji: Counter = Counter()
    n_att = n_links = n_replies = n_edited = chars = 0
    for m in msgs:
        if m.get("system_text") and m["type"] not in CONTENT_TYPES:
            continue
        aid = m["author"]["id"] or "?"
        by_author[aid] += 1
        names[aid] = m["author"]["display_name"]
        day = m["local_time"][:10]
        by_day[day] += 1
        by_hour[int(m["local_time"][11:13])] += 1
        try:
            by_wd[date.fromisoformat(day).weekday()] += 1
        except ValueError:
            pass
        text = m["content_text"]
        chars += len(text)
        n_att += len(m["attachments"])
        n_links += len(_RE_URL_PLAIN.findall(text))
        n_replies += 1 if m["reply_to"] else 0
        n_edited += 1 if m["edited_timestamp"] else 0
        for w in _RE_WORD.findall(_RE_URL_PLAIN.sub(" ", RE_EMOJI.sub(" ", text)).lower()):
            if w not in _STOPWORDS:
                words[w] += 1
        for mm in RE_EMOJI.finditer(m["content"]):
            emoji[f":{mm.group(2)}:"] += 1
        for ch in _RE_UNICODE_EMOJI.findall(text):
            emoji[ch] += 1
        for r in m["reactions"]:
            emoji[r["emoji"]["display"] or "?"] += r["count"]
    total = sum(by_author.values())
    days = sorted(by_day)
    span_days = ((date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1) if days else 0
    return {
        "total": total,
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "span_days": span_days,
        "active_days": len(days),
        "avg_per_active_day": round(total / len(days), 1) if days else 0,
        "avg_chars": round(chars / total, 1) if total else 0,
        "attachments": n_att, "links": n_links, "replies": n_replies, "edited": n_edited,
        "authors": [{"id": a, "name": names[a], "count": c, "share": round(100 * c / total, 1)}
                    for a, c in by_author.most_common(25)],
        "by_hour": by_hour,
        "by_weekday": dict(zip(WEEKDAYS, by_wd)),
        "top_days": [{"day": d, "count": c} for d, c in by_day.most_common(10)],
        "top_emoji": [{"emoji": e, "count": c} for e, c in emoji.most_common(15)],
        "top_words": [{"word": w, "count": c} for w, c in words.most_common(25)],
    }


def stats_html(s: dict) -> str:
    esc = html.escape
    if not s["total"]:
        return "<p>No messages in range.</p>"

    def bars(rows: List[Tuple[str, int]], label_w: str = "9em") -> str:
        top = max((c for _, c in rows), default=1) or 1
        return "".join(
            f'<div class="brow"><span class="bl" style="width:{label_w}">{esc(str(l))}</span>'
            f'<span class="bar"><i style="width:{100 * c / top:.1f}%"></i></span><span class="bn">{c:,}</span></div>'
            for l, c in rows)

    return (
        f'<div class="statgrid">'
        f'<div class="card"><h3>Overview</h3><table>'
        f'<tr><td>Messages</td><td>{s["total"]:,}</td></tr>'
        f'<tr><td>Period</td><td>{esc(str(s["first_day"]))} to {esc(str(s["last_day"]))} ({s["span_days"]:,} days)</td></tr>'
        f'<tr><td>Active days</td><td>{s["active_days"]:,}</td></tr>'
        f'<tr><td>Avg per active day</td><td>{s["avg_per_active_day"]}</td></tr>'
        f'<tr><td>Avg message length</td><td>{s["avg_chars"]} chars</td></tr>'
        f'<tr><td>Attachments / links</td><td>{s["attachments"]:,} / {s["links"]:,}</td></tr>'
        f'<tr><td>Replies / edited</td><td>{s["replies"]:,} / {s["edited"]:,}</td></tr></table></div>'
        f'<div class="card"><h3>Who talks most</h3>{bars([(a["name"], a["count"]) for a in s["authors"][:10]])}</div>'
        f'<div class="card"><h3>By hour of day</h3>{bars([(f"{h:02d}:00", c) for h, c in enumerate(s["by_hour"])], "4em")}</div>'
        f'<div class="card"><h3>By weekday</h3>{bars(list(s["by_weekday"].items()), "4em")}</div>'
        f'<div class="card"><h3>Busiest days</h3>{bars([(d["day"], d["count"]) for d in s["top_days"]])}</div>'
        f'<div class="card"><h3>Top emoji</h3>{bars([(e["emoji"], e["count"]) for e in s["top_emoji"]], "6em")}</div>'
        f'<div class="card"><h3>Top words</h3>{bars([(w["word"], w["count"]) for w in s["top_words"][:15]])}</div>'
        f'</div>')


def build_model(job_info: dict, msgs_raw: List[dict], ctx: CleanContext, opt_info: dict) -> dict:
    cleaned = [clean_message(m, ctx) for m in msgs_raw]
    return {
        "export": {"tool": "discord_export.py", "version": __version__,
                   "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "timezone": tz_label(ctx.tz)},
        "channel": job_info,
        "range": opt_info,
        "message_count": len(cleaned),
        "stats": compute_stats(cleaned),
        "users": {uid: {"id": uid, "username": u.get("username"), "global_name": u.get("global_name"),
                        "display_name": ctx.res.user_name(uid), "bot": bool(u.get("bot")),
                        "avatar_url": avatar_url(u)} for uid, u in ctx.res.users.items()},
        "messages": cleaned,
    }


def _summary_line(m: dict) -> str:
    return m["system_text"] if (m["system_text"] and m["type"] not in CONTENT_TYPES) else m["content_text"]


def write_json(path: Path, model: dict) -> None:
    atomic_write(path, json.dumps(model, ensure_ascii=False, indent=2))


def write_jsonl(path: Path, model: dict) -> None:
    atomic_write(path, "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in model["messages"]))


def _csv_safe(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def write_csv(path: Path, model: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    cols = ["id", "timestamp_utc", "local_time", "author_id", "author_username", "author_name", "type",
            "content", "reply_to_id", "reply_to_author", "pinned", "edited", "attachments",
            "embeds", "stickers", "reactions"]
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for m in model["messages"]:
            att = "; ".join(a["local_path"] or a["url"] or a["filename"] or "" for a in m["attachments"])
            rx = "; ".join(f'{r["emoji"]["display"]} x{r["count"]}' for r in m["reactions"])
            w.writerow([_csv_safe(v) for v in [
                m["id"], m["timestamp"], m["local_time"], m["author"]["id"], m["author"]["username"],
                m["author"]["display_name"], m["type_name"], _summary_line(m),
                (m["reply_to"] or {}).get("message_id", ""), (m["reply_to"] or {}).get("author", ""),
                int(m["pinned"]), m["edited_timestamp"] or "", att, len(m["embeds"]),
                "; ".join(s["name"] or "" for s in m["stickers"]), rx]])
    os.replace(tmp, path)


def write_txt(path: Path, model: dict) -> None:
    ch = model["channel"]
    out = [f"Channel: {ch.get('label')}  (id {ch.get('id')})",
           f"Exported: {model['export']['exported_at']}  |  Times in {model['export']['timezone']}  |  "
           f"{model['message_count']:,} messages", ""]
    prev_day = None
    for m in model["messages"]:
        day, hhmm = m["local_time"][:10], m["local_time"][11:19]
        if day != prev_day:
            out += ["", f"===== {day} =====", ""]
            prev_day = day
        pad = " " * (len(hhmm) + 3)
        body = _summary_line(m) or ""
        if m["system_text"] and m["type"] not in CONTENT_TYPES:
            out.append(f"[{hhmm}] * {body}")
            continue
        lines = body.split("\n") if body else [""]
        flags = (" [pinned]" if m["pinned"] else "") + (" (edited)" if m["edited_timestamp"] else "")
        out.append(f"[{hhmm}] {m['author']['display_name']}{flags}: {lines[0]}")
        out += [pad + ln for ln in lines[1:]]
        if m["reply_to"]:
            r = m["reply_to"]
            what = "[deleted message]" if r["deleted"] else (r["content_preview"] or "")[:100].replace("\n", " ")
            out.append(f'{pad}> replying to {r["author"] or "?"}: {what}')
        if m["forwarded"]:
            f = m["forwarded"]
            out.append(f"{pad}>> forwarded: " + (f["content_text"] or "").replace("\n", " ")[:200])
            out += [f"{pad}   [file] {a['filename']} ({a['local_path'] or a['url']})" for a in f["attachments"]]
        for a in m["attachments"]:
            out.append(f"{pad}[file] {a['filename']} ({human_size(a['size'])}) {a['local_path'] or a['url']}")
        for e in m["embeds"]:
            bits = [x for x in (e.get("title"), e.get("description"), e.get("url")) if x]
            if bits:
                out.append(f"{pad}[embed] " + " | ".join(b.replace("\n", " ")[:160] for b in bits))
        for s in m["stickers"]:
            out.append(f"{pad}[sticker] {s['name']}")
        if m["poll"]:
            p = m["poll"]
            out.append(f"{pad}[poll] {p['question']}: " + ", ".join(
                f"{a['text']} ({a['votes'] if a['votes'] is not None else '?'})" for a in p["answers"]))
        if m["reactions"]:
            out.append(f"{pad}[reactions] " + "  ".join(f"{r['emoji']['display']} {r['count']}" for r in m["reactions"]))
    atomic_write(path, "\n".join(out) + "\n")


_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".svg"}
_VID_EXT = {".mp4", ".webm", ".mov", ".m4v", ".ogv"}
_AUD_EXT = {".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus"}


def _kind(filename: Optional[str], ctype: Optional[str]) -> str:
    ct = (ctype or "").split(";")[0].lower()
    ext = os.path.splitext(filename or "")[1].lower()
    if ct.startswith("image/") or ext in _IMG_EXT:
        return "image"
    if ct.startswith("video/") or ext in _VID_EXT:
        return "video"
    if ct.startswith("audio/") or ext in _AUD_EXT:
        return "audio"
    return "file"


def _href(local: Optional[str], remote: Optional[str]) -> Optional[str]:
    if local:
        return quote(local, safe="/")
    if remote and remote.lower().startswith(("http://", "https://")):
        return html.escape(remote, quote=True)
    return None


def _att_html(a: dict) -> str:
    esc = html.escape
    href = _href(a["local_path"], a["url"])
    name = esc(a["filename"] or "file")
    if not href:
        return f'<div class="file">{name}</div>'
    k = _kind(a["filename"], a["content_type"])
    note = "" if a["local_path"] else ' <span class="muted">(remote link, may expire)</span>'
    alt = esc(a["description"] or a["filename"] or "")
    if k == "image":
        return (f'<a href="{href}" target="_blank" rel="noopener"><img class="media" src="{href}" '
                f'alt="{alt}" loading="lazy"></a>{note}')
    if k == "video":
        return f'<video class="media" src="{href}" controls preload="metadata"></video>{note}'
    if k == "audio":
        return f'<audio src="{href}" controls preload="none"></audio> <span class="muted">{name}</span>{note}'
    return (f'<a class="file" href="{href}" target="_blank" rel="noopener">&#128196; {name} '
            f'<span class="muted">{esc(human_size(a["size"]))}</span></a>{note}')


def _embed_media(x: Optional[dict], cls: str) -> str:
    if not x:
        return ""
    href = _href(x.get("local_path"), x.get("proxy_url") or x.get("url"))
    return f'<img class="{cls}" src="{href}" loading="lazy" alt="">' if href else ""


def _embed_html(e: dict, res: Resolver, emoji_src) -> str:
    esc = html.escape
    color = e.get("color")
    style = f' style="border-left-color:#{int(color):06x}"' if isinstance(color, int) else ""
    parts = []
    if e.get("provider") and e["provider"].get("name") and e.get("type") != "rich":
        parts.append(f'<div class="e-prov">{esc(e["provider"]["name"])}</div>')
    au = e.get("author")
    if au and au.get("name"):
        parts.append(f'<div class="e-author">{esc(au["name"])}</div>')
    if e.get("title"):
        t = esc(e["title"])
        u = _href(None, e.get("url"))
        parts.append(f'<div class="e-title"><a href="{u}" target="_blank" rel="noopener noreferrer">{t}</a></div>' if u
                     else f'<div class="e-title">{t}</div>')
    if e.get("description"):
        parts.append(f'<div class="e-desc">{render_html(e["description"], res, emoji_src)}</div>')
    if e.get("fields"):
        fl = "".join(f'<div class="e-field{" inline" if f["inline"] else ""}"><b>{esc(f["name"] or "")}</b>'
                     f'<div>{render_html(f["value"] or "", res, emoji_src)}</div></div>' for f in e["fields"])
        parts.append(f'<div class="e-fields">{fl}</div>')
    parts.append(_embed_media(e.get("image"), "e-img"))
    parts.append(_embed_media(e.get("thumbnail"), "e-thumb") if not e.get("image") else "")
    v = e.get("video")
    if v and v.get("url") and e.get("type") not in ("gifv", "image") and not e.get("image") and not e.get("thumbnail"):
        vu = _href(None, v["url"])
        if vu:
            parts.append(f'<div><a href="{vu}" target="_blank" rel="noopener noreferrer">&#9654; video</a></div>')
    foot = []
    if e.get("footer") and e["footer"].get("text"):
        foot.append(esc(e["footer"]["text"]))
    if e.get("timestamp"):
        foot.append(esc(str(e["timestamp"])[:16].replace("T", " ")))
    if foot:
        parts.append(f'<div class="e-foot">{" | ".join(foot)}</div>')
    return f'<div class="embed"{style}>{"".join(p for p in parts if p)}</div>'


def message_html(m: dict, ctx: CleanContext) -> str:
    esc = html.escape
    res = ctx.res

    def emoji_src(eid: str, animated: bool) -> str:
        return quote(ctx.local(emoji_rel(eid, animated)) or "", safe="/") or emoji_url(eid, animated)

    a = m["author"]
    hue = (int(a["id"]) % 360) if a["id"] and str(a["id"]).isdigit() else 200
    av_href = _href(a["avatar_local"], a["avatar_url"])
    initial = esc((a["display_name"] or "?")[:1].upper())
    avatar = (f'<img class="av" src="{av_href}" alt="" loading="lazy">' if av_href
              else f'<div class="av ph" style="background:hsl({hue} 45% 40%)">{initial}</div>')
    mid = esc(m["id"])
    if m["system_text"] and m["type"] not in CONTENT_TYPES:
        return (f'<div class="msg sys" id="m-{mid}"><div class="sysline">&#8594; {esc(m["system_text"])} '
                f'<span class="time">{esc(m["local_time"][11:16])}</span></div></div>')

    bits: List[str] = []
    r = m["reply_to"]
    if r:
        if r["deleted"]:
            bits.append('<div class="reply muted">&#8618; Original message was deleted</div>')
        else:
            prev = esc((r["content_preview"] or "")[:120])
            bits.append(f'<div class="reply"><a href="#m-{esc(r["message_id"])}" data-jump="{esc(r["message_id"])}">'
                        f'&#8618; <b>{esc(r["author"] or "?")}</b> {prev}</a></div>')
    flags = ""
    if m["pinned"]:
        flags += ' <span title="pinned">&#128204;</span>'
    if m["edited_timestamp"]:
        flags += ' <span class="muted" title="edited">(edited)</span>'
    if a["bot"]:
        flags += ' <span class="tag">BOT</span>'
    bits.append(f'<div class="hdr"><span class="name" style="--h:{hue}">{esc(a["display_name"])}</span> '
                f'<span class="time" title="{esc(m["timestamp"])}">{esc(m["local_time"][11:16])}</span>{flags}</div>')
    if m["content"]:
        bits.append(f'<div class="content">{render_html(m["content"], res, emoji_src)}</div>')
    if m["forwarded"]:
        f = m["forwarded"]
        inner = render_html(f["content"], res, emoji_src)
        inner += "".join(_att_html(x) for x in f["attachments"]) + "".join(_embed_html(x, res, emoji_src) for x in f["embeds"])
        bits.append(f'<div class="fwd"><div class="muted">&#8618; Forwarded</div>{inner}</div>')
    bits += [_att_html(x) for x in m["attachments"]]
    bits += [_embed_html(x, res, emoji_src) for x in m["embeds"]]
    for s in m["stickers"]:
        href = _href(s["local_path"], s["url"] if s["format_type"] != 3 else None)
        bits.append(f'<img class="sticker" src="{href}" alt="{esc(s["name"] or "sticker")}" loading="lazy">' if href
                    else f'<div class="muted">[sticker: {esc(s["name"] or "?")}]</div>')
    if m["poll"]:
        p = m["poll"]
        rows = "".join(f'<li>{esc((x["emoji"] or "") + " " + (x["text"] or ""))} '
                       f'<span class="muted">{"" if x["votes"] is None else str(x["votes"]) + " votes"}</span></li>'
                       for x in p["answers"])
        bits.append(f'<div class="poll"><b>&#128202; {esc(p["question"] or "Poll")}</b><ul>{rows}</ul></div>')
    if m["thread"]:
        bits.append(f'<div class="muted">&#129525; Thread: {esc(m["thread"]["name"] or "")}</div>')
    if m["reactions"]:
        chips = []
        for rx in m["reactions"]:
            e = rx["emoji"]
            if e["id"]:
                src = _href(e["local_path"], emoji_url(e["id"], e["animated"]))
                sym = f'<img class="emoji" src="{src}" alt="{esc(e["display"] or "")}">'
            else:
                sym = esc(e["name"] or "?")
            who = ""
            if rx.get("users"):
                who = ' title="' + esc(", ".join(u.get("global_name") or u.get("username") or "?" for u in rx["users"][:50])) + '"'
            chips.append(f'<span class="rx"{who}>{sym} {rx["count"]}</span>')
        bits.append(f'<div class="rxs">{"".join(chips)}</div>')
    return f'<div class="msg" id="m-{mid}">{avatar}<div class="body">{"".join(bits)}</div></div>'


def _search_text(m: dict) -> str:
    parts = [m["author"]["display_name"] or "", m["author"]["username"] or "", _summary_line(m) or ""]
    parts += [a["filename"] or "" for a in m["attachments"]]
    for e in m["embeds"]:
        parts += [e.get("title") or "", e.get("description") or "", e.get("url") or ""]
    if m["poll"]:
        parts.append(m["poll"]["question"] or "")
    if m["forwarded"]:
        parts.append(m["forwarded"]["content_text"])
    return " ".join(parts).lower()


def _json_for_script(obj: Any) -> str:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


VIEWER_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src * data: blob:; media-src * data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>__TITLE__</title>
<style>
:root{--nl:70%;--bg:#313338;--bg2:#2b2d31;--bg3:#1e1f22;--fg:#dbdee1;--muted:#949ba4;--link:#00a8fc;--accent:#5865f2;--line:#3f4147;--mention:#5865f240}
@media (prefers-color-scheme:light){:root{--nl:36%;--bg:#fff;--bg2:#f2f3f5;--bg3:#e3e5e8;--fg:#313338;--muted:#5c5e66;--link:#006ce7;--line:#d9dadd;--mention:#5865f230}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 "Segoe UI Variable Text","Segoe UI",Inter,system-ui,-apple-system,Roboto,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--bg2);border-bottom:1px solid var(--line);padding:10px 16px}
header h1{margin:0 0 8px;font-size:16px}header .sub{color:var(--muted);font-weight:400;font-size:13px}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select,button{background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font:inherit}
input[type=search]{min-width:220px;flex:1}button{cursor:pointer}button:hover{border-color:var(--accent)}
#count{color:var(--muted);font-size:13px}
#stats{padding:12px 16px;background:var(--bg2);border-bottom:1px solid var(--line)}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.card h3{margin:0 0 8px;font-size:14px}
.card table td{padding:2px 10px 2px 0}.card table td:first-child{color:var(--muted)}
.brow{display:flex;align-items:center;gap:6px;font-size:12px;margin:2px 0}.bl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:none}
.bar{flex:1;background:var(--bg3);border-radius:3px;height:10px}.bar i{display:block;height:100%;background:var(--accent);border-radius:3px}.bn{width:3.5em;text-align:right;color:var(--muted)}
#list{max-width:980px;margin:0 auto;padding:8px 16px 60px}
.day{display:flex;align-items:center;gap:10px;margin:18px 0 6px;color:var(--muted);font-size:12px}.day:before,.day:after{content:"";flex:1;border-top:1px solid var(--line)}
.msg{display:flex;gap:12px;padding:5px 8px;border-radius:6px}.msg:hover{background:var(--bg2)}.msg.hl{background:var(--mention);outline:1px solid var(--accent)}
.av{width:40px;height:40px;border-radius:50%;flex:none;object-fit:cover}.av.ph{display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600}
.body{min-width:0;flex:1}.hdr{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.name{font-weight:600;color:hsl(var(--h) 65% var(--nl))}.time,.muted{color:var(--muted);font-size:12px}
.tag{background:var(--accent);color:#fff;font-size:10px;border-radius:3px;padding:0 4px}
.content{overflow-wrap:anywhere}.reply{font-size:12.5px;color:var(--muted);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.reply a{color:var(--muted)}.sysline{color:var(--muted);font-size:13px;padding:2px 52px}
.mention{background:var(--mention);border-radius:3px;padding:0 2px}.emoji{width:22px;height:22px;vertical-align:middle}
.spoiler{background:var(--bg3);color:transparent;border-radius:3px;cursor:pointer}.spoiler.shown{color:inherit;background:var(--mention)}
blockquote{margin:2px 0;padding-left:10px;border-left:4px solid var(--line)}pre{background:var(--bg3);padding:8px;border-radius:6px;overflow:auto;margin:4px 0}
code{background:var(--bg3);padding:0 4px;border-radius:3px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}pre code{padding:0;background:none}
.h{font-weight:700}.h1{font-size:1.5em}.h2{font-size:1.25em}.h3{font-size:1.1em}.sub{font-size:12px;color:var(--muted)}
.media{max-width:min(420px,100%);max-height:360px;border-radius:6px;margin:4px 0;display:block}video.media{background:#000}
.sticker{width:128px;height:128px;object-fit:contain}.file{display:inline-block;background:var(--bg2);border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:3px 0}
.embed{border-left:4px solid var(--line);background:var(--bg2);border-radius:4px;padding:8px 12px;margin:4px 0;max-width:520px}
.e-prov,.e-foot{font-size:12px;color:var(--muted)}.e-author{font-weight:600;font-size:13px}.e-title{font-weight:600}.e-desc{font-size:14px}
.e-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:6px;margin-top:6px}.e-field.inline{}.e-field:not(.inline){grid-column:1/-1}
.e-img{max-width:100%;border-radius:4px;margin-top:6px}.e-thumb{max-width:80px;float:right;border-radius:4px}
.fwd{border-left:3px solid var(--muted);padding-left:10px;margin:3px 0}.poll{background:var(--bg2);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:4px 0;max-width:420px}
.rxs{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}.rx{background:var(--bg2);border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:13px}
.more{display:block;margin:10px auto}
</style></head><body>
<header>
 <h1>__TITLE__ <span class="sub">__SUB__</span></h1>
 <div class="tools">
  <input id="q" type="search" placeholder="Search messages (all words must match)...">
  <select id="who"><option value="">Everyone</option></select>
  <input id="from" type="date" title="From"><input id="to" type="date" title="To">
  <button id="clear">Clear</button><button id="tstats">Stats</button><span id="count"></span>
 </div>
</header>
<div id="stats" hidden>__STATS__</div>
<div id="list"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
 var D=JSON.parse(document.getElementById('data').textContent), ALL=D.messages, view=ALL, lo=0, hi=0, PAGE=300;
 var list=document.getElementById('list'), q=document.getElementById('q'), who=document.getElementById('who'),
     from=document.getElementById('from'), to=document.getElementById('to'), count=document.getElementById('count');
 D.authors.forEach(function(a){var o=document.createElement('option');o.value=a.id;o.textContent=a.name+' ('+a.n+')';who.appendChild(o);});
 function render(){
  var out=[],prev=null;
  if(lo>0)out.push('<button class="more" data-more="up">Load earlier messages ('+lo+' more)</button>');
  for(var i=lo;i<hi;i++){var m=view[i];if(m.d!==prev){out.push('<div class="day"><span>'+m.d+'</span></div>');prev=m.d;}out.push(m.h);}
  if(hi<view.length)out.push('<button class="more" data-more="down">Load later messages ('+(view.length-hi)+' more)</button>');
  if(!view.length)out.push('<p class="muted" style="text-align:center">No messages match.</p>');
  list.innerHTML=out.join('');
  count.textContent=view.length.toLocaleString()+' of '+ALL.length.toLocaleString()+' messages';
 }
 function apply(){
  var words=q.value.toLowerCase().split(/\s+/).filter(Boolean), a=who.value, f=from.value, t=to.value;
  view=ALL.filter(function(m){
   if(a&&m.a!==a)return false; if(f&&m.d<f)return false; if(t&&m.d>t)return false;
   for(var i=0;i<words.length;i++)if(m.t.indexOf(words[i])<0)return false; return true;});
  lo=0; hi=Math.min(view.length,PAGE); render(); window.scrollTo(0,0);
 }
 function jump(id){
  var idx=-1; for(var i=0;i<view.length;i++)if(view[i].id===id){idx=i;break;}
  if(idx<0){q.value='';who.value='';from.value='';to.value='';view=ALL;for(var j=0;j<view.length;j++)if(view[j].id===id){idx=j;break;}}
  if(idx<0)return; lo=Math.max(0,idx-100); hi=Math.min(view.length,idx+200); render();
  var el=document.getElementById('m-'+id); if(el){el.scrollIntoView({block:'center'});el.classList.add('hl');setTimeout(function(){el.classList.remove('hl')},2500);}
 }
 list.addEventListener('click',function(e){
  var j=e.target.closest('[data-jump]'); if(j){e.preventDefault();jump(j.getAttribute('data-jump'));return;}
  var s=e.target.closest('.spoiler'); if(s){s.classList.toggle('shown');return;}
  var b=e.target.closest('[data-more]'); if(b){var h0=document.body.scrollHeight,y=window.scrollY;
   if(b.getAttribute('data-more')==='up'){lo=Math.max(0,lo-PAGE);render();window.scrollTo(0,y+document.body.scrollHeight-h0);}
   else{hi=Math.min(view.length,hi+PAGE);render();window.scrollTo(0,y);}}
 });
 list.addEventListener('error',function(e){var t=e.target; if(t&&t.tagName==='IMG'&&!t.dataset.failed){t.dataset.failed='1';
  var s=document.createElement('span');s.className='muted';s.textContent='['+(t.alt||'image unavailable')+']';t.replaceWith(s);}},true);
 var timer; q.addEventListener('input',function(){clearTimeout(timer);timer=setTimeout(function(){apply()},200)});
 [who,from,to].forEach(function(x){x.addEventListener('change',function(){apply()})});
 document.getElementById('clear').addEventListener('click',function(){q.value='';who.value='';from.value='';to.value='';apply()});
 document.getElementById('tstats').addEventListener('click',function(){var s=document.getElementById('stats');s.hidden=!s.hidden});
 apply();
 var loading=false; window.addEventListener('scroll',function(){
  if(loading||hi>=view.length)return;
  if(window.innerHeight+window.scrollY>=document.body.scrollHeight-700){loading=true;hi=Math.min(view.length,hi+PAGE);render();loading=false;}
 },{passive:true});
 function fromHash(){var h=location.hash.match(/^#m-(\d+)$/); if(h)jump(h[1]);}
 window.addEventListener('hashchange',fromHash); fromHash();
})();
</script></body></html>
"""


def write_html(path: Path, model: dict, ctx: CleanContext) -> None:
    esc = html.escape
    msgs = model["messages"]
    authors = Counter()
    names: Dict[str, str] = {}
    payload = []
    for m in msgs:
        aid = m["author"]["id"] or "?"
        if m["type"] in CONTENT_TYPES:
            authors[aid] += 1
            names[aid] = m["author"]["display_name"]
        payload.append({"id": m["id"], "d": m["local_time"][:10], "a": aid,
                        "t": _search_text(m), "h": message_html(m, ctx)})
    data = {"messages": payload,
            "authors": [{"id": a, "name": names[a], "n": n} for a, n in authors.most_common()]}
    ch = model["channel"]
    sub = f'{model["message_count"]:,} messages | exported {model["export"]["exported_at"][:16].replace("T", " ")} UTC | times in {model["export"]["timezone"]}'
    vals = {"TITLE": esc(ch.get("label") or ch.get("id") or "Discord export"),
            "SUB": esc(sub), "STATS": stats_html(model["stats"])}
    before, after = VIEWER_TEMPLATE.split("__DATA__")
    before = re.sub(r"__(TITLE|SUB|STATS)__", lambda m: vals[m.group(1)], before)
    atomic_write(path, before + _json_for_script(data) + after)


WRITERS = {"json": ("chat.json", write_json), "jsonl": ("chat.jsonl", write_jsonl),
           "csv": ("chat.csv", write_csv), "txt": ("chat.txt", write_txt)}
ALL_FORMATS = ["json", "jsonl", "csv", "txt", "html"]


@dataclass
class Job:
    channel: dict
    label: str
    guild_id: Optional[str] = None
    guild_name: Optional[str] = None
    dir: Path = Path(".")


def channel_label(ch: dict, guild_name: Optional[str] = None) -> str:
    t = ch.get("type")
    recips = ch.get("recipients") or []
    if t == CH_DM:
        return "DM with " + (user_display(recips[0]) if recips else str(ch.get("id")))
    if t == CH_GROUP_DM:
        return ch.get("name") or ("Group: " + ", ".join(user_display(r) for r in recips) if recips else f"Group {ch.get('id')}")
    name = ch.get("name") or str(ch.get("id"))
    prefix = f"{guild_name} / " if guild_name else ""
    return f"{prefix}{'thread: ' if t in THREAD_TYPES else '#'}{name}"


class Catalog:
    def __init__(self, api: DiscordAPI):
        self.api = api
        self.guild_names: Dict[str, Optional[str]] = {}
        self.active_threads: Dict[str, List[dict]] = {}

    def guild_name(self, gid: Optional[str]) -> Optional[str]:
        if not gid:
            return None
        if gid not in self.guild_names:
            try:
                self.guild_names[gid] = self.api.guild(gid).get("name")
            except ApiError as e:
                log.debug("Could not read guild %s: %s", gid, e)
                self.guild_names[gid] = None
        return self.guild_names[gid]

    def make_job(self, ch: dict, out: Path, base: Optional[Path] = None) -> Job:
        gid = ch.get("guild_id")
        gname = self.guild_name(gid)
        label = channel_label(ch, gname)
        folder = safe_filename(label.replace(" / ", "-").replace("#", "").replace("thread: ", "thread-"),
                               maxlen=60) + "-" + str(ch["id"])
        return Job(ch, label, gid, gname, (base or out) / folder)

    def threads_of(self, ch: dict) -> List[dict]:
        gid = ch.get("guild_id")
        if not gid or ch.get("type") not in THREAD_PARENT_TYPES:
            return []
        if gid not in self.active_threads:
            try:
                self.active_threads[gid] = self.api.active_threads(gid)
            except ApiError as e:
                log.warning("Could not list active threads for guild %s: %s", gid, e)
                self.active_threads[gid] = []
        found = [t for t in self.active_threads[gid] if t.get("parent_id") == ch["id"]]
        try:
            found += self.api.archived_threads(ch["id"])
        except ApiError as e:
            log.warning("Could not list archived threads for #%s: %s", ch.get("name"), e)
        seen, out = set(), []
        for t in found:
            if t["id"] not in seen:
                seen.add(t["id"])
                t.setdefault("guild_id", gid)
                out.append(t)
        return out


def guild_text_channels(api: DiscordAPI, gid: str) -> List[dict]:
    chans = [c for c in api.guild_channels(gid)
             if c.get("type") in TEXTLIKE_GUILD_TYPES | {CH_FORUM, CH_MEDIA}]
    for c in chans:
        c.setdefault("guild_id", gid)
    return sorted(chans, key=lambda c: (c.get("position", 0), int(c["id"])))


def _parse_selection(raw: str, n: int) -> Optional[List[int]]:
    out: List[int] = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= a <= b <= n):
            return None
        out += list(range(a - 1, b))
    return sorted(set(out)) or None


def pick(items: List[str], prompt: str, multi: bool = True) -> List[int]:
    for i, it in enumerate(items, 1):
        say(f"  {i:3d}) {it}")
    hint = "numbers like 1,3,5-8 or 'all'" if multi else "a number"
    while True:
        raw = input(f"{prompt} ({hint}, q to quit): ").strip().lower()
        if raw in ("q", "quit", "exit"):
            sys.exit(0)
        if multi and raw in ("a", "all"):
            return list(range(len(items)))
        sel = _parse_selection(raw, len(items))
        if sel and (multi or len(sel) == 1):
            return sel
        say("  Please try again.")


def interactive_select(api: DiscordAPI) -> List[dict]:
    while True:
        say("\nWhat do you want to export?\n   1) Direct messages / group chats\n   2) Channels from a server\n   q) Quit")
        choice = input("> ").strip().lower()
        if choice in ("q", "quit"):
            sys.exit(0)
        if choice == "1":
            dms = sorted(api.dm_channels(), key=lambda c: int(c.get("last_message_id") or 0), reverse=True)
            if not dms:
                say("No DMs found.")
                continue
            return [dms[i] for i in pick([channel_label(c) for c in dms], "Which chats")]
        if choice == "2":
            guilds = api.guilds()
            if not guilds:
                say("You're not in any servers.")
                continue
            g = guilds[pick([f"{g['name']}  ({g['id']})" for g in guilds], "Which server", multi=False)[0]]
            chans = guild_text_channels(api, g["id"])
            if not chans:
                say("No readable text channels there.")
                continue
            return [chans[i] for i in pick([f"#{c.get('name')}" for c in chans], "Which channels")]


def print_listing(api: DiscordAPI) -> None:
    say("\n== Direct messages / group chats ==")
    for c in sorted(api.dm_channels(), key=lambda c: int(c.get("last_message_id") or 0), reverse=True):
        say(f"  {c['id']:>20}  {channel_label(c)}")
    say("\n== Servers ==  (use  -g SERVER_ID  to export all its channels)")
    for g in api.guilds():
        say(f"  {g['id']:>20}  {g['name']}")


def process_job(api: DiscordAPI, job: Job, args: argparse.Namespace, tz: Optional[tzinfo],
                formats: List[str]) -> dict:
    ch, cid = job.channel, job.channel["id"]
    res = Resolver(tz)
    if job.guild_id:
        try:
            for r in api.guild_roles(job.guild_id):
                res.roles[r["id"]] = r["name"]
            for c in api.guild_channels(job.guild_id):
                res.channels[c["id"]] = c.get("name")
        except ApiError as e:
            log.warning("Could not load role/channel names for guild %s (%s); mentions may show as unknown",
                        job.guild_id, e)
    res.channels.setdefault(cid, ch.get("name"))
    for r in ch.get("recipients") or []:
        res.add_user(r)

    store = ChannelStore(job.dir)
    store.load()
    if args.fresh:
        store.reset()
    elif store.messages:
        say(f"  Resuming: {len(store.messages):,} messages already cached")

    opt = SyncOptions(limit=args.limit, after_id=args.after_id, before_id=args.before_id, delay=args.delay)
    prog = Progress("  Fetching", args.limit or None)
    if not args.limit or args.limit > 100:
        opt.total_hint = api.count_messages(cid, job.guild_id, min_id=args.after_id, max_id=args.before_id)
    try:
        new = sync_channel(api, cid, store, opt, prog)
    except (KeyboardInterrupt, ApiError):
        prog.done("stopped")
        raise
    prog.done(f"{new:,} new messages ({len(store.messages):,} cached)")

    def in_range(mid: int) -> bool:
        return (args.after_id is None or mid > args.after_id) and (args.before_id is None or mid < args.before_id)

    msgs = sorted((m for k, m in store.messages.items() if in_range(int(k))), key=lambda m: int(m["id"]))
    if args.limit:
        msgs = msgs[-args.limit:]

    reactors: Dict[str, Dict[str, Any]] = {}
    if args.reactors and any(m.get("reactions") for m in msgs):
        reactors = fetch_reactors(api, cid, msgs, job.dir / "reactors.json", args.delay)

    for m in msgs:
        res.feed(m)

    media_stats: Counter = Counter()
    if args.media != "none" and msgs:
        items = collect_media(msgs, args.media, res)
        dl = MediaDownloader(job.dir, api, args.workers,
                             int(args.max_media_mb * 1024 * 1024) if args.max_media_mb else None)
        media_stats = dl.run(items)

    ctx = CleanContext(res=res, tz=tz, root=job.dir, index=store.messages, reactors=reactors)
    info = {
        "id": cid, "label": job.label, "name": ch.get("name"), "type": ch.get("type"),
        "guild_id": job.guild_id, "guild_name": job.guild_name, "topic": ch.get("topic"),
        "recipients": [user_display(r) for r in ch.get("recipients") or []],
    }
    rng = {
        "limit": args.limit or None,
        "after": snowflake_to_datetime(args.after_id).isoformat() if args.after_id else None,
        "before": snowflake_to_datetime(args.before_id).isoformat() if args.before_id else None,
    }
    model = build_model(info, msgs, ctx, rng)
    written = []
    for fmt in formats:
        if fmt == "html":
            write_html(job.dir / "chat.html", model, ctx)
            written.append("chat.html")
        else:
            name, fn = WRITERS[fmt]
            fn(job.dir / name, model)
            written.append(name)
    say(f"  Wrote {', '.join(written)}  ->  {job.dir}")
    return {"label": job.label, "dir": str(job.dir), "messages": len(msgs),
            "media": dict(media_stats), "error": None}


EPILOG = """examples:
  %(prog)s                                   interactive menu (pick DMs / servers / channels)
  %(prog)s --list                            print your DMs and servers with their IDs
  %(prog)s -c 123456789012345678             entire history of one channel / DM
  %(prog)s -c 1234... -n 2000                only the most recent 2000 messages
  %(prog)s -c 1234... --after 2025-01-01 --before 2025-02-01 --format json,csv,html
  %(prog)s -g 9876... --threads              every text channel (and thread) of a server
  %(prog)s -c 1234... --media all --reactors archive everything incl. avatars, emoji, who reacted

dates: YYYY-MM-DD, 'YYYY-MM-DD HH:MM', relative (7d, 12h, 3w) or a message ID. Naive dates use --tz.
token: set DISCORD_TOKEN (or put it in a .env file); if absent you are asked, with hidden input.
"""


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="discord_export.py", description=f"Discord chat exporter v{__version__}",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    sel = p.add_argument_group("what to export")
    sel.add_argument("-c", "--channel", action="append", default=[], metavar="ID",
                     help="channel / DM / thread ID (repeat, or comma-separate)")
    sel.add_argument("-g", "--guild", action="append", default=[], metavar="ID",
                     help="export every text channel of this server")
    sel.add_argument("--all-dms", action="store_true", help="export all your DMs and group chats")
    sel.add_argument("--threads", action="store_true", help="also export threads (active + archived public)")
    sel.add_argument("--list", action="store_true", help="list your DMs and servers with IDs, then exit")
    rng = p.add_argument_group("range")
    rng.add_argument("-n", "--limit", type=int, default=0, metavar="N", help="only the most recent N messages (default: all)")
    rng.add_argument("--after", metavar="WHEN", help="only messages at/after this date, or after this message ID")
    rng.add_argument("--before", metavar="WHEN", help="only messages before this date / message ID (so --after 2025-01-01 --before 2025-02-01 = January)")
    out = p.add_argument_group("output")
    out.add_argument("-o", "--out", default="discord_export", metavar="DIR", help="output folder (default: discord_export)")
    out.add_argument("-f", "--format", default="json,html", metavar="LIST",
                     help="comma list of json,jsonl,csv,txt,html or 'all' (default: json,html)")
    out.add_argument("--tz", default="local", metavar="TZ", help="'local' (default), 'utc' or an IANA name like Europe/Istanbul")
    med = p.add_argument_group("media & extras")
    med.add_argument("--media", choices=["none", "attachments", "all"], default="attachments",
                     help="download files so links never expire; 'all' adds embed images, stickers, emoji, avatars")
    med.add_argument("--max-media-mb", type=float, default=100, metavar="MB", help="skip files larger than this (0 = no limit)")
    med.add_argument("--workers", type=int, default=6, help="parallel downloads (default 6)")
    med.add_argument("--reactors", action="store_true", help="also record WHO reacted (1+ extra request per reaction)")
    beh = p.add_argument_group("behaviour")
    beh.add_argument("--fresh", action="store_true", help="ignore cache and re-download everything (refreshes edits/deletes)")
    beh.add_argument("--delay", type=float, default=0.3, metavar="SEC", help="pause between message requests (default 0.3)")
    beh.add_argument("--bot", action="store_true", help="token is a bot token")
    beh.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--api-base", default=os.environ.get("DISCORD_API_BASE"), help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    if args.limit < 0 or args.workers < 1 or args.delay < 0:
        sys.exit("--limit must be >= 0, --workers >= 1, --delay >= 0")

    tz = get_tz(args.tz)
    formats = ALL_FORMATS if args.format.strip().lower() == "all" else \
        [f.strip().lower() for f in args.format.split(",") if f.strip()]
    bad = [f for f in formats if f not in ALL_FORMATS]
    if bad or not formats:
        sys.exit(f"Unknown format(s): {', '.join(bad) or '(none)'}. Choose from {', '.join(ALL_FORMATS)} or 'all'.")
    args.after_id = args.before_id = None
    try:
        if args.after:
            args.after_id = parse_when(args.after, tz)
        if args.before:
            args.before_id = parse_when(args.before, tz)
    except ValueError as e:
        sys.exit(str(e))
    if args.after_id and args.before_id and args.after_id >= args.before_id:
        sys.exit("--after must be earlier than --before")

    token = get_token()
    out_dir = Path(args.out)
    setup_logging(None if args.list else out_dir / "export.log", args.verbose, [token])
    api = DiscordAPI(token, bot=args.bot, base=args.api_base or API_BASE)
    try:
        me = api.me()
    except AuthError:
        sys.exit("Discord rejected the token (401). It may be wrong, revoked, or you changed your password.")
    except ApiError as e:
        sys.exit(f"Could not reach Discord: {e}")
    say(f"Logged in as {user_display(me)}")

    if args.list:
        print_listing(api)
        return 0

    catalog = Catalog(api)
    chans: List[dict] = []
    failures = 0
    ids = [x for raw in args.channel for x in re.split(r"[,\s]+", raw) if x]
    for cid in ids:
        if not cid.isdigit():
            sys.exit(f"Channel ID must be numeric, got {cid!r}")
        try:
            chans.append(api.channel(cid))
        except ApiError as e:
            say(f"Channel {cid}: {e}")
            failures += 1
    for gid in args.guild:
        try:
            chans += guild_text_channels(api, gid)
        except ApiError as e:
            say(f"Server {gid}: {e}")
            failures += 1
    if args.all_dms:
        chans += api.dm_channels()

    if not chans and not failures:
        if not sys.stdin.isatty():
            sys.exit("Nothing to export. Use -c CHANNEL_ID, -g SERVER_ID, --all-dms, or run in a terminal for the menu.")
        chans = interactive_select(api)
        if not (args.limit or args.after or args.before):
            raw = input("How many of the most recent messages? [Enter = everything]: ").strip()
            if raw.isdigit():
                args.limit = int(raw)

    jobs: List[Job] = []
    seen = set()
    for ch in chans:
        if ch["id"] in seen:
            continue
        seen.add(ch["id"])
        parent_job = catalog.make_job(ch, out_dir)
        is_container = ch.get("type") in (CH_FORUM, CH_MEDIA)
        if not is_container:
            jobs.append(parent_job)
        if args.threads or is_container:
            for t in catalog.threads_of(ch):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    jobs.append(catalog.make_job(t, out_dir, base=parent_job.dir / "threads"))
    if not jobs:
        say("Nothing to export.")
        return 1 if failures else 0

    say(f"\nExporting {len(jobs)} channel(s) to {out_dir.resolve()}")
    results: List[dict] = []
    t0 = time.monotonic()
    for i, job in enumerate(jobs, 1):
        say(f"\n[{i}/{len(jobs)}] {job.label}")
        try:
            results.append(process_job(api, job, args, tz, formats))
        except KeyboardInterrupt:
            say("\nInterrupted. Progress is saved; run the same command again to resume.")
            return 130
        except AuthError:
            say("\nToken was rejected mid-run (401). Progress so far is saved.")
            return 2
        except ApiError as e:
            say(f"  Skipped: {e}")
            log.error("Channel %s failed: %s", job.label, e)
            results.append({"label": job.label, "dir": str(job.dir), "messages": 0, "media": {}, "error": str(e)})
            failures += 1
        except Exception as e:
            log.exception("Unexpected error exporting %s", job.label)
            say(f"  Failed unexpectedly: {e.__class__.__name__}: {e} (see export.log)")
            results.append({"label": job.label, "dir": str(job.dir), "messages": 0, "media": {}, "error": str(e)})
            failures += 1

    total = sum(r["messages"] for r in results)
    say(f"\n{'=' * 60}\nDone in {human_duration(time.monotonic() - t0)}: {total:,} messages from "
        f"{len(results) - sum(1 for r in results if r['error'])}/{len(results)} channels, "
        f"{api.requests_made:,} API requests ({api.rate_limited} rate-limited)")
    for r in results:
        say(f"  {'FAILED ' if r['error'] else 'ok     '}{r['label']}: {r['messages']:,} msgs"
            + (f"  ({r['error']})" if r["error"] else ""))
    if any(r["media"].get("failed") for r in results):
        say("  Some media failed to download (expired/deleted). Details in export.log")
    if "html" in formats:
        ok = [r for r in results if not r["error"]]
        if len(ok) == 1:
            say(f"Open in your browser:  {Path(ok[0]['dir']) / 'chat.html'}")
        elif ok:
            say("Open each channel's chat.html (inside its folder) in your browser.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
