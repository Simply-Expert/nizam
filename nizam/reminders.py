"""When to bring a session back: parse what the user typed, and say it back."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MAX_AHEAD = 7 * 86400
CLEAR = ("", "off", "clear", "none", "cancel")
HELP = "try 1:30pm, 14:00, +45m, in 2h, tomorrow 9am or fri 10:00"
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_REL = re.compile(r"^(?:in\s+|\+)?(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?)$")
_TIME = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?m?\.?$")


def _day(word: str, today: date) -> date | None:
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    for i, name in enumerate(_DAYS):
        if len(word) >= 3 and name.startswith(word):
            return today + timedelta(days=(i - today.weekday()) % 7)
    return None


def clock(text: str) -> tuple[int, int]:
    m = _TIME.match(text)
    if not m:
        raise ValueError(f"can't read '{text}' as a time; {HELP}")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(f"'{text}' is not a 12-hour time")
        hour = hour % 12 + (12 if half == "p" else 0)
    elif m.group(2) is None and hour < 13:
        raise ValueError(f"'{text}' could be morning or afternoon; say {hour}am, {hour}pm or {hour}:00")
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' is not a time of day")
    return hour, minute


def parse(text: str, now: float) -> float | None:
    """Epoch seconds for `text`, or None when it asks to clear the reminder."""
    t = " ".join(text.strip().lower().split())
    if t in CLEAR:
        return None
    t = t.removeprefix("at ")
    at: float
    if m := _REL.match(t):
        n = float(m.group(1))
        at = now + n * (3600 if m.group(2).startswith("h") else 60)
    elif re.match(r"^\d{4}-\d{2}-\d{2}", t):
        try:
            at = datetime.fromisoformat(t).timestamp()
        except ValueError:
            raise ValueError(f"can't read '{text}' as a date and time") from None
    else:
        today = datetime.fromtimestamp(now).date()
        head, _, rest = t.partition(" ")
        day = _day(head.rstrip(","), today)
        spec = rest.strip().removeprefix("at ") if day else t
        hour, minute = clock(spec) if spec else (9, 0)
        when = datetime.combine(day or today, datetime.min.time()).replace(hour=hour, minute=minute)
        if when.timestamp() <= now:
            if day is None:
                when += timedelta(days=1)
            elif head != "today":
                when += timedelta(days=7)
        at = when.timestamp()
    if at <= now:
        raise ValueError(f"{label(at, now)} has already passed")
    if at - now > MAX_AHEAD:
        raise ValueError("that is more than a week away; a follow-up fits better")
    return at


def _parts(at: float, now: float) -> tuple[str, str]:
    when, today = datetime.fromtimestamp(at), datetime.fromtimestamp(now).date()
    clock = when.strftime("%I:%M %p").lstrip("0")
    days = (when.date() - today).days
    day = {0: "", 1: "tomorrow", -1: "yesterday"}.get(days)
    if day is None:
        day = f"{when:%a}" if 1 < days < 7 else f"{when:%b} {when.day}"
    return day, clock


def label(at: float, now: float) -> str:
    """'1:30 PM', 'tomorrow 1:30 PM', 'Fri 10:00 AM' or 'Oct 5 9:00 AM', relative to now."""
    day, clock = _parts(at, now)
    return f"{day} {clock}".strip()


def spoken(at: float, now: float) -> str:
    """The same moment inside a sentence: 'at 1:30 PM', 'tomorrow at 1:30 PM', 'on Fri at 10:00 AM'."""
    day, clock = _parts(at, now)
    if not day:
        return f"at {clock}"
    return f"{day} at {clock}" if day in ("tomorrow", "yesterday") else f"on {day} at {clock}"
