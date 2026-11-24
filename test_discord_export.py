#!/usr/bin/env python3
import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import discord_export as dx

TOKEN = "TEST.TOKEN.never-leak-me-123"
def _make_png():
    import struct, zlib
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes([88, 101, 242]) * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _make_png()


class TestTimeMath(unittest.TestCase):
    def test_snowflake_roundtrip(self):
        dt = datetime(2024, 5, 17, 12, 30, 45, 123000, tzinfo=timezone.utc)
        sf = dx.datetime_to_snowflake(dt)
        self.assertEqual(sf >> 22, int(dt.timestamp() * 1000) - dx.DISCORD_EPOCH)
        self.assertAlmostEqual(dx.snowflake_to_datetime(sf).timestamp(), dt.timestamp(), places=2)

    def test_known_snowflake(self):
        self.assertEqual(dx.snowflake_to_datetime(175928847299117063).strftime("%Y-%m-%d %H:%M:%S"),
                         "2016-04-30 11:18:25")

    def test_epoch_clamp(self):
        self.assertEqual(dx.datetime_to_snowflake(datetime(2010, 1, 1, tzinfo=timezone.utc)), 0)

    def test_parse_when(self):
        utc = timezone.utc
        self.assertEqual(dx.parse_when("175928847299117063", utc), 175928847299117063)
        a = dx.parse_when("2025-01-31", utc)
        self.assertEqual(dx.snowflake_to_datetime(a).isoformat(), "2025-01-31T00:00:00+00:00")
        b = dx.parse_when("2025-01-31 14:30", utc)
        self.assertEqual(dx.snowflake_to_datetime(b).hour, 14)
        self.assertGreater(dx.parse_when("1d", utc), dx.parse_when("3d", utc))
        self.assertGreater(dx.parse_when("2025-01-31T14:30:00+03:00", utc), a)
        with self.assertRaises(ValueError):
            dx.parse_when("not a date", utc)

    def test_parse_iso_variants(self):
        for s in ("2026-03-01T12:00:00.123456+00:00", "2026-03-01T12:00:00+00:00", "2026-03-01T12:00:00.1Z",
                  "2026-03-01T12:00:00.123+00:00"):
            self.assertEqual(dx.parse_iso(s).year, 2026)

    def test_tz_dst_aware_local(self):
        tz = dx.get_tz("Europe/Istanbul")
        dt = dx.parse_iso("2026-03-01T12:00:00+00:00").astimezone(tz)
        self.assertEqual(dt.hour, 15)


class TestTzLabel(unittest.TestCase):
    def test_offset_style_labels(self):
        from datetime import timezone, timedelta
        self.assertEqual(dx.tz_label(timezone.utc), "UTC")
        self.assertEqual(dx.tz_label(timezone(timedelta(hours=3))), "UTC+3")
        self.assertEqual(dx.tz_label(timezone(timedelta(hours=-5, minutes=-30))), "UTC-5:30")
        self.assertEqual(dx.tz_label(timezone(timedelta(hours=5, minutes=30))), "UTC+5:30")
        for name in ("Europe/Istanbul", "Asia/Kolkata", "America/New_York"):
            tz = dx.get_tz(name)
            if tz is not None and str(tz) == name:
                self.assertRegex(dx.tz_label(tz), r"^UTC([+-]\d{1,2}(:\d{2})?)?$")
        self.assertRegex(dx.tz_label(None), r"^UTC([+-]\d{1,2}(:\d{2})?)?$")


