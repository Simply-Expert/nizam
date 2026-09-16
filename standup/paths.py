from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CLAUDE_SESSIONS = CLAUDE_DIR / "sessions"      # runtime status files (Claude >= 2.1.158)
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"

STANDUP_DIR = HOME / ".standup"
EVENTS_FILE = STANDUP_DIR / "events.jsonl"
STATE_FILE = STANDUP_DIR / "state.json"
LOG_FILE = STANDUP_DIR / "standup.log"
PORT_FILE = STANDUP_DIR / "server.port"


def ensure_dirs() -> None:
    STANDUP_DIR.mkdir(exist_ok=True)
