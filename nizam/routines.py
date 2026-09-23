"""Routines: recurring headless runs an agent owns.

A routine is `routines/<name>.md` at the agent root or inside an area: a
small `key: value` header and the prompt. With a `command:` in the header
it is a script routine: no Claude, and the body is only a description.
The file stays the source of truth and the agent stays its editor; launchd
is the clock, one LaunchAgent per scheduled routine.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from . import agents as agents_mod
from .paths import NIZAM_DIR

DIR = "routines"
ROUTINES_DIR = NIZAM_DIR / "routines"
RUNS_FILE = ROUTINES_DIR / "runs.jsonl"
LOGS_DIR = ROUTINES_DIR / "logs"
LOCKS_DIR = ROUTINES_DIR / "locks"
RUNNER_LOG = ROUTINES_DIR / "runner.log"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "co.nizam.routine."
SRC_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TIMEOUT = 30 * 60
FAILED = ("incomplete", "errored", "timed_out")
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_FRONT = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)\Z", re.S)
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?!\d)")
_OUTCOME = re.compile(r"OUTCOME:\s*\**\s*(COMPLETE|INCOMPLETE)\b[ \t]*[-—–:]*[ \t]*([^\n*]*)", re.I)


class ScheduleError(ValueError):
    pass



def _days(spec: str) -> list[int]:
    out: list[int] = []
    for tok in spec.replace(",", " ").split():
        a, _, b = tok.partition("-")
        try:
            lo = _DAYS[a[:3]]
            hi = _DAYS[b[:3]] if b else lo
        except KeyError:
            raise ScheduleError(f"unknown day '{tok}'")
        n = lo
        while True:
            if n not in out:
                out.append(n)
            if n == hi:
                break
            n = (n + 1) % 7
    return out


def parse_schedule(text: str) -> list[dict]:
    """`manual`, or `;`-separated clauses: `daily 09:30, 16:00`,
    `sat-thu 09:30; fri 09:30`, `mon, wed 08:00`, `monthly 1 09:00`,
    `hourly :15`, `hourly :15 08-22`. Returns launchd StartCalendarInterval dicts ([] = manual)."""
    text = (text or "").strip().lower()
    if text in ("", "manual"):
        return []
    out: list[dict] = []
    for clause in filter(None, (c.strip() for c in text.split(";"))):
        if clause.startswith("hourly"):
            mins = [int(m) for m in re.findall(r":(\d{2})", clause)] or [0]
            if any(m > 59 for m in mins):
                raise ScheduleError(f"bad minute in '{clause}'")
            window = re.search(r"(?<![\d:])(\d{1,2})\s*-\s*(\d{1,2})\b", clause)
            if not window:
                out += [{"Minute": m} for m in mins]
                continue
            lo, hi = int(window.group(1)), int(window.group(2))
            if lo > 23 or hi > 23:
                raise ScheduleError(f"bad hours in '{clause}' (use 08-22)")
            hours = [h % 24 for h in range(lo, hi + 1 if hi >= lo else hi + 25)]
            out += [{"Hour": h, "Minute": m} for h in hours for m in mins]
            continue
        times = [(int(h), int(m)) for h, m in _TIME.findall(clause)]
        if not times or any(h > 23 or m > 59 for h, m in times):
            raise ScheduleError(f"'{clause}' needs times like 09:30")
        spec = _TIME.sub(" ", clause).replace(",", " ").strip()
        if spec in ("daily", "every day"):
            keys: list[dict] = [{}]
        elif spec.startswith("monthly"):
            try:
                doms = [int(x) for x in spec[len("monthly"):].split()] or [1]
            except ValueError:
                raise ScheduleError(f"bad day of month in '{clause}'")
            if any(not 1 <= d <= 31 for d in doms):
                raise ScheduleError(f"bad day of month in '{clause}'")
            keys = [{"Day": d} for d in doms]
        elif spec:
            keys = [{"Weekday": d} for d in _days(spec)]
        else:
            raise ScheduleError(f"'{clause}' needs days: daily, mon-fri, monthly 1 …")
        out += [{**k, "Hour": h, "Minute": m} for k in keys for h, m in times]
    return out


def _next_for(iv: dict, now: datetime) -> datetime | None:
    base = now.replace(second=0, microsecond=0)
    if "Hour" not in iv:
        c = base.replace(minute=iv["Minute"])
        return c if c > now else c + timedelta(hours=1)
    for d in range(370):
        day = (base + timedelta(days=d)).date()
        if "Weekday" in iv and day.isoweekday() % 7 != iv["Weekday"]:
            continue
        if "Day" in iv and day.day != iv["Day"]:
            continue
        c = datetime.combine(day, dtime(iv["Hour"], iv["Minute"]))
        if c > now:
            return c
    return None


def next_run(intervals: list[dict], now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    found = [c for c in (_next_for(iv, now) for iv in intervals) if c]
    return min(found) if found else None


def when_label(ts: float | datetime | None) -> str:
    if not ts:
        return ""
    d = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts)
    days = (d.date() - datetime.now().date()).days
    hm = d.strftime("%H:%M")
    if days == 0:
        return hm
    if days in (1, -1):
        return ("Tomorrow " if days == 1 else "Yesterday ") + hm
    return d.strftime("%a %-d %b ") + hm



def split_list(v: str) -> list[str]:
    """Comma-separated, but not inside parentheses: `Read, Bash(git log, -n 5)`."""
    out, cur, depth = [], "", 0
    for ch in v:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines as a shell would `source` them: comments, `export `, quotes."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip()):
            v = v.strip()
            out[k.strip()] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v.split(" #")[0].strip()
    return out


def _seconds(v: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([smh]?)", v.strip().lower())
    if not m:
        raise ValueError(f"bad timeout '{v}' (use 90s, 30m or 2h)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


@dataclass
class Routine:
    path: Path
    name: str
    agent_root: Path
    area: str | None
    cwd: Path
    meta: dict
    prompt: str
    intervals: list[dict]
    timeout: int
    enabled: bool
    error: str | None = None

    @property
    def id(self) -> str:
        return hashlib.md5(str(self.path).encode()).hexdigest()[:12]

    @property
    def label(self) -> str:
        slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"
        return f"{LABEL_PREFIX}{slug(self.agent_root.name)}.{slug(self.name)}.{self.id[:6]}"

    @property
    def plist_path(self) -> Path:
        return LAUNCH_AGENTS / f"{self.label}.plist"

    @property
    def command(self) -> str:
        return self.meta.get("command", "").strip()

    @property
    def scheduled(self) -> bool:
        return bool(self.enabled and not self.error and self.intervals)


_cache: dict[Path, tuple[float, Routine]] = {}


def load(path: Path) -> Routine:
    path = Path(path).resolve()
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    cwd = path.parent.parent
    agent = agents_mod.load_agent(agents_mod.find_agent_root(cwd))
    area = agent.area_for(cwd)
    text = path.read_text(encoding="utf-8", errors="ignore")
    meta: dict = {}
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() and not k.lstrip().startswith("#"):
                meta[k.strip().lower()] = v.strip().strip("\"'")
        text = m.group(2)
    r = Routine(path=path, name=path.stem, agent_root=agent.root, area=area.rel if area else None,
                cwd=cwd, meta=meta, prompt=text.strip(), intervals=[], timeout=DEFAULT_TIMEOUT,
                enabled=meta.get("enabled", "true").lower() not in ("false", "no", "off", "0"))
    try:
        if path.parent.name != DIR:
            raise ValueError(f"a routine lives in a {DIR}/ folder")
        if not r.prompt and not r.command:
            raise ValueError("the prompt is empty (or add a command: line for a script routine)")
        r.intervals = parse_schedule(meta.get("schedule", "manual"))
        if "timeout" in meta:
            r.timeout = _seconds(meta["timeout"])
        if meta.get("env_file") and not (cwd / meta["env_file"]).is_file():
            raise ValueError(f"env_file {meta['env_file']} was not found next to the {DIR}/ folder")
    except ValueError as e:
        r.error = str(e)
    _cache[path] = (mtime, r)
    return r


def for_agent_root(agent) -> list[Routine]:
    out: list[Routine] = []
    for folder in [agent.root] + [a.path for a in agent.areas.values()]:
        d = Path(folder) / DIR
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name.startswith(("_", ".")) or f.name.upper() == "README.MD":
                continue
            try:
                out.append(load(f))
            except OSError:
                continue
    return out



def _python() -> str:
    venv = NIZAM_DIR / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else os.path.realpath(sys.executable)


def _plist(r: Routine) -> dict:
    return {
        "Label": r.label,
        "ProgramArguments": [_python(), "-m", "nizam", "run", str(r.path), "--scheduled"],
        "WorkingDirectory": str(SRC_ROOT),
        "StartCalendarInterval": r.intervals,
        "StandardOutPath": str(RUNNER_LOG),
        "StandardErrorPath": str(RUNNER_LOG),
    }


def installed() -> dict[str, dict]:
    """Routine path -> {label, file, intervals} for every LaunchAgent we wrote."""
    out: dict[str, dict] = {}
    for f in LAUNCH_AGENTS.glob(LABEL_PREFIX + "*.plist"):
        try:
            with open(f, "rb") as fh:
                d = plistlib.load(fh)
            args = d.get("ProgramArguments") or []
            target = args[args.index("run") + 1]
        except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
            continue
        out[target] = {"label": d.get("Label", f.stem), "file": f,
                       "intervals": d.get("StartCalendarInterval") or []}
    return out


def _norm(intervals: list[dict]) -> list:
    return sorted(sorted(iv.items()) for iv in intervals)


def install_state(r: Routine, inst: dict[str, dict]) -> str:
    if r.error:
        return "invalid"
    if not r.enabled:
        return "disabled"
    if not r.intervals:
        return "manual"
    have = inst.get(str(r.path))
    if not have:
        return "not installed"
    return "scheduled" if _norm(have["intervals"]) == _norm(r.intervals) else "out of date"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _remove(entry: dict) -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{entry['label']}")
    try:
        os.remove(entry["file"])
    except FileNotFoundError:
        pass


def sync(agent_roots: list[Path]) -> list[str]:
    """Make launchd match the routine files of these agents, and drop any
    LaunchAgent of ours whose routine file is gone."""
    ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    inst = installed()
    msgs: list[str] = []
    for root in agent_roots:
        for r in for_agent_root(agents_mod.load_agent(Path(root))):
            state = install_state(r, inst)
            have = inst.get(str(r.path))
            if state == "invalid":
                msgs.append(f"! {r.name}: {r.error}" + (" (its old schedule stays installed)" if have else ""))
            elif state in ("not installed", "out of date"):
                if have:
                    _remove(have)
                tmp = r.plist_path.with_suffix(".tmp")
                with open(tmp, "wb") as fh:
                    plistlib.dump(_plist(r), fh)
                os.replace(tmp, r.plist_path)
                res = _launchctl("bootstrap", f"gui/{os.getuid()}", str(r.plist_path))
                ok = res.returncode == 0
                msgs.append(f"{'✓' if ok else '!'} {r.name}: {r.meta.get('schedule')}"
                            + ("" if ok else f" — launchctl: {res.stderr.strip() or res.returncode}"))
            elif state in ("manual", "disabled") and have:
                _remove(have)
                msgs.append(f"✓ {r.name}: {state}, schedule removed")
            else:
                msgs.append(f"· {r.name}: {state}")
    for target, entry in installed().items():
        if not Path(target).is_file():
            _remove(entry)
            msgs.append(f"✓ removed the schedule of a deleted routine: {target}")
    return msgs


def remove_all() -> int:
    inst = installed()
    for entry in inst.values():
        _remove(entry)
    return len(inst)


def audit() -> list[str]:
    """Drift between installed LaunchAgents and their routine files."""
    issues: list[str] = []
    for target, entry in installed().items():
        if not Path(target).is_file():
            issues.append(f"{entry['label']}: routine file is gone ({target})")
            continue
        state = install_state(load(Path(target)), {target: entry})
        if state != "scheduled":
            issues.append(f"{entry['label']}: {state} — run `nizam routines sync` in that agent")
    return issues



def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def record(ev: dict) -> None:
    ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUNS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


_runs_cache: tuple[tuple[float, int], dict] | None = None


def runs_index() -> dict:
    """{'last': routine id -> latest real run, 'sessions': session id -> run}."""
    global _runs_cache
    try:
        st = RUNS_FILE.stat()
    except OSError:
        return {"last": {}, "sessions": {}}
    key = (st.st_mtime, st.st_size)
    if _runs_cache and _runs_cache[0] == key:
        return _runs_cache[1]
    runs: dict[str, dict] = {}
    with open(RUNS_FILE, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("run_id"):
                runs.setdefault(ev["run_id"], {}).update(ev)
    last: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    for run in runs.values():
        for sid in run.get("sessions") or []:
            sessions[sid] = run
        if run.get("status") != "skipped":
            last[run.get("routine", "")] = run      # file order is time order
    idx = {"last": last, "sessions": sessions}
    _runs_cache = (key, idx)
    return idx


def _live_status(run: dict) -> dict:
    if run.get("status") == "running" and not is_running(run):
        return {**run, "status": "errored", "summary": "The runner died before it could record a result."}
    return run


def board_rows(agent, acks: dict) -> list[dict]:
    idx = runs_index()
    inst = installed()
    rows = []
    for r in for_agent_root(agent):
        last = idx["last"].get(r.id)
        last = _live_status(last) if last else None
        status = last["status"] if last else "never"
        nxt = next_run(r.intervals) if r.scheduled else None
        rows.append({
            "id": "r:" + r.id, "agent": str(r.agent_root), "area": r.area, "name": r.name,
            "kind": "script" if r.command else "agent", "command": r.command, "about": r.prompt[:600] if r.command else "",
            "file": str(r.path), "cwd": str(r.cwd), "schedule": r.meta.get("schedule", "manual"),
            "state": install_state(r, inst), "error": r.error, "timeout": r.timeout,
            "next": when_label(nxt), "status": status,
            "failing": bool(last) and status in FAILED and acks.get(r.id) != last["run_id"],
            "last": last and {
                "run_id": last.get("run_id"), "status": status, "when": when_label(last.get("started")),
                "duration": last.get("duration"), "summary": last.get("summary", ""),
                "session_id": (last.get("sessions") or [None])[-1], "log": last.get("log"),
                "trigger": last.get("trigger"), "cost": last.get("cost"),
            },
        })
    return rows


def hidden_sessions() -> dict[str, dict]:
    """Session id -> run, for transcripts that belong to a routine run; the board shows the routine instead."""
    return runs_index()["sessions"]


def is_running(run: dict) -> bool:
    return run.get("status") == "running" and _pid_alive(int(run.get("pid") or 0))



def parse_outcome(text: str) -> tuple[str | None, str, str]:
    """(complete|incomplete|None, reason, text without the marker line)."""
    hits = list(_OUTCOME.finditer(text or ""))
    if not hits:
        return None, "", (text or "").strip()
    m = hits[-1]
    body = "\n".join(l for l in text.splitlines() if not _OUTCOME.search(l)).strip()
    return m.group(1).lower(), m.group(2).strip(), body


def decide_status(timed_out: bool, exit_code: int | None, is_error: bool, outcome: str | None) -> str:
    if timed_out:
        return "timed_out"
    if exit_code != 0 or is_error:
        return "errored"
    return "completed" if outcome == "complete" else "incomplete"
