import queue
import sys
import threading
import time
import types
import unittest
from unittest import mock


def _sample_png():
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((4, 4, 68, 68), fill=(255, 204, 77, 255))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


PNG = _sample_png()


class UFake:
    def __init__(self, *a, **k): self._opts = dict(k)
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return UFake()
    def __call__(self, *a, **k): return UFake()
    def __int__(self): return 100
    def __float__(self): return 100.0
    def __index__(self): return 100
    def __bool__(self): return True
    def __iter__(self): return iter(())
    def __len__(self): return 0
    def __getitem__(self, k): return UFake()
    def __setitem__(self, k, v): pass
    def __add__(self, o): return 100
    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __add__
    def __truediv__(self, o): return 100.0
    __rtruediv__ = __floordiv__ = __rfloordiv__ = __truediv__
    def __lt__(self, o): return False
    __le__ = __gt__ = __ge__ = __lt__
    def cget(self, k): return self._opts.get(k, UFake())
    def winfo_width(self): return 300
    def winfo_height(self): return 600
    def yview(self, *a): return (0.0, 1.0)
    def index(self, *a): return "5.0"


class FakeCanvas(UFake):
    def __init__(self, master=None, **k):
        super().__init__(**k)
        self.opts = dict(k)
        self.destroyed = False
    def cget(self, key): return self.opts.get(key, 0)
    def configure(self, **k): self.opts.update(k)
    def destroy(self): self.destroyed = True


class FakeVar:
    def __init__(self, master=None, value=None, **k):
        self._v, self._cbs = value, []
    def get(self): return self._v if self._v is not None else ""
    def set(self, v):
        self._v = v
        for cb in list(self._cbs):
            cb()
    def trace_add(self, mode, cb): self._cbs.append(lambda: cb())


class FakeText(UFake):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.buf = []
        self.configured_images = []
        self.deleted = []
        self.calls = []
    def insert(self, idx, text, *tags):
        self.buf.append(text)
        self.calls.append((text, tags[0] if tags else ()))
    def delete(self, *a):
        if a == ("1.0", "end"):
            self.buf = []
            self.calls = []
        else:
            self.deleted.append(a)
    def image_configure(self, mark, **kw): self.configured_images.append((mark, kw))
    def get(self, *a): return "".join(self.buf)
    def tag_prevrange(self, *a): return None


def install_fake_tk():
    tk = types.ModuleType("tkinter")
    for n in ("Frame", "Canvas", "Label", "Entry", "Toplevel", "PhotoImage", "Tk"):
        setattr(tk, n, type(n, (UFake,), {}))
    tk.Text = FakeText
    tk.StringVar, tk.BooleanVar = FakeVar, FakeVar
    tk.TclError = type("TclError", (Exception,), {})
    font = types.ModuleType("tkinter.font")
    font.Font = lambda **k: types.SimpleNamespace(measure=lambda s: 7 * len(s))
    font.families = lambda: ("Segoe UI", "Segoe UI Semibold", "Arial")
    tk.font = font
    ttk = types.ModuleType("tkinter.ttk")
    fd, mb, st = (types.ModuleType("tkinter." + n) for n in ("filedialog", "messagebox", "scrolledtext"))
    tk.ttk, tk.filedialog, tk.messagebox, tk.scrolledtext = ttk, fd, mb, st
    mods = {"tkinter": tk, "tkinter.font": font, "tkinter.ttk": ttk, "tkinter.filedialog": fd,
            "tkinter.messagebox": mb, "tkinter.scrolledtext": st}
    return mods


def load_gui():
    saved = {k: sys.modules.pop(k, None) for k in ("discord_export_gui", "restore_ui")}
    saved_tk = {k: sys.modules.get(k) for k in install_fake_tk()}
    sys.modules.update(install_fake_tk())
    import discord_export_gui as gui
    import restore_ui as ui
    return gui, ui


gui, ui = load_gui()


