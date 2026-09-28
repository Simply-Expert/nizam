import unittest
from datetime import datetime

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import reminders

# A Monday, 11:00 local time.
NOW = datetime(2026, 9, 28, 11, 0).timestamp()


def at(text):
    return datetime.fromtimestamp(reminders.parse(text, NOW))


class Parse(unittest.TestCase):
    def test_clock_times(self):
        self.assertEqual(at("1:30pm"), datetime(2026, 9, 28, 13, 30))
        self.assertEqual(at("1:30 PM"), datetime(2026, 9, 28, 13, 30))
        self.assertEqual(at("13:30"), datetime(2026, 9, 28, 13, 30))
        self.assertEqual(at("at 5 p.m."), datetime(2026, 9, 28, 17, 0))
        self.assertEqual(at("14"), datetime(2026, 9, 28, 14, 0))

    def test_a_passed_time_means_tomorrow(self):
        self.assertEqual(at("9am"), datetime(2026, 9, 29, 9, 0))

    def test_relative(self):
        for text, mins in (("+45m", 45), ("in 2h", 120), ("90 min", 90), ("1.5h", 90), ("in 3 hours", 180)):
            with self.subTest(text):
                self.assertEqual(reminders.parse(text, NOW), NOW + mins * 60)

    def test_days(self):
        self.assertEqual(at("tomorrow 9am"), datetime(2026, 9, 29, 9, 0))
        self.assertEqual(at("tomorrow"), datetime(2026, 9, 29, 9, 0))
        self.assertEqual(at("fri 10:00"), datetime(2026, 10, 2, 10, 0))
        self.assertEqual(at("Friday at 2pm"), datetime(2026, 10, 2, 14, 0))
        self.assertEqual(at("mon 3pm"), datetime(2026, 9, 28, 15, 0))
        self.assertEqual(at("mon 9am"), datetime(2026, 10, 5, 9, 0))
        self.assertEqual(at("2026-09-30 08:15"), datetime(2026, 9, 30, 8, 15))

    def test_clear(self):
        for text in ("off", "clear", "", "  "):
            self.assertIsNone(reminders.parse(text, NOW))

    def test_refusals(self):
        for text in ("9", "25:00", "13pm", "today 8am", "in 9 days", "soon", "2026-10-20 09:00"):
            with self.subTest(text), self.assertRaises(ValueError):
                reminders.parse(text, NOW)


class Label(unittest.TestCase):
    def test_relative_to_now(self):
        self.assertEqual(reminders.label(datetime(2026, 9, 28, 13, 30).timestamp(), NOW), "1:30 PM")
        self.assertEqual(reminders.label(datetime(2026, 9, 29, 9, 0).timestamp(), NOW), "tomorrow 9:00 AM")
        self.assertEqual(reminders.label(datetime(2026, 10, 2, 10, 0).timestamp(), NOW), "Fri 10:00 AM")
        self.assertEqual(reminders.label(datetime(2026, 9, 27, 16, 0).timestamp(), NOW), "yesterday 4:00 PM")

    def test_spoken(self):
        self.assertEqual(reminders.spoken(datetime(2026, 9, 28, 13, 30).timestamp(), NOW), "at 1:30 PM")
        self.assertEqual(reminders.spoken(datetime(2026, 9, 29, 9, 0).timestamp(), NOW), "tomorrow at 9:00 AM")
        self.assertEqual(reminders.spoken(datetime(2026, 10, 2, 10, 0).timestamp(), NOW), "on Fri at 10:00 AM")


if __name__ == "__main__":
    unittest.main()