class TestHelpers(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(dx.safe_filename("a/b\\c:d*.png"), "a_b_c_d_.png")
        self.assertEqual(dx.safe_filename("../../etc/passwd"), ".._.._etc_passwd".strip(" ."))
        self.assertTrue(dx.safe_filename("CON.txt").startswith("_"))
        self.assertLessEqual(len(dx.safe_filename("x" * 500 + ".mp4")), 100)
        self.assertEqual(dx.safe_filename(None), "file")
        self.assertNotIn("/", dx.safe_filename("../x"))

    def test_csv_injection(self):
        self.assertEqual(dx._csv_safe("=1+1"), "'=1+1")
        self.assertEqual(dx._csv_safe("hello"), "hello")

    def test_selection_parser(self):
        self.assertEqual(dx._parse_selection("1,3,5-7", 9), [0, 2, 4, 5, 6])
        self.assertIsNone(dx._parse_selection("0", 5))
        self.assertIsNone(dx._parse_selection("9", 5))
        self.assertIsNone(dx._parse_selection("x", 5))

    def test_route_key(self):
        k = dx.DiscordAPI._route_key
        self.assertEqual(k("/channels/123/messages"), "channels/123/messages")
        self.assertEqual(k("/channels/123/messages/456/reactions/%F0%9F%91%8D"), "channels/123/messages/:id/reactions")

    def test_token_redaction(self):
        import logging
        rec = logging.LogRecord("x", logging.WARNING, "f", 1, "oops %s", (TOKEN,), None)
        dx.RedactFilter([TOKEN]).filter(rec)
        self.assertNotIn(TOKEN, rec.getMessage())


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.res = dx.Resolver(timezone.utc)
        self.res.add_user({"id": "42", "username": "bob", "global_name": "Bob"})
        self.res.roles["7"] = "Mods"
        self.res.channels["9"] = "general"

    def test_text_mentions(self):
        t = dx.to_text("hi <@42> <@!42> <@&7> <#9> <:wave:123> <a:dance:124> <t:0:t>", self.res)
        self.assertEqual(t, "hi @Bob @Bob @Mods #general :wave: :dance: 00:00")

    def test_unknown_mention(self):
        self.assertIn("user 5", dx.to_text("<@5>", self.res))

    def test_xss_is_escaped(self):
        payloads = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
                    '[x](https://a.com" onmouseover="alert(1))', '"><svg onload=alert(1)>',
                    'https://a.com/"><script>x</script>', '`<b>`', '```<i>```', '||<u>||']
        for p in payloads:
            out = dx.render_html(p, self.res)
            self.assertNotRegex(out, r"<(script|img src=x|svg)", p)
            self.assertNotIn('" onmouseover=', out)
        self.assertNotRegex(dx.render_html("[a](javascript:alert(1))", self.res), r"href=.?javascript")

    def test_formatting(self):
        r = dx.render_html("**b** *i* __u__ ~~s~~ `c` ||sp||", self.res)
        for frag in ("<strong>b</strong>", "<em>i</em>", "<u>u</u>", "<s>s</s>", "<code>c</code>",
                     '<span class="spoiler">sp</span>'):
            self.assertIn(frag, r)

    def test_links_and_mentions(self):
        r = dx.render_html("see https://example.com/a_b_c?x=1&y=2. and <@42>", self.res)
        self.assertIn('href="https://example.com/a_b_c?x=1&amp;y=2"', r)
        self.assertNotIn("<em>", r)
        self.assertIn("@Bob", r)

    def test_code_block_and_quote(self):
        r = dx.render_html("```py\nprint('<x>')\n```\n> quoted\nplain", self.res)
        self.assertIn("<pre><code>print(", r)
        self.assertIn("&lt;x&gt;", r)
        self.assertIn("<blockquote>quoted</blockquote>", r)

    def test_stash_marker_injection(self):
        r = dx.render_html("\ue0000\ue001 `x`", self.res)
        self.assertNotIn("\ue000", r)

    def test_json_for_script(self):
        s = dx._json_for_script({"x": "</script><!--", "y": "\u2028"})
        self.assertNotIn("</script>", s)
        self.assertNotIn("<", s)
        self.assertEqual(json.loads(s)["x"], "</script><!--")


class TestStore(unittest.TestCase):
    def test_roundtrip_and_corruption_repair(self):
        with tempfile.TemporaryDirectory() as d:
            st = dx.ChannelStore(Path(d))
            st.load()
            st.append([{"id": "30", "x": 1}, {"id": "20", "x": 2}])
            st.append([{"id": "20", "x": 2}, {"id": "10", "x": 3}])
            self.assertEqual((st.oldest_id, st.newest_id, len(st.messages)), (10, 30, 3))
            with open(st.raw_path, "a", encoding="utf-8") as f:
                f.write('{"id":"5","trunc')
            st2 = dx.ChannelStore(Path(d))
            st2.load()
            self.assertEqual(len(st2.messages), 3)
            st2.append([{"id": "4"}])
            st3 = dx.ChannelStore(Path(d))
            st3.load()
            self.assertEqual(len(st3.messages), 4)


