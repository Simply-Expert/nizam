import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import agents, followups

FILE = """# Follow-ups — dated, one-off

---

## 2026-09-30 (Wed) — [email] Untimed today
Body.

## 2026-09-30 (Wed) 21:00 — Tonight
Late work.

## 2026-09-29 (Tue) 22:30 — Last night
Still going.

## 2026-09-28 (Mon) — Two days ago

## 2026-10-01 (Thu) 9pm — Tomorrow night

## 2026-10-01 (Thu) — 12 monkeys, a title that starts with a number

## 2026-10-05 (Mon) — Far off
"""


def at(*hm):
    return datetime(2026, 9, 30, *hm).timestamp()


class FollowupHours(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "CLAUDE.md").write_text("agent")
        (root / "email").mkdir()
        (root / "email" / "INSTRUCTIONS.md").write_text("area")
        (root / "FOLLOWUPS.md").write_text(FILE)
        self.agent = agents.load_agent(root)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, now, hour=followups.DEFAULT_HOUR):
        return {f["title"]: f for f in followups.for_agent(self.agent, hour, now)}

    def test_times_are_read_from_the_heading(self):
        r = self.rows(at(8, 0))
        self.assertIsNone(r["Untimed today"]["time"])
        self.assertEqual(r["Untimed today"]["area"], "email")
        self.assertEqual(r["Tonight"]["time"], "21:00")
        self.assertEqual(r["Tomorrow night"]["time"], "21:00")
        self.assertIsNone(r["12 monkeys, a title that starts with a number"]["time"])

    def test_untimed_items_come_up_at_the_default_hour(self):
        self.assertEqual(self.rows(at(9, 59))["Untimed today"]["due"], "upcoming")
        self.assertEqual(self.rows(at(9, 59))["Untimed today"]["when"], "today 10:00 AM")
        self.assertEqual(self.rows(at(10, 0))["Untimed today"]["due"], "today")
        self.assertEqual(self.rows(at(10, 0))["Untimed today"]["when"], "today")
        self.assertEqual(self.rows(at(19, 0), hour=(20, 0))["Untimed today"]["due"], "upcoming")

    def test_timed_items_come_up_at_their_own_time(self):
        self.assertEqual(self.rows(at(20, 59))["Tonight"]["due"], "upcoming")
        self.assertEqual(self.rows(at(21, 0))["Tonight"]["due"], "today")
        self.assertEqual(self.rows(at(21, 0))["Tonight"]["when"], "today 9:00 PM")

    def test_late_once_the_next_day_comes_up(self):
        r = self.rows(at(9, 0))
        self.assertEqual(r["Last night"]["due"], "today")
        self.assertEqual(r["Last night"]["when"], "yesterday 10:30 PM")
        self.assertEqual(self.rows(at(10, 0))["Last night"]["due"], "overdue")
        self.assertEqual(r["Two days ago"]["due"], "overdue")
        self.assertEqual(r["Two days ago"]["when"], "2d late")

    def test_later_items_wait(self):
        r = self.rows(at(23, 0))
        self.assertEqual(r["Tomorrow night"]["due"], "upcoming")
        self.assertEqual(r["Tomorrow night"]["when"], "tomorrow 9:00 PM")
        self.assertEqual(r["Far off"]["when"], "in 5d")

    def test_sorted_by_when_they_come_up(self):
        titles = [f["title"] for f in followups.for_agent(self.agent, followups.DEFAULT_HOUR, at(8, 0))]
        self.assertLess(titles.index("Untimed today"), titles.index("Tonight"))
        self.assertLess(titles.index("12 monkeys, a title that starts with a number"), titles.index("Tomorrow night"))

    def test_default_hour_pref(self):
        self.assertEqual(followups.default_hour({}), (10, 0))
        self.assertEqual(followups.default_hour({"followup_hour": "20:00"}), (20, 0))
        self.assertEqual(followups.default_hour({"followup_hour": "nonsense"}), (10, 0))


if __name__ == "__main__":
    unittest.main()
