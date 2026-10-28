"""The settings page: every setting Nizam has, read and changed in one place.

Settings live where their owners already keep them: board prefs in state.json,
the phone bot in telegram.json, start-at-login as a LaunchAgent. Settings the
menu bar app applies itself (the badge, Mac notifications, a pinned board) are
read here but changed through the app.
"""
from __future__ import annotations

from . import __version__, reminders, telegram, terminal, update
from .followups import default_hour
from .launch import app_running, login_enabled, set_login

PERMISSION_MODES = ("acceptEdits", "default", "plan", "auto")


def read(board) -> dict:
    prefs = board.persist.data["prefs"]
    cfg = telegram.load_config()
    h, m = default_hour(prefs)
    info = update.poll()
    return {
        "app": {"version": __version__, "running": app_running(), "login": login_enabled(),
                "badge": bool(prefs.get("badge", True)), "board_pinned": bool(prefs.get("board_pinned")),
                "notifications": bool(prefs.get("notifications")),
                "launcher": prefs.get("launcher", "Terminal"), "launchers": list(terminal.LAUNCHERS),
                "terminal": terminal.terminal_for(prefs),
                "triage": bool(prefs.get("triage")),
                "current": info and info.get("current"), "latest": info and info.get("latest")},
        "followups": {"hour": f"{h:02d}:{m:02d}"},
        "telegram": {"bot": cfg.get("bot") if cfg.get("token") else None, "paired": len(cfg.get("users") or []),
                     "notify": cfg.get("notify", "off"),
                     "away_minutes": cfg.get("away_minutes", telegram.DEFAULT_AWAY_MINUTES),
                     "permission_mode": cfg.get("permission_mode", telegram.DEFAULT_PERMISSION_MODE),
                     "idle_hours": cfg.get("idle_hours", telegram.DEFAULT_IDLE_HOURS),
                     "modes": list(PERMISSION_MODES)},
    }


def _number(v, lo: float, hi: float, what: str) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be between {lo:g} and {hi:g}")
    return int(n) if n == int(n) else n


def apply(board, changes: dict) -> None:
    """Change the given settings; ValueError says what was wrong, in words for the page."""
    tg: dict = {}
    for k, v in changes.items():
        if k == "launcher":
            if v not in terminal.LAUNCHERS:
                raise ValueError(f"Unknown launcher: {v}")
            # Moving to the Claude app keeps the terminal it replaces for what the app cannot open.
            board.persist.set_pref("terminal", terminal.terminal_for(board.persist.data["prefs"]))
            board.persist.set_pref("launcher", v)
            if v in terminal.TERMINALS:
                board.persist.set_pref("terminal", v)
        elif k == "terminal":
            if v not in terminal.TERMINALS:
                raise ValueError(f"Unknown terminal: {v}")
            board.persist.set_pref("terminal", v)
        elif k == "triage":
            if not isinstance(v, bool):
                raise ValueError("Check first is on or off")
            board.persist.set_pref("triage", v)
        elif k == "followup_hour":
            try:
                h, m = reminders.clock(" ".join(str(v).lower().split()).removeprefix("at "))
            except ValueError as e:
                raise ValueError(str(e)[:1].upper() + str(e)[1:]) from None
            board.persist.set_pref("followup_hour", f"{h:02d}:{m:02d}")
        elif k == "login":
            set_login(bool(v))
        elif k == "notify":
            if v not in telegram.NOTIFY_MODES:
                raise ValueError(f"Unknown notify mode: {v}")
            tg["notify"] = v
        elif k == "away_minutes":
            tg["away_minutes"] = _number(v, 1, 240, "Minutes away")
        elif k == "idle_hours":
            tg["idle_hours"] = _number(v, 0, 72, "Hours")
        elif k == "permission_mode":
            if v not in PERMISSION_MODES:
                raise ValueError(f"Unknown permission mode: {v}")
            tg["permission_mode"] = v
        else:
            raise ValueError(f"Unknown setting: {k}")
    if tg:
        # Waking from the board or a terminal uses the phone settings too; only messaging needs the bot.
        if ("notify" in tg or "away_minutes" in tg) and not telegram.load_config().get("token"):
            raise ValueError("Connect the Telegram bot first")
        telegram.configure(**tg)
    board.invalidate()