class MockDiscord:
    GUILD = "900"
    CHANNEL = "111"
    THREAD = "444"

    def __init__(self, n=450):
        self.lock = threading.Lock()
        self.messages = []
        self.log = []
        self.msg_requests = 0
        self.inject_500_at = {2}
        self.inject_429_at = {4}
        self.gone_refreshed = False
        base = datetime(2026, 3, 1, tzinfo=timezone.utc)
        self.base_ms = int(base.timestamp() * 1000)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        for i in range(n):
            self.messages.append(self.make(i))
        self.thread_messages = [dict(self.make(i), channel_id=self.THREAD, content=f"thread msg {i}", attachments=[],
                                     reactions=[], type=0, pinned=False) for i in range(5)]
        for m in self.thread_messages:
            m.pop("message_reference", None); m.pop("referenced_message", None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def make(self, i):
        ms = self.base_ms + i * 90_000
        mid = str(((ms - dx.DISCORD_EPOCH) << 22) | (i & 0xFFF))
        ts = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="microseconds")
        author = {"id": "1", "username": "alice", "global_name": "Alice", "avatar": None} if i % 2 == 0 else \
                 {"id": "2", "username": "bob", "global_name": "Bob", "avatar": None}
        m = {"id": mid, "channel_id": self.CHANNEL, "type": 0, "timestamp": ts, "edited_timestamp": None,
             "author": author, "content": f"message number {i}", "mentions": [], "mention_roles": [],
             "mention_everyone": False, "attachments": [], "embeds": [], "pinned": False, "tts": False, "flags": 0}
        if i % 10 == 0:
            m["content"] = f"hey <@2> look at this <#{self.CHANNEL}> <@&5>"
            m["mentions"] = [{"id": "2", "username": "bob", "global_name": "Bob"}]
            m["attachments"] = [{"id": f"a{i}", "filename": f"pic{i}.png", "size": len(PNG), "content_type": "image/png",
                                 "url": f"{self.url}/cdn/file{i}.png", "proxy_url": f"{self.url}/cdn/file{i}.png",
                                 "width": 1, "height": 1}]
        if i == 20:
            m["attachments"][0]["url"] = m["attachments"][0]["proxy_url"] = f"{self.url}/cdn/gone.png"
        if i % 7 == 0:
            m["reactions"] = [{"emoji": {"id": None, "name": "\U0001F44D"}, "count": 2, "me": False}]
        if i % 13 == 0 and i > 0:
            prev = self.messages[i - 1]
            m["type"] = 19
            m["message_reference"] = {"message_id": prev["id"], "channel_id": self.CHANNEL, "type": 0}
            m["referenced_message"] = prev
        if i == 30:
            m["type"] = 19
            m["message_reference"] = {"message_id": "1", "channel_id": self.CHANNEL}
            m["referenced_message"] = None
        if i % 50 == 0 and i > 0:
            m["pinned"] = True
        if i == 5:
            m["content"] = '<img src=x onerror=alert(1)> <script>alert(1)</script> https://example.com/a_b_c'
        if i == 6:
            m["embeds"] = [{"type": "rich", "title": "Embed <b>title</b>", "description": "desc **bold**",
                            "color": 0xFF0000, "fields": [{"name": "f1", "value": "v1", "inline": True}],
                            "footer": {"text": "foot"}, "image": {"url": f"{self.url}/cdn/embed.png"}}]
        if i == 8:
            m["type"] = 7
            m["content"] = ""
        if i == 9:
            m["poll"] = {"question": {"text": "Pizza?"}, "answers": [
                {"answer_id": 1, "poll_media": {"text": "Yes"}}, {"answer_id": 2, "poll_media": {"text": "No"}}],
                "results": {"is_finalized": True, "answer_counts": [{"id": 1, "count": 3}, {"id": 2, "count": 1}]}}
        if i == 11:
            m["sticker_items"] = [{"id": "555", "name": "wave", "format_type": 1}]
        if i == 12:
            m["content"] = ""
            m["message_reference"] = {"type": 1, "message_id": "77", "channel_id": "321"}
            m["message_snapshots"] = [{"message": {"content": "forwarded text", "timestamp": ts, "attachments": [], "embeds": []}}]
        if i == 14:
            m["content"] = "=SUM(A1) csv injection"
        return m

    def page(self, before, after, limit, source=None):
        with self.lock:
            msgs = list(self.messages if source is None else source)
        if before:
            sel = [m for m in msgs if int(m["id"]) < before][-limit:]
        elif after:
            sel = [m for m in msgs if int(m["id"]) > after][:limit]
        else:
            sel = msgs[-limit:]
        return sorted(sel, key=lambda m: int(m["id"]), reverse=True)

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json", extra=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.endswith("/attachments/refresh-urls"):
                    outer.gone_refreshed = True
                    return self.send(200, {"refreshed_urls": [
                        {"original": u, "refreshed": f"{outer.url}/cdn/file_ok.png"}
                        for u in body["attachment_urls"] if "gone.png" in u]})
                self.send(404, {"message": "nope"})

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                has_auth = "Authorization" in self.headers
                outer.log.append((u.path, q, has_auth))
                p = u.path
                if p.startswith("/cdn/"):
                    if p == "/cdn/gone.png":
                        return self.send(404, b"gone", "text/plain")
                    return self.send(200, PNG, "image/png")
                if not p.startswith("/api/v10"):
                    return self.send(404, {"message": "bad"})
                p = p[len("/api/v10"):]
                if self.headers.get("Authorization") != TOKEN:
                    return self.send(401, {"message": "401: Unauthorized", "code": 0})
                if p == "/users/@me":
                    return self.send(200, {"id": "1", "username": "alice", "global_name": "Alice"})
                if p == "/users/@me/channels":
                    return self.send(200, [{"id": "333", "type": 1, "last_message_id": "9",
                                            "recipients": [{"id": "2", "username": "bob", "global_name": "Bob"}]}])
                if p == "/users/@me/guilds":
                    return self.send(200, [{"id": outer.GUILD, "name": "Test Guild"}])
                if p == f"/guilds/{outer.GUILD}/threads/active":
                    return self.send(200, {"threads": [{"id": outer.THREAD, "parent_id": outer.CHANNEL, "type": 11,
                                                        "name": "my thread", "guild_id": outer.GUILD}]})
                if p == f"/channels/{outer.CHANNEL}/threads/archived/public":
                    return self.send(200, {"threads": [], "has_more": False})
                if p == f"/channels/{outer.THREAD}/messages":
                    return self.send(200, outer.page(int(q["before"]) if "before" in q else None,
                                                     int(q["after"]) if "after" in q else None,
                                                     int(q.get("limit", 50)), outer.thread_messages))
                if p == f"/channels/{outer.CHANNEL}":
                    return self.send(200, {"id": outer.CHANNEL, "type": 0, "name": "general",
                                           "guild_id": outer.GUILD, "topic": "chat"})
                if p == f"/guilds/{outer.GUILD}":
                    return self.send(200, {"id": outer.GUILD, "name": "Test Guild"})
                if p == f"/guilds/{outer.GUILD}/roles":
                    return self.send(200, [{"id": "5", "name": "Mods"}])
                if p == f"/guilds/{outer.GUILD}/channels":
                    return self.send(200, [{"id": "50", "type": 4, "name": "Category"},
                                           {"id": outer.CHANNEL, "type": 0, "name": "general", "position": 1}])
                if re.fullmatch(rf"/channels/{outer.CHANNEL}/messages/\d+/reactions/.+", p):
                    return self.send(200, [{"id": "1", "username": "alice", "global_name": "Alice"},
                                           {"id": "2", "username": "bob"}])
                if p == f"/channels/{outer.CHANNEL}/messages":
                    with outer.lock:
                        outer.msg_requests += 1
                        n = outer.msg_requests
                    if n in outer.inject_500_at:
                        return self.send(500, {"message": "boom"})
                    if n in outer.inject_429_at:
                        return self.send(429, {"message": "rate limited", "retry_after": 0.05, "global": False},
                                         extra={"Retry-After": "1"})
                    page = outer.page(int(q["before"]) if "before" in q else None,
                                      int(q["after"]) if "after" in q else None, int(q.get("limit", 50)))
                    return self.send(200, page)
                self.send(404, {"message": "Unknown Channel", "code": 10003})

        return H


