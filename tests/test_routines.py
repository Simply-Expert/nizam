import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nizam import routines as R


class Schedule(unittest.TestCase):
    def test_manual(self):
        self.assertEqual(R.parse_schedule("manual"), [])
        self.assertEqual(R.parse_schedule(""), [])

    def test_daily_many_times(self):
        self.assertEqual(R.parse_schedule("daily 09:30, 16:00"),
                         [{"Hour": 9, "Minute": 30}, {"Hour": 16, "Minute": 0}])

    def test_wrapping_range_and_clauses(self):
        got = R.parse_schedule("sat-thu 09:30, 22:00; fri 09:30")
        self.assertEqual(sorted({iv["Weekday"] for iv in got}), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual([iv for iv in got if iv["Weekday"] == 5], [{"Weekday": 5, "Hour": 9, "Minute": 30}])
        self.assertEqual(len(got), 13)

    def test_day_list(self):
        self.assertEqual(R.parse_schedule("Mon, Wed 8:05"),
                         [{"Weekday": 1, "Hour": 8, "Minute": 5}, {"Weekday": 3, "Hour": 8, "Minute": 5}])

    def test_monthly_and_hourly(self):
        self.assertEqual(R.parse_schedule("monthly 1, 15 09:00"),
                         [{"Day": 1, "Hour": 9, "Minute": 0}, {"Day": 15, "Hour": 9, "Minute": 0}])
        self.assertEqual(R.parse_schedule("hourly :15"), [{"Minute": 15}])

    def test_hourly_window(self):
        got = R.parse_schedule("hourly :15 08-22")
        self.assertEqual(len(got), 15)
        self.assertEqual((got[0], got[-1]), ({"Hour": 8, "Minute": 15}, {"Hour": 22, "Minute": 15}))
        self.assertEqual([iv["Hour"] for iv in R.parse_schedule("hourly :00 22-01")], [22, 23, 0, 1])
        now = datetime(2026, 9, 18, 23, 0)
        self.assertEqual(R.next_run(R.parse_schedule("hourly :15 08-22"), now), datetime(2026, 9, 19, 8, 15))
        with self.assertRaises(R.ScheduleError):
            R.parse_schedule("hourly :15 08-25")

    def test_rejects(self):
        for bad in ("daily", "funday 09:00", "daily 25:00", "09:00", "monthly 40 09:00", "0 9 * * *"):
            with self.assertRaises(R.ScheduleError, msg=bad):
                R.parse_schedule(bad)

    def test_next_run(self):
        now = datetime(2026, 9, 18, 10, 0)          # a Friday
        self.assertEqual(R.next_run(R.parse_schedule("daily 09:30, 16:00"), now), datetime(2026, 9, 18, 16, 0))
        self.assertEqual(R.next_run(R.parse_schedule("sat 08:00"), now), datetime(2026, 9, 19, 8, 0))
        self.assertEqual(R.next_run(R.parse_schedule("fri 09:30"), now), datetime(2026, 9, 25, 9, 30))
        self.assertEqual(R.next_run(R.parse_schedule("monthly 1 09:00"), now), datetime(2026, 10, 1, 9, 0))
        self.assertEqual(R.next_run(R.parse_schedule("hourly :15"), now), datetime(2026, 9, 18, 10, 15))


class Outcome(unittest.TestCase):
    def test_marker(self):
        self.assertEqual(R.parse_outcome("Did it.\nOUTCOME: COMPLETE"), ("complete", "", "Did it."))
        self.assertEqual(R.parse_outcome("x\n**OUTCOME: INCOMPLETE - Jira is down**")[:2], ("incomplete", "Jira is down"))
        self.assertEqual(R.parse_outcome("Shall I proceed?")[0], None)

    def test_status(self):
        self.assertEqual(R.decide_status(True, -15, False, None), "timed_out")
        self.assertEqual(R.decide_status(False, 1, False, "complete"), "errored")
        self.assertEqual(R.decide_status(False, 0, True, "complete"), "errored")
        self.assertEqual(R.decide_status(False, 0, False, None), "incomplete")
        self.assertEqual(R.decide_status(False, 0, False, "complete"), "completed")


class Definition(unittest.TestCase):
    def _write(self, text, folder="routines"):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text("agent")
        (root / folder).mkdir()
        f = root / folder / "daily.md"
        f.write_text(text)
        return root, R.load(f)

    def test_load(self):
        root, r = self._write("---\nschedule: daily 09:30\ntimeout: 20m\nmodel: sonnet\n"
                              "allowed_tools: Read, Bash(git log, -n 5), mcp__slack__*\n---\nDo the thing.\n")
        self.assertIsNone(r.error)
        self.assertEqual((r.name, r.timeout, r.prompt, r.agent_root), ("daily", 1200, "Do the thing.", root.resolve()))
        self.assertEqual(R.split_list(r.meta["allowed_tools"]), ["Read", "Bash(git log, -n 5)", "mcp__slack__*"])
        self.assertTrue(r.scheduled)
        self.assertEqual(R.install_state(r, {}), "not installed")
        self.assertEqual(R.install_state(r, {str(r.path): {"intervals": [{"Minute": 30, "Hour": 9}]}}), "scheduled")
        self.assertEqual(R.install_state(r, {str(r.path): {"intervals": [{"Minute": 0, "Hour": 9}]}}), "out of date")

    def test_env_file(self):
        root, r = self._write("---\nenv_file: .env.local\n---\nx")
        self.assertIn("env_file", r.error)
        (root / ".env.local").write_text("# keys\nexport A=1\nB=\"two words\"\nC=3 # note\nbad line\n")
        self.assertEqual(R.read_env_file(root / ".env.local"), {"A": "1", "B": "two words", "C": "3"})

    def test_script_routine(self):
        _, r = self._write("---\nschedule: monthly 6 09:07\ncommand: node scripts/collect.mjs --month last\n---\n")
        self.assertEqual((r.error, r.command, r.prompt), (None, "node scripts/collect.mjs --month last", ""))
        self.assertTrue(r.scheduled)

    def test_no_header_is_manual(self):
        _, r = self._write("Just a prompt.")
        self.assertEqual((r.error, r.intervals, R.install_state(r, {})), (None, [], "manual"))

    def test_errors(self):
        self.assertIn("needs times", self._write("---\nschedule: daily\n---\nx")[1].error)
        self.assertIn("empty", self._write("---\nschedule: manual\n---\n")[1].error)
        self.assertIn("routines/", self._write("x", folder="notes")[1].error)
        _, off = self._write("---\nschedule: daily 09:00\nenabled: false\n---\nx")
        self.assertEqual(R.install_state(off, {}), "disabled")


if __name__ == "__main__":
    unittest.main()
