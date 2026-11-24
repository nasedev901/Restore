import os
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

if "tkinter" not in sys.modules:
    tk_mod = types.ModuleType("tkinter")
    ttk_mod = types.ModuleType("tkinter.ttk")

    class _DummyFrame:
        def __init__(self, *a, **k):
            pass

    ttk_mod.Frame = _DummyFrame
    tk_mod.ttk = ttk_mod
    tk_mod.Frame = _DummyFrame
    for name, mod in [("tkinter", tk_mod), ("tkinter.ttk", ttk_mod),
                       ("tkinter.filedialog", types.ModuleType("tkinter.filedialog")),
                       ("tkinter.messagebox", types.ModuleType("tkinter.messagebox")),
                       ("tkinter.scrolledtext", types.ModuleType("tkinter.scrolledtext"))]:
        sys.modules[name] = mod

import discord_export as dx
import discord_export_gui as gui
from test_discord_export import MockDiscord, TOKEN, only_local_downloads


class TestCoreLogic(unittest.TestCase):
    def setUp(self):
        self.mock = MockDiscord(60)
        self.addCleanup(self.mock.close)

    def test_main_checks_for_tkinter_before_building_a_window(self):
        with mock.patch.object(gui, "_TK_OK", False):
            with self.assertRaises(SystemExit) as cm:
                gui.main()
        self.assertIn("Tkinter", str(cm.exception))

    def test_hue_to_hex_is_stable_and_well_formed(self):
        a = gui._hue_to_hex("1")
        b = gui._hue_to_hex("1")
        c = gui._hue_to_hex("2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        for hexcolor in (a, c, gui._hue_to_hex(None), gui._hue_to_hex("not-a-number")):
            self.assertRegex(hexcolor, r"^#[0-9a-f]{6}$")

    def test_resource_path_dev_and_frozen(self):
        dev = gui.resource_path("Restore.png")
        self.assertEqual(dev, Path(gui.__file__).resolve().parent / "Restore.png")
        with mock.patch.object(sys, "_MEIPASS", "/tmp/frozen", create=True):
            self.assertEqual(gui.resource_path("Restore.png"), Path("/tmp/frozen/Restore.png"))

    def test_fetch_preview(self):
        msgs = gui.fetch_preview(TOKEN, self.mock.CHANNEL, guild_id=self.mock.GUILD, limit=20,
                                 api_base=f"{self.mock.url}/api/v10")
        self.assertEqual(len(msgs), 20)
        ids = [int(m["id"]) for m in msgs]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [int(m["id"]) for m in self.mock.messages[-20:]])

        first = next(m for m in msgs if "look at this" in m["content"])
        self.assertEqual(first["content_text"], "hey @Bob look at this #general @Mods")
        self.assertEqual(first["author"]["display_name"], "Alice")

    def test_fetch_preview_dm_without_guild_id(self):
        msgs = gui.fetch_preview(TOKEN, self.mock.CHANNEL, guild_id=None, limit=5,
                                 api_base=f"{self.mock.url}/api/v10")
        self.assertEqual(len(msgs), 5)

    def test_fetch_preview_bad_token(self):
        with self.assertRaises(dx.AuthError):
            gui.fetch_preview("wrong-token", self.mock.CHANNEL, api_base=f"{self.mock.url}/api/v10")

    def test_dm_label_shows_just_the_name(self):
        dm = {"type": dx.CH_DM, "recipients": [{"id": "2", "username": "bob", "global_name": "Bob"}]}
        self.assertEqual(gui._dm_label(dm), "Bob")
        empty = {"type": dx.CH_DM, "id": "555", "recipients": []}
        self.assertEqual(gui._dm_label(empty), "DM with 555")
        group = {"type": dx.CH_GROUP_DM, "name": "Squad", "recipients": []}
        self.assertEqual(gui._dm_label(group), "Squad")

    def test_guild_icon_url(self):
        self.assertIsNone(gui._guild_icon_url({"id": "1"}))
        self.assertIsNone(gui._guild_icon_url({"id": "1", "icon": None}))
        self.assertEqual(gui._guild_icon_url({"id": "900", "icon": "abc123"}),
                         "https://cdn.discordapp.com/icons/900/abc123.png?size=64")
        self.assertEqual(gui._guild_icon_url({"id": "900", "icon": "a_abc123"}, size=32),
                         "https://cdn.discordapp.com/icons/900/a_abc123.gif?size=32")

    def test_channel_label_by_type(self):
        self.assertEqual(gui._channel_label({"type": dx.CH_TEXT, "name": "general"}), "# general")
        self.assertEqual(gui._channel_label({"type": dx.CH_VOICE, "name": "General 2"}), "\U0001F50A General 2")
        self.assertEqual(gui._channel_label({"type": dx.CH_ANNOUNCE, "name": "announcements"}),
                         "\U0001F4E2 announcements")
        self.assertEqual(gui._channel_label({"type": dx.CH_STAGE, "name": "Town Hall"}), "\U0001F399 Town Hall")
        self.assertEqual(gui._channel_label({"type": dx.CH_FORUM, "name": "help-forum"}), "\U0001F9F5 help-forum")
        self.assertEqual(gui._channel_label({"type": 999, "id": "5", "name": None}), "# 5")

    def test_export_progress_regex_matches_the_real_format(self):
        line = "  Fetching [########----------------]  33%  1,234 msgs  45/s ETA 1:23 "
        m = gui._RE_EXPORT_PROGRESS.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "33")
        self.assertIsNone(gui._RE_EXPORT_PROGRESS.search("Done in 0:04: 450 messages from 1/1 channels"))

    def test_full_username(self):
        self.assertEqual(gui._full_username({"username": "alice", "global_name": "Alice",
                                             "discriminator": "0"}), "alice")
        self.assertEqual(gui._full_username({"username": "alice", "global_name": "Alice"}), "alice")

        self.assertEqual(gui._full_username({"username": "bob", "discriminator": "4242"}), "bob#4242")
        self.assertEqual(gui._full_username(None), "Unknown")
        self.assertEqual(gui._full_username({}), "Unknown")

    def test_fetch_channel_tree(self):
        tree = gui.fetch_channel_tree(TOKEN, api_base=f"{self.mock.url}/api/v10")

        self.assertEqual(tree["me"], "alice")
        self.assertEqual(len(tree["dms"]), 1)
        self.assertEqual(tree["dms"][0]["label"], "Bob")
        self.assertIn("avatar_url", tree["dms"][0])
        self.assertIsNone(tree["dms"][0]["avatar_url"])
        self.assertEqual(tree["guilds"][0]["name"], "Test Guild")
        self.assertIn("icon_url", tree["guilds"][0])
        self.assertIsNone(tree["guilds"][0]["icon_url"])
        self.assertEqual(tree["guilds"][0]["channels"][0]["label"], "# general")
        self.assertEqual(tree["guilds"][0]["channels"][0]["type"], dx.CH_TEXT)

    def test_connect_account_is_the_fast_path_and_leaves_channels_for_later(self):
        api, ov = gui.connect_account(TOKEN, api_base=f"{self.mock.url}/api/v10")
        self.assertEqual(ov["me"], "alice")
        self.assertIn("me_avatar_url", ov)
        self.assertEqual(ov["dms"][0]["label"], "Bob")
        self.assertEqual(ov["guilds"][0]["name"], "Test Guild")
        self.assertIsNone(ov["guilds"][0]["channels"])
        self.assertFalse([p for p, q, a in self.mock.log if p.endswith("/channels") and "/guilds/" in p],
                         "connecting must not wait on per-server channel requests")
        chans = gui.load_guild_channels(api, ov["guilds"][0]["id"])
        self.assertEqual(chans[0]["label"], "# general")

    def test_connect_account_bad_token(self):
        with self.assertRaises(gui.dx.AuthError):
            gui.connect_account("wrong-token", api_base=f"{self.mock.url}/api/v10")

    def test_preview_reuses_the_connection_and_caches_server_names(self):
        api, _ = gui.connect_account(TOKEN, api_base=f"{self.mock.url}/api/v10")
        cache = {}
        before = len(self.mock.log)
        gui.fetch_preview(TOKEN, self.mock.CHANNEL, guild_id=self.mock.GUILD, limit=5, api=api, guild_cache=cache)
        first = [p for p, q, a in self.mock.log[before:]]
        self.assertIn(self.mock.GUILD, cache)
        self.assertTrue(any(p.endswith("/roles") for p in first))
        before = len(self.mock.log)
        msgs = gui.fetch_preview(TOKEN, self.mock.CHANNEL, guild_id=self.mock.GUILD, limit=5, api=api,
                                 guild_cache=cache)
        second = [p for p, q, a in self.mock.log[before:]]
        self.assertEqual(len(msgs), 5)
        self.assertEqual([p for p in second if not p.endswith("/messages")], [],
                         "second preview of the same server should only fetch messages")

    def test_fetch_avatar_bytes(self):
        data = gui.fetch_avatar_bytes(f"{self.mock.url}/cdn/file0.png")
        self.assertTrue(data and data.startswith(b"\x89PNG"))

    def test_fetch_avatar_bytes_missing_or_none(self):
        self.assertIsNone(gui.fetch_avatar_bytes(f"{self.mock.url}/cdn/gone.png"))
        self.assertIsNone(gui.fetch_avatar_bytes(None))
        self.assertIsNone(gui.fetch_avatar_bytes(""))

    def test_fetch_avatars_concurrent_correctness(self):
        urls = {"a": f"{self.mock.url}/cdn/file0.png", "b": f"{self.mock.url}/cdn/gone.png",
                "c": None, "d": f"{self.mock.url}/cdn/file1.png"}
        results = gui.fetch_avatars_concurrent(urls)
        self.assertEqual(set(results), {"a", "d"})
        for b in results.values():
            self.assertTrue(b.startswith(b"\x89PNG"))
        self.assertEqual(gui.fetch_avatars_concurrent({}), {})

    def test_fetch_avatars_concurrent_is_actually_parallel(self):
        from test_discord_export import PNG
        delay, n = 0.15, 8

        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            urls = {str(i): f"http://127.0.0.1:{port}/avatar{i}.png" for i in range(n)}
            t0 = time.monotonic()
            results = gui.fetch_avatars_concurrent(urls, workers=n)
            elapsed = time.monotonic() - t0
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(results), n)


        self.assertLess(elapsed, delay * n * 0.6, f"took {elapsed:.2f}s - looks sequential, not parallel")

    def test_normalize_date_field(self):
        cases = {
            "7/10/2026": "2026-07-10", "07/10/2026": "2026-07-10", "7-10-2026": "2026-07-10",
            "2026/07/10": "2026-07-10", "2026-07-10": "2026-07-10", "2026-07-10 14:30": "2026-07-10 14:30",
            "7d": "7d", "now": "now", "": "", "175928847299117063": "175928847299117063",
            "not a date": "not a date",
        }
        for raw, expected in cases.items():
            self.assertEqual(gui.normalize_date_field(raw), expected, raw)

    def test_normalize_date_field_fixes_the_reported_bug(self):
        normalized = gui.normalize_date_field("7/10/2026")
        sf = dx.parse_when(normalized, None)
        self.assertEqual(dx.snowflake_to_datetime(sf).astimezone().strftime("%Y-%m-%d"), "2026-07-10")
        with self.assertRaises(ValueError):
            dx.parse_when("7/10/2026", None)

    def test_fetch_channel_tree_bad_token(self):
        with self.assertRaises(dx.AuthError):
            gui.fetch_channel_tree("wrong-token", api_base=f"{self.mock.url}/api/v10")

    def test_build_export_argv(self):
        argv = gui.build_export_argv({
            "channel_id": "111", "out_dir": "/tmp/out", "formats": ["json", "csv"],
            "limit": "50", "after": "2026-01-01", "before": "2026-02-01",
            "media": "all", "reactors": True, "fresh": True, "tz": "local",
        })
        self.assertEqual(argv, [
            "-c", "111", "-o", "/tmp/out", "-f", "json,csv", "--media", "all", "--tz", "local",
            "-n", "50", "--after", "2026-01-01", "--before", "2026-02-01", "--reactors", "--fresh",
        ])

    def test_build_export_argv_minimal(self):
        argv = gui.build_export_argv({"channel_id": "111", "out_dir": "/tmp/out", "formats": ["json"]})
        self.assertEqual(argv, ["-c", "111", "-o", "/tmp/out", "-f", "json", "--media", "attachments", "--tz", "local"])

    def test_line_writer_splits_on_newline_and_carriage_return(self):
        lines = []
        w = gui._LineWriter(lines.append)
        w.write("hello "); w.write("world\nsecond"); w.write(" line\r")
        w.write("third\r\nfourth")

        self.assertEqual(lines, ["hello world", "second line", "third"])
        self.assertEqual(w._buf, "fourth")

    def test_run_export_job_success_streams_output_and_sets_rc(self):
        lines = []
        with mock.patch.object(dx.MediaDownloader, "_get_to_file", only_local_downloads), \
             mock.patch.object(dx, "_is_discord_cdn", lambda u: "127.0.0.1" in u):
            rc = gui.run_export_job(
                gui.build_export_argv({"channel_id": self.mock.CHANNEL, "out_dir": "/tmp/gui_out1",
                                       "formats": ["json"], "media": "none", "delay": 0,
                                       "api_base": f"{self.mock.url}/api/v10"}),
                TOKEN, on_line=lines.append)
        self.assertEqual(rc, 0)
        self.assertTrue(any("Done in" in l for l in lines))
        self.assertNotIn(TOKEN, "\n".join(lines))
        self.assertNotIn("DISCORD_TOKEN", os.environ)

    def test_run_export_job_bad_token_surfaces_message_and_rc(self):
        lines = []
        rc = gui.run_export_job(
            gui.build_export_argv({"channel_id": self.mock.CHANNEL, "out_dir": "/tmp/gui_out2",
                                   "formats": ["json"], "api_base": f"{self.mock.url}/api/v10"}),
            "totally-wrong", on_line=lines.append)
        self.assertNotEqual(rc, 0)
        self.assertTrue(any("401" in l for l in lines), lines)

    def test_run_export_job_stdout_restored_even_on_crash(self):
        old = sys.stdout
        with mock.patch.object(dx, "main", side_effect=RuntimeError("boom")):
            rc = gui.run_export_job(["-c", "1"], TOKEN, on_line=lambda l: None)
        self.assertEqual(rc, 1)
        self.assertIs(sys.stdout, old)


    def test_clamp_zoom_stays_within_bounds(self):
        self.assertEqual(gui.clamp_zoom(0), 0)
        self.assertEqual(gui.clamp_zoom(gui.ZOOM_MAX + 5), gui.ZOOM_MAX)
        self.assertEqual(gui.clamp_zoom(gui.ZOOM_MIN - 5), gui.ZOOM_MIN)

    def test_zoomed_pt_scales_but_never_shrinks_past_the_floor(self):
        self.assertEqual(gui.zoomed_pt(10, 0), 10)
        self.assertEqual(gui.zoomed_pt(10, 3), 13)
        self.assertEqual(gui.zoomed_pt(9, gui.ZOOM_MIN), gui.ZOOM_FLOOR_PT)

    def test_zoom_percent_matches_browser_style_10_percent_steps(self):
        self.assertEqual(gui.zoom_percent(0), 100)
        self.assertEqual(gui.zoom_percent(2), 120)
        self.assertEqual(gui.zoom_percent(-2), 80)


    def test_notify_export_finished_is_a_silent_noop_off_windows(self):
        with mock.patch.object(gui.subprocess, "Popen") as popen:
            self.assertFalse(gui.notify_export_finished(True, "Test Chat"))
            self.assertFalse(gui.notify_export_finished(False, ""))
        popen.assert_not_called()

    def test_notify_export_finished_beeps_on_windows_and_never_spawns_powershell(self):
        fake = types.SimpleNamespace(MB_OK=0, MB_ICONHAND=16, MessageBeep=mock.Mock())
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"winsound": fake}), \
             mock.patch.object(gui.subprocess, "Popen") as popen:
            self.assertTrue(gui.notify_export_finished(True, "x"))
            self.assertTrue(gui.notify_export_finished(False, "x"))
        self.assertEqual([c.args[0] for c in fake.MessageBeep.call_args_list], [0, 16])
        popen.assert_not_called()
        self.assertFalse(hasattr(gui, "build_toast_command"))


    def test_line_writer_reports_as_a_terminal_so_progress_updates_are_frequent(self):
        self.assertTrue(gui._LineWriter(lambda l: None).isatty())

    def test_raise_in_thread_interrupts_a_busy_thread(self):
        out = []
        started = threading.Event()
        def work():
            started.set()
            try:
                while True:
                    time.sleep(0.01)
            except KeyboardInterrupt:
                out.append("stopped")
        t = threading.Thread(target=work)
        t.start()
        started.wait(2)
        self.assertTrue(gui.raise_in_thread(t.ident, KeyboardInterrupt))
        t.join(2)
        self.assertEqual(out, ["stopped"])
        self.assertFalse(gui.raise_in_thread(None, KeyboardInterrupt))

    def test_cancelling_a_real_export_returns_130_and_restores_state(self):
        lines, result = [], {}
        started = threading.Event()
        def on_line(l):
            lines.append(l)
            started.set()
        def job():
            with mock.patch.object(dx.MediaDownloader, "_get_to_file", only_local_downloads), \
                 mock.patch.object(dx, "_is_discord_cdn", lambda u: "127.0.0.1" in u):
                result["rc"] = gui.run_export_job(
                    gui.build_export_argv({"channel_id": self.mock.CHANNEL, "out_dir": "/tmp/gui_cancel",
                                           "formats": ["json"], "media": "none", "delay": 0.2, "fresh": True,
                                           "api_base": f"{self.mock.url}/api/v10"}),
                    TOKEN, on_line=on_line)
        old = sys.stdout
        t = threading.Thread(target=job)
        t.start()
        self.assertTrue(started.wait(5))
        gui.raise_in_thread(t.ident, KeyboardInterrupt)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("rc"), 130)
        self.assertIs(sys.stdout, old)
        self.assertNotIn("DISCORD_TOKEN", os.environ)


if __name__ == "__main__":
    unittest.main(verbosity=2)