def msg(i, author="1", name="Alice", text="hello", t="2026-07-10T14:%02d:00", **kw):
    m = {"id": str(i), "type": 0, "system_text": None, "local_time": t % (i % 60),
         "author": {"id": author, "display_name": name, "avatar_url": f"https://cdn/{author}.png"},
         "reply_to": None, "content_text": text, "attachments": [], "embeds": [], "stickers": [],
         "poll": None, "reactions": []}
    m.update(kw)
    return m


class TestApp(unittest.TestCase):
    def setUp(self):
        ui.set_scale(1.0)
        self.app = gui.App(UFake())
        self.app.after = lambda *a, **k: "job"
        self.app.images._fetch_many = lambda urls: {}
        patcher = mock.patch.object(gui, "fetch_preview", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)

    def drain(self):
        lines = []
        while True:
            try:
                m = self.app.queue.get_nowait()
            except queue.Empty:
                break
            self.app._handle(m, lines)
        self.app._append_log_lines(lines)
        return lines

    def test_constructs_and_poll_survives_a_bad_message(self):
        self.app.queue.put(("nonsense",))
        self.app.queue.put(("progress", "bad", None))
        self.app._poll()

    OVERVIEW = {"me": "alice", "me_id": "77", "me_avatar_url": "https://cdn/me.png",
                "dms": [{"id": "5", "label": "Bob", "avatar_url": None}],
                "guilds": [{"id": "9", "name": "Guild", "icon_url": None, "channels": None}]}

    def wait_queue(self):
        for _ in range(150):
            if not self.app.queue.empty():
                return
            time.sleep(0.02)

    def test_connect_is_fast_then_channels_fill_in_and_avatar_shows(self):
        self.app.token_var.set("tok")
        api = object()
        with mock.patch.object(gui, "connect_account", return_value=(api, dict(self.OVERVIEW))), \
             mock.patch.object(gui, "load_guild_channels",
                               return_value=[{"id": "7", "label": "# general", "type": 0}]) as load:
            self.app._on_connect()
            self.wait_queue()
            self.drain()
            self.assertEqual(self.app._token, "tok")
            self.assertIs(self.app._api, api)
            self.assertEqual(self.app._status_state, "ok")
            self.assertIsNotNone(self.app._me_photo)
            for _ in range(100):
                if "9" in self.app._chan_done or not self.app.queue.empty():
                    self.drain()
                if "9" in self.app._chan_done:
                    break
                time.sleep(0.02)
            self.assertIn("9", self.app._chan_done)
            load.assert_called_with(api, "9")
        self.assertEqual(self.app.sidebar._guilds[0]["channels"][0]["label"], "# general")
        self.app._on_tree(self.app._conn_gen - 1, "old", object(), self.OVERVIEW)
        self.assertEqual(self.app._token, "tok")

    def test_expanding_an_unloaded_server_asks_for_it_immediately(self):
        self.app._api = object()
        with mock.patch.object(gui, "load_guild_channels", return_value=[]) as load:
            self.app._request_channels_now("9")
            self.wait_queue()
        load.assert_called_once()

    def test_paste_connects_straight_away(self):
        self.app.master = mock.Mock()
        self.app.master.clipboard_get.return_value = "  pasted-token  "
        with mock.patch.object(self.app, "_on_connect") as conn:
            self.app._paste_token()
        self.assertEqual(self.app.token_var.get(), "pasted-token")
        conn.assert_called_once()

    def test_how_to_get_token_button_opens_the_video_and_there_is_no_copy_button(self):
        self.assertNotIn("copy_btn", vars(self.app))
        self.assertNotIn("_copy_token", dir(type(self.app)))
        self.assertEqual(self.app.help_token_btn._text, "How to get token")
        with mock.patch.object(gui.webbrowser, "open", return_value=True) as opened:
            self.app._open_token_help()
        opened.assert_called_once_with("https://youtu.be/dBjdGnwBHBY?si=bEeHm7nmdT3Gepvk")
        with mock.patch.object(gui.webbrowser, "open", return_value=False), \
             mock.patch.object(self.app, "_toast") as toast:
            self.app._open_token_help()
        self.assertIn("youtu.be/dBjdGnwBHBY", toast.call_args.args[0])

    def test_a_pasted_token_autoconnects_once_and_a_bad_one_is_not_retried(self):
        tok = "A" * 24 + "." + "b" * 6 + "." + "c" * 30
        scheduled = []
        self.app.after = lambda ms, fn=None, *a: scheduled.append(fn) or "job"
        self.app.token_var.set(tok)
        self.assertEqual(len(scheduled), 1)
        with mock.patch.object(self.app, "_on_connect") as conn:
            scheduled[0]()
        conn.assert_called_once()
        self.app._failed_token = tok
        self.app.token_var.set(tok + "")
        self.assertEqual(len(scheduled), 1)
        self.app.token_var.set("short")
        self.assertEqual(len(scheduled), 1)

    def test_connect_without_token_does_not_start(self):
        self.app._on_connect()
        self.assertFalse(self.app._connecting)

    def test_connect_auth_error_reports_and_recovers(self):
        self.app.token_var.set("bad")
        with mock.patch.object(gui, "connect_account", side_effect=gui.dx.AuthError(401, "bad token")):
            self.app._on_connect()
            for _ in range(100):
                if not self.app.queue.empty():
                    break
                time.sleep(0.02)
            self.drain()
        self.assertFalse(self.app._connecting)
        self.assertIsNone(self.app._token)
        self.assertEqual(self.app._failed_token, "bad")

    def test_pick_chats_and_render_preview_grouping(self):
        self.app._token = "tok"
        msgs = [msg(1), msg(2, text="same author soon after https://x.io/a."),
                msg(3, author="2", name="Bob", text="@Alice hi", attachments=[{"filename": "a.png"}],
                    reactions=[{"emoji": {"display": "x"}, "count": 2}]),
                msg(4, reply_to={"author": "Alice", "deleted": False, "content_preview": "hello"},
                    embeds=[{"title": "T", "description": "D"}], stickers=[{"name": "s"}],
                    poll={"question": "q?"}),
                msg(5, system_text="joined", type=7)]
        with mock.patch.object(gui, "fetch_preview", return_value=msgs):
            self.app._on_sidebar_pick("dm", {"id": "5", "label": "Bob", "avatar_url": None})
            for _ in range(100):
                if not self.app.queue.empty():
                    break
                time.sleep(0.02)
            self.drain()
        text = self.app.preview.get()
        self.assertEqual([label for _c, label in self.app._seps], ["Friday, July 10, 2026"])
        self.assertNotIn("\u2500", text)

        self.assertEqual(text.count("Alice"), 4)
        self.assertIn("same author soon after", text)
        self.assertIn("a.png", text)

        self.app._on_sidebar_pick("dm", {"id": "5", "label": "Bob", "avatar_url": None})
        self.assertEqual(self.app._preview_cache.__len__(), 1)
        self.app._on_sidebar_pick("chan", {"id": "7", "label": "# general", "guild_id": "9", "guild_name": "G",
                                            "guild_icon": None})
        self.assertEqual(self.app.channel_var.get(), "7")

    def test_preview_error_and_images_swap_in_place(self):
        self.app._current = {"id": "5", "guild_id": None}
        self.app._preview_gen = 3
        self.app._on_preview_error(3, "5", "boom")
        self.assertIn("boom", self.app.preview.get())
        self.app._render_preview([msg(1)])
        url = self.app._preview_urls[0]
        with mock.patch.object(self.app, "_render_preview") as redraw:
            self.app.images.raw[url] = PNG
            self.app._handle(("images", {url: PNG}), [])
        redraw.assert_not_called()
        self.assertEqual(len(self.app.preview.configured_images), 1)

    def test_no_line_starts_with_a_coloured_run(self):
        colored = {"attach", "bar", "mention", "react", "embed", "link"}
        msgs = [msg(1, text="@Alice first word is a mention", attachments=[{"filename": "a.png"}],
                    embeds=[{"title": "T", "description": "D"}], stickers=[{"name": "s"}], poll={"question": "q?"},
                    reactions=[{"emoji": {"display": "x"}, "count": 2}])]
        with mock.patch.object(gui.ui, "can_render_images", return_value=False):
            self.app._render_preview(msgs)
        at_start = True
        for text, tags in self.app.preview.calls:
            if at_start and text != "\n":
                self.assertFalse(colored & set(tags), (text, tags))
            at_start = text.endswith("\n")
        self.assertIn("\u200a", "".join(t for t, _ in self.app.preview.calls))

    def test_day_dividers_are_real_widgets_that_resize_and_are_cleaned_up(self):
        made = []
        def make_canvas(*a, **k):
            c = FakeCanvas(*a, **k); made.append(c); return c
        with mock.patch.object(gui.tk, "Canvas", make_canvas):
            self.app._render_preview([msg(1, t="2026-07-10T14:%02d:00"), msg(2, t="2026-07-11T09:%02d:00"),
                                      msg(3, t="2026-07-11T09:%02d:00")])
            self.assertEqual([lbl for _c, lbl in self.app._seps], ["Friday, July 10, 2026", "Saturday, July 11, 2026"])
            self.assertEqual(len(made), 2)
            self.app.preview.winfo_width = lambda: 900
            self.app._on_preview_resize()
            self.assertTrue(all(int(c.cget("width")) > 800 for c in made))
            self.app._render_preview([msg(1)])
            self.assertTrue(all(c.destroyed for c in made[:2]))
            self.assertEqual(len(self.app._seps), 1)

    def test_reactions_become_chips_and_content_emoji_become_pictures(self):
        m = msg(1, text="nice \U0001F44D\U0001F3FD and \u2764\ufe0f ok",
                reactions=[{"emoji": {"id": None, "name": "\U0001F602", "animated": False, "display": "\U0001F602"}, "count": 3},
                           {"emoji": {"id": "99", "name": "pepe", "animated": False, "display": ":pepe:"}, "count": 12}])
        self.app._render_preview([m])
        urls = self.app._preview_urls
        self.assertIn(gui.TWEMOJI_BASE + "1f44d-1f3fd.png", urls)
        self.assertIn(gui.TWEMOJI_BASE + "2764.png", urls)
        self.assertIn(gui.TWEMOJI_BASE + "1f602.png", urls)
        self.assertTrue(any("/emojis/99.png" in u for u in urls))
        self.assertEqual(sum(1 for sl in self.app._slots if sl["char"]), 2)
        self.assertNotIn("\U0001F602", self.app.preview.get())

    def test_failed_emoji_download_falls_back_to_the_plain_character(self):
        self.app._render_preview([msg(1, text="hi \U0001F44D")])
        sl = next(x for x in self.app._slots if x["char"])
        self.app.images.failed.add(sl["urls"][0])
        self.app._refresh_slots()
        self.assertTrue(self.app.preview.deleted)
        self.assertIn("\U0001F44D", self.app.preview.get())
        self.assertTrue(sl["done"])

    def test_chip_keeps_its_width_when_the_emoji_arrives(self):
        e = {"id": None, "name": "\U0001F602", "animated": False, "display": "\U0001F602"}
        url = gui.reaction_emoji_url(e)
        before = ui.render_chip(None, "", "7", 26).size
        after = ui.render_chip(PNG, "", "7", 26).size
        self.assertEqual(before, after)
        self.assertEqual(self.app._chip_photo(e, 7) is not None, True)
        self.app.images.raw[url] = PNG
        self.assertIsNotNone(self.app._chip_photo(e, 7))

    def test_add_manual_chat_validation(self):
        self.app.manual_id_var.set("abc")
        self.app._on_add_manual_chat()
        self.assertEqual(self.app.channel_var.get(), "")
        self.app.manual_id_var.set("123456")
        self.app._on_add_manual_chat()
        self.assertEqual(self.app.channel_var.get(), "123456")

    def test_export_values_validation(self):
        with self.assertRaises(ValueError):
            self.app._export_values()
        self.app._token = "t"
        with self.assertRaises(ValueError):
            self.app._export_values()
        self.app.channel_var.set("42")
        self.app.limit_var.set("x")
        with self.assertRaises(ValueError):
            self.app._export_values()
        self.app.limit_var.set("10")
        self.app.after_var.set("not a date")
        with self.assertRaises(ValueError):
            self.app._export_values()
        self.app.after_var.set("7/10/2026")
        v = self.app._export_values()
        self.assertEqual(v["after"], "2026-07-10")
        self.assertEqual(v["formats"], ["json", "html"])
        self.assertEqual(v["media"], "attachments")

    def test_export_lifecycle_progress_done_and_sound_only(self):
        self.app._token = "t"
        self.app.channel_var.set("42")
        def fake_job(argv, token, on_line):
            on_line("  Fetching [########----------------]  33%  1,234 of 3,700 msgs  45/s ETA 1:23 ")
            on_line("Done in 0:04: 450 messages")
            return 0
        with mock.patch.object(gui, "run_export_job", fake_job), \
             mock.patch.object(gui, "notify_export_finished", return_value=True) as snd, \
             mock.patch.object(gui.subprocess, "Popen") as popen:
            self.app._on_start()
            self.app._thread.join(2)
            lines = self.drain()
        self.assertTrue(any("Done in" in l for l in lines))
        self.assertFalse(any("Fetching" in l for l in lines))
        self.assertFalse(self.app._running)
        snd.assert_called_once()
        popen.assert_not_called()

    def test_progress_text_is_phase_aware_and_the_bar_never_goes_back(self):
        app = self.app
        app._running = True
        app._phases = ["Fetching", "Media"]
        app._overall_max = 0.0
        d = {"label": "Fetching", "pct": 50, "done": 1500, "total": 3000, "unit": "msgs", "eta": "1:30", "rate": "45/s"}
        app._on_progress(d)
        text = app.export_status_var.get()
        self.assertIn("Step 1 of 2: Messages", text)
        self.assertIn("1,500 of 3,000 msgs", text)
        self.assertIn("about 1:30 left", text)
        first = app._overall_max
        app._on_progress({**d, "label": "Media", "pct": 0, "done": 0, "total": None, "unit": "files", "eta": None})
        self.assertGreaterEqual(app._overall_max, first)
        app._on_progress({**d, "pct": 10})
        self.assertGreaterEqual(app._overall_max, first)
        app._on_progress({**d, "pct": 60, "eta": None})
        self.assertIn("estimating time left", app.export_status_var.get())

    def test_cancel_stops_a_running_export_and_skips_the_sound(self):
        self.app._token = "t"
        self.app.channel_var.set("42")
        started = threading.Event()
        def slow_job(argv, token, on_line):
            started.set()
            try:
                while True:
                    time.sleep(0.01)
            except KeyboardInterrupt:
                return 130
        with mock.patch.object(gui, "run_export_job", slow_job), \
             mock.patch.object(gui, "notify_export_finished", return_value=True) as snd:
            self.app._on_start()
            self.assertTrue(started.wait(2))
            self.app._on_start_or_cancel()
            self.app._thread.join(3)
            self.assertFalse(self.app._thread.is_alive())
            self.drain()
        self.assertFalse(self.app._running)
        snd.assert_not_called()

    def test_failed_export_plays_error_sound_and_opens_log(self):
        self.app._running = True
        with mock.patch.object(gui, "notify_export_finished", return_value=True) as snd:
            self.app._on_export_done(2)
        snd.assert_called_once_with(False, "")
        self.assertTrue(self.app._log_open)

    def test_log_is_bounded(self):
        self.app._append_log_lines([f"line {i}" for i in range(5)])
        self.assertEqual(self.app.log_summary_var.get(), "line 4")

    def test_zoom_and_escape_and_close(self):
        self.app._adjust_zoom(2)
        self.assertEqual(self.app.zoom_var.get(), "120%")
        self.app._adjust_zoom(reset=True)
        self.app.search_var.set("abc")
        self.app._on_escape()
        self.assertEqual(self.app.search_var.get(), "")
        self.app.master.destroy = mock.Mock()
        self.app._on_close()
        self.app.master.destroy.assert_called_once()

    def test_tk_error_hook_logs_instead_of_raising(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            self.app._on_tk_error(*sys.exc_info())
        self.assertIn("boom", self.app.log.get())


class TestWidgetLogic(unittest.TestCase):
    def make_list(self, dms, guilds):
        images = ui.ImageStore(lambda d: {}, lambda g: None)
        picks = []
        sl = ui.SidebarList(UFake(), images, lambda k, i: picks.append((k, i)))
        sl.after_idle = lambda f: None
        sl.canvasy = lambda y: y
        sl.set_data(dms, guilds)
        return sl, picks

    DMS = [{"id": str(i), "label": n, "avatar_url": None} for i, n in enumerate(["Ann", "Bob", "Cy"])]
    GUILDS = [{"id": "9", "name": "Alpha", "icon_url": None,
               "channels": [{"id": "1", "label": "# general", "type": 0}, {"id": "2", "label": "\U0001F50A Lounge", "type": 2}]},
              {"id": "8", "name": "Beta", "icon_url": None, "channels": []}]

    def kinds(self, sl):
        return [r["kind"] for r in sl._rows]

    def test_tabs_show_dms_or_servers_never_both(self):
        sl, picks = self.make_list(self.DMS, self.GUILDS)
        self.assertEqual(self.kinds(sl), ["pad", "dm", "dm", "dm"])
        sl.set_mode("server")
        self.assertEqual(self.kinds(sl), ["pad", "guild", "guild"])
        sl.set_mode("dm")
        sl._on_click(types.SimpleNamespace(y=sl._ys[1] + 1))
        self.assertEqual(picks[-1][0], "dm")

    def test_server_expand_click_filter_and_loading_rows(self):
        guilds = [dict(g) for g in self.GUILDS] + [{"id": "7", "name": "Gamma", "icon_url": None, "channels": None}]
        sl, picks = self.make_list(self.DMS, guilds)
        needed = []
        sl.on_need_channels = needed.append
        sl.set_mode("server")
        sl._on_click(types.SimpleNamespace(y=sl._ys[1] + 1))
        self.assertEqual(self.kinds(sl).count("chan"), 2)
        gamma = next(i for i, r in enumerate(sl._rows) if r["kind"] == "guild" and r["guild"]["id"] == "7")
        sl._on_click(types.SimpleNamespace(y=sl._ys[gamma] + 1))
        self.assertEqual(needed, ["7"])
        self.assertIn("loading", self.kinds(sl))
        sl.set_guild_channels("7", [{"id": "70", "label": "# hello", "type": 0}])
        self.assertNotIn("loading", self.kinds(sl))
        sl._on_click(types.SimpleNamespace(y=sl._ys[2] + 1))
        self.assertEqual(picks[-1][0], "chan")
        self.assertEqual(picks[-1][1]["guild_name"], "Alpha")
        sl.set_filter("lounge")
        self.assertEqual(self.kinds(sl), ["pad", "guild", "chan"])
        sl.set_filter("zzz")
        self.assertEqual(self.kinds(sl), ["pad", "note"])

    def test_counts_are_reported_for_the_tab_labels(self):
        sl, _ = self.make_list(self.DMS, self.GUILDS)
        seen = []
        sl.on_counts = lambda d, g: seen.append((d, g))
        sl.set_filter("a")
        self.assertEqual(seen[-1][0], 1)
        sl.set_filter("")
        self.assertEqual(seen[-1], (3, 2))

    def test_empty_notes_and_add_dm(self):
        sl, _ = self.make_list([], [])
        self.assertEqual(self.kinds(sl), ["pad", "note"])
        sl.add_dm({"id": "5", "label": "New", "avatar_url": None})
        sl.add_dm({"id": "5", "label": "New", "avatar_url": None})
        self.assertEqual(self.kinds(sl).count("dm"), 1)

    def test_draw_runs_for_every_row_kind(self):
        sl, _ = self.make_list(self.DMS, self.GUILDS)
        sl.set_mode("server")
        sl._expanded.add("9")
        sl._rebuild()
        sl._sel = ("chan", "1")
        sl._hover = 2
        sl._draw()

    def test_image_store_requests_once_and_ingests(self):
        got = []
        store = ui.ImageStore(lambda d: {u: b"img" for u in d}, got.append)
        self.assertEqual(store.request(["a", "a", None, "b"]), 2)
        for _ in range(100):
            if got:
                break
            time.sleep(0.02)
        store.ingest(got[0])
        self.assertTrue(store.has("a"))
        self.assertEqual(store.request(["a", "b"]), 0)

    def test_button_chip_and_progress_state(self):
        b = ui.Button(UFake(), "Go", command=lambda: calls.append(1))
        calls = []
        b.invoke(); b.set_enabled(False); b.invoke()
        self.assertEqual(calls, [1])
        chips = ui.ChipGroup(UFake(), [("a", "A"), ("b", "B")], ["a"], multi=True)
        chips._clicked("a")
        self.assertEqual(chips.get(), ["a"])
        chips._clicked("b")
        self.assertEqual(sorted(chips.get()), ["a", "b"])
        chips.set_label("a", "A 3")
        self.assertEqual(chips._buttons["a"]._text, "A 3")
        single = ui.ChipGroup(UFake(), [("x", "X"), ("y", "Y")], "x")
        single._clicked("y")
        self.assertEqual(single.get(), "y")
        b2 = ui.Button(UFake(), "x", variant="chip_off", trigger="press")
        b2.set_variant("chip_on", animate=True)
        mid = b2._shown_colors()
        b2._anim_t = 1.0
        b2._anim_from = None
        self.assertEqual(b2._shown_colors()[2], ui.Button.VARIANTS["chip_on"]["outline"])
        self.assertEqual(len(mid), 3)
        pb = ui.ProgressBar(UFake())
        pb.set_determinate(150)
        self.assertEqual(pb._value, 100.0)


class TestHelpers(unittest.TestCase):
    def test_emoji_parsing_and_twemoji_names(self):
        self.assertEqual(gui.split_emoji("hi \U0001F44D!"), [("hi ", False), ("\U0001F44D", True), ("!", False)])
        self.assertEqual(gui.split_emoji("plain text"), [("plain text", False)])
        self.assertEqual(gui.split_emoji("\U0001F1FA\U0001F1F8"), [("\U0001F1FA\U0001F1F8", True)])
        fam = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
        self.assertEqual(gui.split_emoji(fam), [(fam, True)])
        self.assertEqual(gui.twemoji_name("\U0001F44D"), "1f44d")
        self.assertEqual(gui.twemoji_name("\u2764\ufe0f"), "2764")
        self.assertEqual(gui.twemoji_name("\u2764\ufe0f\u200D\U0001F525"), "2764-fe0f-200d-1f525")
        self.assertEqual(gui.twemoji_name("\U0001F1FA\U0001F1F8"), "1f1fa-1f1f8")
        self.assertEqual(gui.reaction_emoji_url({"id": "5", "animated": True, "name": "x"}).split("?")[0].endswith("/5.gif"), True)
        self.assertIsNone(gui.reaction_emoji_url({"id": None, "name": None}))

    def test_image_store_remembers_failures(self):
        got = []
        store = ui.ImageStore(lambda d: {}, got.append)
        store.request(["http://x/a.png"])
        for _ in range(100):
            if got:
                break
            time.sleep(0.02)
        self.assertTrue(store.is_failed("http://x/a.png"))
        store.forget_failures()
        self.assertFalse(store.is_failed("http://x/a.png"))

    def test_looks_like_token(self):
        self.assertTrue(gui.looks_like_token("A" * 24 + "." + "b" * 6 + "." + "c" * 30))
        self.assertTrue(gui.looks_like_token("mfa." + "x" * 80))
        for bad in ("", "short", "has spaces " * 10, "x" * 49, "é" * 60):
            self.assertFalse(gui.looks_like_token(bad), bad)

    def test_fonts_and_color_mix(self):
        ui.init_fonts()
        self.assertEqual(ui.FONT, "Segoe UI")
        self.assertEqual(ui.fnt(10, True), ("Segoe UI Semibold", 10))
        self.assertEqual(ui.fnt(10), ("Segoe UI", 10))
        self.assertEqual(ui.mix("#000000", "#ffffff", 0.5), "#808080")

    def test_overall_percent_is_monotonic_across_phases(self):
        ph = gui.active_phases(True, "attachments")
        self.assertEqual(ph, ["Fetching", "Reaction users", "Media"])
        self.assertEqual(gui.active_phases(False, "none"), ["Fetching"])
        vals = [gui.overall_percent(ph, p, x) for p in ph for x in (0, 50, 100)]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(gui.overall_percent(["Fetching"], "Fetching", 100), 100.0)
        self.assertAlmostEqual(gui.overall_percent(ph, "Media", 100), 100.0)

    def test_split_rich(self):
        self.assertEqual(gui.split_rich("hi @Bob see https://a.io/x. ok #general"),
                         [("hi ", None), ("@Bob", "mention"), (" see ", None), ("https://a.io/x", "link"),
                          (".", None), (" ok ", None), ("#general", "mention")])
        self.assertEqual(gui.split_rich("plain"), [("plain", None)])
        self.assertEqual(gui.split_rich("mail a@b.com"), [("mail a@b.com", None)])

    def test_msg_minutes_and_pretty_day(self):
        a, b = gui.msg_minutes("2026-07-10T14:30:00+03:00"), gui.msg_minutes("2026-07-10T14:35:59")
        self.assertEqual(b - a, 5)
        self.assertIsNone(gui.msg_minutes("garbage"))
        self.assertEqual(gui.pretty_day("2026-07-04T01:00:00"), "Saturday, July 4, 2026")
        self.assertEqual(gui.pretty_day("nonsense!!!"), "nonsense!!")

    def test_parse_progress_line_on_real_exporter_output(self):
        dx = gui.dx
        lines = []
        with mock.patch.object(sys, "stderr") as err:
            err.isatty.return_value = True
            p = dx.Progress("  Fetching", 100)
            p.last_draw = 0
            p.update(33)
            out = "".join(c.args[0] for c in err.write.call_args_list)
        line = out.replace("\r", "").rstrip()
        pct, text = gui.parse_progress_line(line)
        self.assertEqual(pct, 33)
        self.assertTrue(text.startswith("Fetching 33%"), text)
        self.assertEqual(gui.parse_progress_line("  Media 12 files  3/s  0:04"), (None, "Media 12 files · 3/s · 0:04"))
        for not_progress in ("Done in 0:04: 450 messages from 1/1 channels", "Fetching: 5 new messages (9 cached)",
                             "Token was rejected mid-run (401)."):
            self.assertIsNone(gui.parse_progress_line(not_progress), not_progress)


if __name__ == "__main__":
    unittest.main()
