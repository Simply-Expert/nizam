"""Hook entrypoint. Appends one JSON line per event to
~/.nizam/events.jsonl and never blocks or prints (exit 0 always).

`nizam install` wires it into each tool, with the provider's name as its one argument.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time

KEEP = ("session_id", "cwd", "transcript_path", "hook_event_name", "permission_mode",
        "tool_name", "tool_use_id", "notification_type", "message", "agent_id",
        "agent_type", "stop_hook_active", "source", "reason", "matcher")
TEXT_FIELDS = ("prompt", "last_assistant_message")
MAX_TEXT = 600


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if data.get("agent_id"):          # sub-agent events don't change the parent's state
            return 0
        ev = {k: data[k] for k in KEEP if k in data}
        for k in TEXT_FIELDS:
            v = data.get(k)
            if isinstance(v, str):
                ev[k] = v[-MAX_TEXT:]
        ti = data.get("tool_input")
        if isinstance(ti, dict) and ev.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
            qs = ti.get("questions")
            if isinstance(qs, list) and qs and isinstance(qs[0], dict):
                ev["question"] = str(qs[0].get("question", ""))[:MAX_TEXT]
        if len(sys.argv) > 1:
            ev["provider"] = sys.argv[1]
        ev["ts"] = time.time()
        path = os.path.join(os.path.expanduser("~"), ".nizam", "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            try:                          # this file is trimmed in place while hooks run
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
