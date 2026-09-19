from pathlib import Path

HOME = Path.home()
NIZAM_DIR = HOME / ".nizam"
EVENTS_FILE = NIZAM_DIR / "events.jsonl"
STATE_FILE = NIZAM_DIR / "state.json"
LOG_FILE = NIZAM_DIR / "nizam.log"
PORT_FILE = NIZAM_DIR / "server.port"


def ensure_dirs() -> None:
    NIZAM_DIR.mkdir(exist_ok=True)
