from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CLAUDE_SESSIONS = CLAUDE_DIR / "sessions"      # runtime status files (Claude >= 2.1.158)
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"

NIZAM_DIR = HOME / ".nizam"
EVENTS_FILE = NIZAM_DIR / "events.jsonl"
STATE_FILE = NIZAM_DIR / "state.json"
LOG_FILE = NIZAM_DIR / "nizam.log"
PORT_FILE = NIZAM_DIR / "server.port"


def ensure_dirs() -> None:
    NIZAM_DIR.mkdir(exist_ok=True)
