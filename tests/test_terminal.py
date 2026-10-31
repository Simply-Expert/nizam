import unittest
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import terminal as T


class FakeGhostty:
    """Terminals by id; a title written to a tty shows on the terminal attached to it."""

    def __init__(self, ttys: dict[str, str]):
        self.ttys = ttys
        self.names = {tid: f"✳ {tid}" for tid in ttys.values()}
        self.writes: list[tuple[str, str]] = []
        self.focused = None

    def set_title(self, tty, title):
        self.writes.append((tty, title))
        if tty in self.ttys:
            self.names[self.ttys[tty]] = title
        return True

    def osascript(self, script):
        if script == T._GHOSTTY_NAMES:
            return "".join(f"{tid}\t{name}\n" for tid, name in self.names.items())
        tid = script.split('terminal id "')[1].split('"')[0]
        if tid in self.names:
            self.focused = tid
            return "found"
        return ""


class GhosttyJump(unittest.TestCase):
    def setUp(self):
        self.g = FakeGhostty({"ttys001": "A", "ttys002": "B"})
        self.patches = [mock.patch.object(T, "_set_title", self.g.set_title),
                        mock.patch.object(T, "_osascript", self.g.osascript),
                        mock.patch.dict(T._ghostty_ids, clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_the_terminal_is_found_by_its_tty_and_keeps_its_title(self):
        self.assertTrue(T.focus_ghostty_tty(10, "ttys002"))
        self.assertEqual(self.g.focused, "B")
        self.assertEqual(self.g.names["B"], "✳ B")

    def test_a_second_jump_does_not_touch_the_title(self):
        T.focus_ghostty_tty(10, "ttys002")
        self.g.writes.clear()
        self.assertTrue(T.focus_ghostty_tty(10, "ttys002"))
        self.assertEqual(self.g.writes, [])

    def test_a_closed_terminal_is_looked_up_again(self):
        T._ghostty_ids[(10, "ttys002")] = "GONE"
        self.assertTrue(T.focus_ghostty_tty(10, "ttys002"))
        self.assertEqual(T._ghostty_ids[(10, "ttys002")], "B")

    def test_a_tty_ghostty_does_not_show_gets_its_mark_cleared(self):
        self.assertFalse(T.focus_ghostty_tty(10, "ttys009"))
        self.assertEqual(self.g.writes[-1], ("ttys009", ""))

    def test_only_tty_names_are_written_to(self):
        self.assertFalse(T.focus_ghostty_tty(10, "../disk0"))
        self.assertEqual(self.g.writes, [])




class OpenSession(unittest.TestCase):
    def session(self, **kw):
        return {"id": "abc-123", "live": True, "pid": 42, "cwd": "/w", "title": "t", "auto_title": "t",
                "provider": "claude", "bucket": "inbox", "runtime_status": "idle", "remote_url": None,
                "remind_due": False, **kw}

    def open(self, s, background=False, released=True, focused=False):
        with mock.patch("nizam.remote.background", return_value=background), \
             mock.patch("nizam.remote.release", return_value=released) as release, \
             mock.patch.object(T, "focus", return_value=focused) as focus, \
             mock.patch.object(T, "resume", return_value=True) as resume, \
             mock.patch.object(T, "open_in_desktop", return_value=False), \
             mock.patch.object(T.subprocess, "Popen") as popen:
            ok = T.open_session(s, "Ghostty")
        return ok, release, focus, resume, popen

    def test_a_background_session_between_turns_moves_to_a_window(self):
        ok, release, focus, resume, popen = self.open(self.session(remote_url="https://x"), background=True)
        self.assertTrue(ok)
        release.assert_called_once_with(42)
        resume.assert_called_once()
        focus.assert_not_called()
        popen.assert_not_called()

    def test_a_working_background_session_opens_its_link_instead(self):
        ok, release, _, resume, popen = self.open(
            self.session(bucket="working", runtime_status="busy", remote_url="https://x"), background=True)
        self.assertTrue(ok)
        release.assert_not_called()
        resume.assert_not_called()
        self.assertEqual(popen.call_args[0][0], ["open", "https://x"])

    def test_a_live_session_with_no_window_is_never_resumed_twice(self):
        ok, _, _, resume, popen = self.open(self.session())
        self.assertFalse(ok)
        resume.assert_not_called()
        popen.assert_not_called()

    def test_a_closed_session_resumes(self):
        ok, _, focus, resume, _ = self.open(self.session(live=False, pid=None))
        self.assertTrue(ok)
        focus.assert_not_called()
        resume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