def run_export(mock, out, *extra):
    argv = ["-c", mock.CHANNEL, "-o", str(out), "--api-base", f"{mock.url}/api/v10", "--delay", "0",
            "--tz", "UTC", *extra]
    with mock_env():
        return dx.main(argv)


def mock_env():
    stack = mock.patch.dict(os.environ, {"DISCORD_TOKEN": TOKEN})
    return stack


def only_local_downloads(url_self, url, dest):
    return real_get_to_file(url_self, url, dest) if "127.0.0.1" in url else 404


real_get_to_file = dx.MediaDownloader._get_to_file


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.mock = MockDiscord(450)
        p1 = mock.patch.object(dx.DiscordAPI, "_backoff", staticmethod(lambda attempt: 0.01))
        p2 = mock.patch.object(dx.MediaDownloader, "_get_to_file", only_local_downloads)
        p3 = mock.patch.object(dx, "_is_discord_cdn", lambda url: "127.0.0.1" in url)
        p1.start(); p2.start(); p3.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop); self.addCleanup(p3.stop)
        self.addCleanup(self.mock.close); self.addCleanup(self.tmp.cleanup)

    def chan_dir(self):
        dirs = [d for d in self.out.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def test_full_export_all_formats(self):
        rc = run_export(self.mock, self.out, "--format", "all", "--media", "all", "--reactors")
        self.assertEqual(rc, 0)
        d = self.chan_dir()
        for f in ("chat.json", "chat.jsonl", "chat.csv", "chat.txt", "chat.html", "raw.jsonl", "state.json", "reactors.json"):
            self.assertTrue((d / f).exists(), f)
        model = json.loads((d / "chat.json").read_text("utf-8"))
        msgs = model["messages"]
        self.assertEqual(model["message_count"], 450)
        ids = [int(m["id"]) for m in msgs]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 450)
        self.assertTrue(json.loads((d / "state.json").read_text())["complete_backwards"])
        self.assertGreaterEqual(self.mock.msg_requests, 6)

        by = {m["content"][:14]: m for m in msgs}
        m0 = msgs[0]
        self.assertEqual(m0["content_text"], "hey @Bob look at this #general @Mods")
        self.assertEqual(m0["author"]["display_name"], "Alice")
        self.assertTrue(m0["attachments"][0]["local_path"])
        self.assertTrue((d / m0["attachments"][0]["local_path"]).is_file())

        self.assertTrue(self.mock.gone_refreshed)
        gone = msgs[20]["attachments"][0]
        self.assertTrue(gone["local_path"] and (d / gone["local_path"]).is_file())

        r = msgs[13]["reply_to"]
        self.assertEqual(r["message_id"], msgs[12]["id"])
        self.assertEqual(r["author"], "Alice")
        self.assertTrue(msgs[30]["reply_to"]["deleted"])
        self.assertEqual(msgs[8]["type_name"], "USER_JOIN")
        self.assertIn("joined the server", msgs[8]["system_text"])
        self.assertEqual(msgs[9]["poll"]["answers"][0]["votes"], 3)
        self.assertEqual(msgs[12]["forwarded"]["content_text"], "forwarded text")
        self.assertEqual(msgs[11]["stickers"][0]["name"], "wave")
        self.assertEqual(msgs[6]["embeds"][0]["fields"][0]["name"], "f1")
        self.assertTrue(msgs[6]["embeds"][0]["image"]["local_path"])
        self.assertTrue(msgs[50]["pinned"])
        self.assertEqual(msgs[0]["reactions"][0]["count"], 2)
        self.assertEqual([u["username"] for u in msgs[0]["reactions"][0]["users"]], ["alice", "bob"])
        self.assertEqual(model["stats"]["total"], 449)
        self.assertEqual(model["export"]["timezone"], "UTC")


        csv_text = (d / "chat.csv").read_text("utf-8-sig")
        self.assertIn("'=SUM(A1) csv injection", csv_text)
        self.assertTrue((d / "chat.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

        txt = (d / "chat.txt").read_text("utf-8")
        self.assertIn("===== 2026-03-01 =====", txt)
        self.assertIn("> replying to Alice", txt)
        self.assertIn("[poll] Pizza?", txt)

        page = (d / "chat.html").read_text("utf-8")
        self.assertNotIn("<script>alert(1)", page)
        self.assertNotIn("<img src=x", page)
        self.assertEqual(page.count("<script"), 2)
        self.assertNotIn("__DATA__", page); self.assertNotIn("__TITLE__", page)
        data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(len(data["messages"]), 450)
        self.assertIn("Test Guild", page)

    def test_token_never_written_and_never_sent_to_cdn(self):
        run_export(self.mock, self.out, "--format", "all", "--media", "attachments", "-v")
        for path in self.out.rglob("*"):
            if path.is_file() and path.suffix not in (".png",):
                self.assertNotIn(TOKEN.encode(), path.read_bytes(), str(path))
        cdn = [(p, a) for p, q, a in self.mock.log if p.startswith("/cdn/")]
        self.assertTrue(cdn)
        self.assertFalse(any(a for _, a in cdn), "Authorization header leaked to the CDN")

    def test_limit_then_resume_then_incremental(self):
        run_export(self.mock, self.out, "-n", "100", "--format", "json", "--media", "none")
        d = self.chan_dir()
        self.assertEqual(json.loads((d / "chat.json").read_text())["message_count"], 100)
        cached1 = len((d / "raw.jsonl").read_text().splitlines())
        self.assertLess(cached1, 250)
        self.assertFalse(json.loads((d / "state.json").read_text())["complete_backwards"])


        self.mock.log.clear()
        run_export(self.mock, self.out, "-n", "250", "--format", "json", "--media", "none")
        self.assertEqual(json.loads((d / "chat.json").read_text())["message_count"], 250)
        msg_reqs = [q for p, q, a in self.mock.log if p.endswith("/messages")]
        self.assertTrue(msg_reqs[0].get("after"))
        self.assertLessEqual(len(msg_reqs), 5)


        for i in range(450, 470):
            self.mock.messages.append(self.mock.make(i))
        self.mock.log.clear()
        run_export(self.mock, self.out, "-n", "250", "--format", "json", "--media", "none")
        model = json.loads((d / "chat.json").read_text())
        self.assertEqual(model["message_count"], 250)
        self.assertEqual(model["messages"][-1]["id"], self.mock.messages[-1]["id"])
        self.assertEqual(model["messages"][0]["id"], self.mock.messages[470 - 250]["id"])
        ids = [m["id"] for m in model["messages"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_date_range(self):
        run_export(self.mock, self.out, "--after", "2026-03-01 01:00", "--before", "2026-03-01 02:00",
                   "--format", "json", "--media", "none")
        model = json.loads((self.chan_dir() / "chat.json").read_text())
        times = [m["timestamp"][:19] for m in model["messages"]]
        self.assertTrue(all("2026-03-01T01:00:00" <= t < "2026-03-01T02:00:00" for t in times), times[:3])
        self.assertEqual(model["message_count"], 40)

        cached = len((self.chan_dir() / "raw.jsonl").read_text().splitlines())
        self.assertLess(cached, 200)

    def test_resume_after_crash_mid_write(self):
        run_export(self.mock, self.out, "-n", "150", "--format", "json", "--media", "none")
        d = self.chan_dir()
        with open(d / "raw.jsonl", "a", encoding="utf-8") as f:
            f.write('{"id": "99999999999999999999", "content": "trunc')
        rc = run_export(self.mock, self.out, "--format", "json", "--media", "none")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((d / "chat.json").read_text())["message_count"], 450)

    def test_ctrl_c_saves_progress_and_resume_has_no_gaps(self):
        real = dx.DiscordAPI.messages
        calls = {"n": 0}

        def interrupting(api, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real(api, *a, **kw)

        with mock.patch.object(dx.DiscordAPI, "messages", interrupting):
            rc = run_export(self.mock, self.out, "--format", "json", "--media", "none")
        self.assertEqual(rc, 130)
        d = self.chan_dir()
        cached = len((d / "raw.jsonl").read_text().splitlines())
        self.assertTrue(0 < cached < 450, cached)
        self.assertFalse((d / "chat.json").exists())
        self.assertFalse(json.loads((d / "state.json").read_text())["complete_backwards"])

        rc = run_export(self.mock, self.out, "--format", "json", "--media", "none")
        self.assertEqual(rc, 0)
        ids = [m["id"] for m in json.loads((d / "chat.json").read_text())["messages"]]
        self.assertEqual(ids, [m["id"] for m in self.mock.messages])

    def test_fresh_flag_redownloads(self):
        run_export(self.mock, self.out, "-n", "50", "--format", "json", "--media", "none")
        self.mock.log.clear()
        run_export(self.mock, self.out, "-n", "50", "--fresh", "--format", "json", "--media", "none")
        first = [q for p, q, a in self.mock.log if p.endswith("/messages")][0]
        self.assertNotIn("after", first)

    def test_list_mode(self):
        import contextlib, io
        buf = io.StringIO()
        with mock_env(), contextlib.redirect_stderr(buf):
            rc = dx.main(["--list", "--api-base", f"{self.mock.url}/api/v10"])
        self.assertEqual(rc, 0)
        self.assertIn("DM with Bob", buf.getvalue())
        self.assertIn("Test Guild", buf.getvalue())
        self.assertFalse(any(self.out.iterdir()), "--list must not create files")

    def test_guild_export_with_threads(self):
        argv = ["-g", self.mock.GUILD, "--threads", "-o", str(self.out), "--api-base", f"{self.mock.url}/api/v10",
                "--delay", "0", "--format", "json", "--media", "none"]
        with mock_env():
            rc = dx.main(argv)
        self.assertEqual(rc, 0)
        main_dir = self.chan_dir()
        self.assertEqual(json.loads((main_dir / "chat.json").read_text())["message_count"], 450)
        thread_files = list((main_dir / "threads").rglob("chat.json"))
        self.assertEqual(len(thread_files), 1)
        tm = json.loads(thread_files[0].read_text())
        self.assertEqual(tm["message_count"], 5)
        self.assertIn("thread", tm["channel"]["label"])

    def test_bad_token_exits_cleanly(self):
        with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "wrong"}):
            with self.assertRaises(SystemExit) as cm:
                dx.main(["-c", "111", "-o", str(self.out), "--api-base", f"{self.mock.url}/api/v10"])
        self.assertIn("401", str(cm.exception))

    def test_missing_channel_is_reported_not_fatal(self):
        argv = ["-c", "222", "-c", self.mock.CHANNEL, "-o", str(self.out), "--api-base", f"{self.mock.url}/api/v10",
                "--delay", "0", "--format", "json", "--media", "none"]
        with mock_env():
            rc = dx.main(argv)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads((self.chan_dir() / "chat.json").read_text())["message_count"], 450)


class ProgressAndCounts(unittest.TestCase):
    def make(self, total=None):
        with mock.patch.object(dx.sys, "stderr") as err:
            err.isatty.return_value = True
            return dx.Progress("  Fetching", total)

    def run_updates(self, prog, steps):
        out = []
        clock = {"t": 0.0}
        with mock.patch.object(dx.time, "monotonic", lambda: clock["t"]), \
             mock.patch.object(dx, "say", out.append), mock.patch.object(dx.sys, "stderr") as err:
            prog.start = 0.0
            for t, n, f in steps:
                clock["t"] = t
                err.reset_mock()
                prog.update(n, f)
                written = "".join(c.args[0] for c in err.write.call_args_list)
                if written:
                    out.append(written.replace("\r", "").strip())
        return out

    def test_eta_ignores_progress_made_before_this_run(self):
        lines = self.run_updates(self.make(), [(1, 100, 0.60), (4, 130, 0.63), (11, 200, 0.70), (21, 300, 0.80)])
        last = lines[-1]
        self.assertIn("80%", last)
        m = re.search(r"ETA (\d+):(\d\d)", last)
        self.assertTrue(m, last)
        secs = int(m.group(1)) * 60 + int(m.group(2))
        self.assertTrue(15 <= secs <= 25, last)

    def test_no_eta_until_there_is_something_to_base_it_on_and_bar_never_goes_back(self):
        lines = self.run_updates(self.make(), [(0.5, 10, 0.05), (1.0, 20, 0.04), (2.0, 30, 0.08)])
        self.assertTrue(all("ETA" not in l for l in lines), lines)
        self.assertIn("5%", lines[1])
        self.assertIn("8%", lines[2])

    def test_shows_done_of_total_when_known(self):
        out = self.run_updates(self.make(), [(1, 100, 0.25)])
        self.assertIn("100", out[0])
        prog = self.make()
        out = []
        with mock.patch.object(dx.sys, "stderr") as err:
            prog.tty = True
            prog.update(100, 0.25, of=(1234, 5000))
            out.append("".join(c.args[0] for c in err.write.call_args_list))
        self.assertIn("1,234 of 5,000 msgs", out[0])

    def test_count_messages_reads_search_total_and_never_raises(self):
        api = dx.DiscordAPI("t", base="http://127.0.0.1:9")
        with mock.patch.object(dx.DiscordAPI, "request", return_value={"total_results": 4321, "messages": []}) as req:
            self.assertEqual(api.count_messages("55", None), 4321)
            self.assertIn("/channels/55/messages/search", req.call_args.args[1])
            self.assertTrue(req.call_args.kwargs["no_retry"])
            api.count_messages("55", "99", min_id=5)
            self.assertIn("/guilds/99/messages/search", req.call_args.args[1])
            self.assertEqual(req.call_args.kwargs["params"]["channel_id"], "55")
        with mock.patch.object(dx.DiscordAPI, "request", return_value={"message": "Index not yet available", "retry_after": 2}):
            self.assertIsNone(api.count_messages("55"))
        for exc in (dx.ForbiddenError(403, "no"), dx.NotFoundError(404, "x"), dx.ApiError(429, "rate limited")):
            with mock.patch.object(dx.DiscordAPI, "request", side_effect=exc):
                self.assertIsNone(api.count_messages("55"))

    def test_no_retry_does_not_sleep_on_rate_limit(self):
        mock_srv = MockDiscord(5)
        self.addCleanup(mock_srv.close)
        api = dx.DiscordAPI(TOKEN, base=f"{mock_srv.url}/api/v10")
        with mock.patch.object(dx.time, "sleep") as sleep:
            self.assertIsNone(api.count_messages(mock_srv.CHANNEL))
        sleep.assert_not_called()


class ProgressInRealExport(unittest.TestCase):
    setUp = EndToEnd.setUp
    chan_dir = EndToEnd.chan_dir

    def run_capture(self, *extra):
        lines = []
        with mock.patch.object(dx, "say", lines.append):
            self.assertEqual(run_export(self.mock, self.out, *extra), 0)
        return lines

    def test_search_total_drives_the_percentage(self):
        with mock.patch.object(dx.DiscordAPI, "count_messages", return_value=450):
            lines = self.run_capture("--format", "json", "--media", "none")
        prog = [l for l in lines if "Fetching [" in l]
        self.assertTrue(prog, lines)
        self.assertIn("of 450", prog[0])
        pcts = [int(re.search(r"(\d+)%", l).group(1)) for l in prog]
        self.assertEqual(pcts, sorted(pcts))
        self.assertLessEqual(max(pcts), 99)

    def test_falls_back_to_time_based_when_search_unavailable(self):
        with mock.patch.object(dx.DiscordAPI, "count_messages", return_value=None):
            lines = self.run_capture("--format", "json", "--media", "none")
        self.assertEqual(sum(1 for l in lines if "450 messages" in l or "450 new" in l) >= 1, True)

    def test_html_report_opens_at_the_top_and_keeps_loading_as_you_scroll(self):
        run_export(self.mock, self.out, "--format", "html", "--media", "none")
        page = (self.chan_dir() / "chat.html").read_text("utf-8")
        self.assertNotIn("window.scrollTo(0,document.body.scrollHeight)", page)
        self.assertIn("lo=0; hi=Math.min(view.length,PAGE)", page)
        self.assertIn("window.scrollTo(0,0)", page)
        self.assertIn("addEventListener('scroll'", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
