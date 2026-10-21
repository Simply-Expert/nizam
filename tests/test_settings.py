import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from urllib.parse import parse_qs, urlparse

from nizam import providers, server, settings as S, state, telegram as T, terminal


class Board:
    def __init__(self):
        self.persist = state.Persisted()
        self.invalidated = 0

    def invalidate(self):
        self.invalidated += 1

    def snapshot(self):
        return {"agents": [], "sessions": []}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.login = False
        for p in (mock.patch.object(state, "STATE_FILE", self.tmp / "state.json"),
                  mock.patch.object(T, "CONFIG_FILE", self.tmp / "telegram.json"),
                  mock.patch.object(T, "NIZAM_DIR", self.tmp),
                  mock.patch.object(S, "login_enabled", lambda: self.login),
                  mock.patch.object(S, "set_login", lambda on: setattr(self, "login", on)),
                  mock.patch.object(S, "app_running", lambda: True),
                  mock.patch.object(S.update, "poll", lambda: {"current": "v0.2.5", "latest": None})):
            p.start()
            self.addCleanup(p.stop)
        self.board = Board()


class Settings(Case):
    def test_defaults(self):
        s = S.read(self.board)
        self.assertEqual((s["app"]["login"], s["app"]["badge"], s["app"]["launcher"], s["followups"]["hour"]),
                         (False, True, "Terminal", "10:00"))
        self.assertEqual((s["telegram"]["bot"], s["telegram"]["notify"], s["telegram"]["away_minutes"]),
                         (None, "off", 5))

    def test_changes_land_where_their_owners_keep_them(self):
        T.save_config({"token": "TOKEN", "bot": "nizam_bot", "users": [1]})
        S.apply(self.board, {"launcher": "Ghostty", "followup_hour": "8pm", "login": True,
                             "notify": "away", "away_minutes": "10", "idle_hours": 0, "permission_mode": "plan"})
        self.assertEqual((self.board.persist.data["prefs"]["launcher"], self.board.persist.data["prefs"]["followup_hour"]),
                         ("Ghostty", "20:00"))
        self.assertTrue(self.login)
        cfg = T.load_config()
        self.assertEqual((cfg["notify"], cfg["away_minutes"], cfg["idle_hours"], cfg["permission_mode"], cfg["token"]),
                         ("away", 10, 0, "plan", "TOKEN"))
        self.assertEqual(S.read(self.board)["telegram"]["paired"], 1)

    def test_bad_values_are_refused(self):
        for change in ({"launcher": "Hyper"}, {"followup_hour": "teatime"}, {"permission_mode": "yolo"},
                       {"idle_hours": -1}, {"nope": 1}):
            with self.subTest(change=change):
                self.assertRaises(ValueError, S.apply, self.board, change)

    def test_messaging_needs_the_bot_but_waking_does_not(self):
        self.assertRaisesRegex(ValueError, "Connect the Telegram bot", S.apply, self.board, {"notify": "always"})
        S.apply(self.board, {"permission_mode": "default"})
        self.assertEqual(T.load_config(), {"permission_mode": "default"})

    def test_the_claude_app_keeps_the_terminal_it_replaces(self):
        S.apply(self.board, {"launcher": "Ghostty"})
        S.apply(self.board, {"launcher": "Claude"})
        s = S.read(self.board)["app"]
        self.assertEqual((s["launcher"], s["terminal"]), ("Claude", "Ghostty"))
        S.apply(self.board, {"terminal": "Terminal"})
        self.assertEqual(terminal.terminal_for(self.board.persist.data["prefs"]), "Terminal")
        self.assertRaises(ValueError, S.apply, self.board, {"terminal": "Claude"})


class DesktopLaunch(unittest.TestCase):
    def test_url_carries_folder_and_prompt(self):
        url = providers.get("claude").desktop_new_url("/Users/me/work/my app", "راجع & أرسل")
        u = urlparse(url)
        self.assertEqual((u.scheme, u.netloc, u.path), ("claude", "code", "/new"))
        self.assertEqual(parse_qs(u.query), {"folder": ["/Users/me/work/my app"], "q": ["راجع & أرسل"],
                                             "source": ["nizam"]})
        self.assertNotIn("q=", providers.get("claude").desktop_new_url("/w"))

    def test_start_opens_the_app_instead_of_a_terminal(self):
        with mock.patch.object(terminal.subprocess, "Popen") as popen, \
                mock.patch.object(terminal, "run_in_new_terminal") as term:
            self.assertIsNone(terminal.start("/w", "hi", launcher="Claude"))
            self.assertTrue(popen.call_args[0][0][1].startswith("claude://code/new?folder=%2Fw&q=hi"))
            term.assert_not_called()


class Routes(Case):
    def setUp(self):
        super().setUp()
        with mock.patch.object(server, "PORT_FILE", self.tmp / "port"):
            self.httpd = server.make_server(self.board, 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_read_and_change(self):
        code, s = self.call("/api/settings")
        self.assertEqual((code, s["app"]["launcher"]), (200, "Terminal"))
        code, r = self.call("/api/settings", {"launcher": "Ghostty"})
        self.assertEqual((code, r["settings"]["app"]["launcher"]), (200, "Ghostty"))
        code, r = self.call("/api/settings", {"followup_hour": "teatime"})
        self.assertEqual(code, 400)
        self.assertTrue(r["error"][0].isupper())

    def test_pair_and_disconnect(self):
        code, r = self.call("/api/settings/telegram-pair", {"code": "NOPE00"})
        self.assertEqual((code, r["error"]), (400, "No bot is set up; run: nizam telegram setup <token>"))
        T.save_config({"token": "TOKEN", "bot": "nizam_bot"})
        code, r = self.call("/api/settings/telegram-disconnect", {})
        self.assertEqual((code, r["settings"]["telegram"]["bot"]), (200, None))
        self.assertFalse(T.CONFIG_FILE.exists())


if __name__ == "__main__":
    unittest.main()
