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


if __name__ == "__main__":
    unittest.main()
