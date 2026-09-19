"""Seed a fake $HOME with demo agents for the README screenshot.

    export HOME=/Users/Shared/nizam-demo    # not under /tmp: the board skips sessions launched there
    python3 scripts/demo_seed.py && python3 -m nizam serve --port 7441 &
    node scripts/demo_shot.mjs http://127.0.0.1:7441/ docs/board.png "Inbox triage"
"""
import json, os, plistlib, shutil, sys, time, uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ["HOME"])
assert "nizam-demo" in str(HOME), "refusing to seed a real home"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for sub in ("agents", ".claude", ".nizam", "Library"):
    shutil.rmtree(HOME / sub, ignore_errors=True)
now = time.time()
AG = HOME / "agents"


def agent(name, about, areas):
    root = AG / name
    root.mkdir(parents=True)
    (root / "CLAUDE.md").write_text(f"# {name}\n\n{about}\n")
    for a in areas:
        (root / a).mkdir(parents=True)
        (root / a / "INSTRUCTIONS.md").write_text(f"# {a}\n")
    return root


cos = agent("chief-of-staff", "Runs the week.", ["email", "calendar", "travel"])
fin = agent("finance", "Books, invoices, taxes.", ["invoices", "taxes"])
blog = agent("blog", "Writing and the newsletter.", ["drafts", "newsletter"])
lab = agent("homelab", "The rack in the closet.", ["backups"])

events, runtimes, done = [], [], {}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def session(cwd, title, prompt, reply, age, state, tools=None, needs=None):
    sid = str(uuid.uuid4())
    d = HOME / ".claude/projects" / str(cwd).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    last = now - age
    content = [{"type": "text", "text": reply}] + [{"type": "tool_use", "name": t} for t in tools or []]
    rows = [
        {"type": "ai-title", "aiTitle": title},
        {"type": "user", "cwd": str(cwd), "timestamp": iso(last - 600), "message": {"content": prompt}},
        {"type": "assistant", "cwd": str(cwd), "timestamp": iso(last), "message": {"content": content}},
    ]
    if state == "working":
        rows.append({"type": "user", "cwd": str(cwd), "timestamp": iso(last), "message": {"content": prompt}})
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    os.utime(f, (last, last))
    if state in ("needs", "working", "inbox"):
        runtimes.append({"pid": 1, "sessionId": sid, "cwd": str(cwd),
                         "status": "busy" if state == "working" else "idle",
                         "statusUpdatedAt": int(last * 1000), "startedAt": int((last - 600) * 1000)})
    if needs:
        events.append({"session_id": sid, "ts": last, "cwd": str(cwd), **needs})
    if state == "done":
        done[sid] = {"at": last + 60, "by": "user"}
    return sid


session(cos / "travel", "Book Lisbon trip for the offsite",
        "Find flights and a hotel near the venue for Oct 12-15",
        "I found a direct flight and two hotels within walking distance. Ready to hold the fare.",
        140, "needs", needs={"hook_event_name": "Notification", "notification_type": "permission_prompt",
                             "message": "Claude needs your permission to use Bash"})
session(fin / "taxes", "Q3 estimated tax payment",
        "Work out the Q3 estimate from the ledger",
        "The estimate depends on how we treat the equipment purchase.",
        900, "needs", needs={"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                             "question": "Depreciate the new laptop over 3 years, or expense it fully this quarter?"})
session(blog / "drafts", "Draft: what I learned running agents for a year",
        "Turn my notes into a first draft, about 1200 words", "Outlining the three sections now.", 15, "working")
session(lab / "backups", "Verify last night's restic snapshots",
        "Check every repo restored cleanly", "Checking the media repo.", 40, "working")
session(cos / "email", "Inbox triage: Thursday",
        "Triage my inbox and draft replies",
        "Triaged 34 emails: 5 need your reply, and I drafted all five. 12 were archived as newsletters.\n\n"
        "Want me to send the two scheduling replies now?", 1500, "inbox")
session(fin / "invoices", "September invoices",
        "Send September invoices to the three retainer clients",
        "All three invoices are generated and saved to invoices/2026-09. Two are sent; the third is waiting on the "
        "client's new billing address.", 5400, "inbox")
session(blog / "newsletter", "Newsletter #42 subject lines",
        "Give me subject line options for issue 42",
        "Here are six options, ranked by how well similar ones performed in past issues.", 9000, "inbox")
session(cos / "calendar", "Reschedule the board prep",
        "Move board prep so it doesn't clash with the dentist",
        "Moved board prep to Tuesday 10:00 and told the attendees.", 20000, "closed")
session(lab, "Upgrade the NAS to the new kernel",
        "Plan the kernel upgrade", "Upgrade done, all pools healthy after reboot.", 50000, "done")
session(fin, "Reconcile August bank statement",
        "Reconcile August", "August reconciles to the cent. Two transactions were re-categorised.", 90000, "done")

rt_dir = HOME / ".claude/sessions"
rt_dir.mkdir(parents=True)
for i, r in enumerate(runtimes):
    (rt_dir / f"{1000 + i}.json").write_text(json.dumps(r))

nz = HOME / ".nizam"
nz.mkdir()
(nz / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
(nz / "state.json").write_text(json.dumps({"done": done, "names": {}, "prefs": {}}))

today = date.today()
fu = lambda days, title, body: f"## {today + timedelta(days=days)} — {title}\n\n{body}\n\n"
(cos / "FOLLOWUPS.md").write_text("# Follow-ups\n\n"
    + fu(-1, "[travel] Confirm visa requirements for Lisbon", "Check whether the team's passports need anything.")
    + fu(0, "[email] Chase the venue for the signed contract", "They promised it by today.")
    + fu(4, "[calendar] Send the offsite agenda", "Draft is in calendar/offsite-agenda.md."))
(fin / "FOLLOWUPS.md").write_text("# Follow-ups\n\n"
    + fu(0, "[invoices] Re-send invoice #118 once the address arrives", "Waiting on the client's new billing address.")
    + fu(9, "[taxes] Pay the Q3 estimate", "Due on the 15th."))


def routine(folder, name, schedule, body, command=None):
    d = folder / "routines"
    d.mkdir(exist_ok=True)
    head = f"---\nschedule: {schedule}\n" + (f"command: {command}\n" if command else "") + "---\n"
    (d / f"{name}.md").write_text(head + body + "\n")
    return d / f"{name}.md"


files = [
    routine(cos / "email", "morning-triage", "sun-thu 08:30", "Triage the inbox and draft replies."),
    routine(fin / "invoices", "overdue-check", "mon 09:00", "List invoices more than 14 days overdue."),
    routine(lab / "backups", "verify-snapshots", "daily 06:00", "Verify restic snapshots.", "scripts/verify.sh"),
    routine(blog / "newsletter", "weekly-digest", "fri 16:00", "Collect this week's links into a digest draft."),
]

from nizam import routines as R
la = HOME / "Library/LaunchAgents"
la.mkdir(parents=True)
runs = []
results = [("complete", "Triaged 34 emails, drafted 5 replies.", 3 * 3600),
           ("complete", "No invoices are overdue.", 3 * 86400),
           ("errored", "restic: repository media is locked by another process", 5 * 3600),
           ("complete", "Digest draft saved with 9 links.", 6 * 86400)]
for f, (status, summary, age) in zip(files, results):
    r = R.load(f)
    with open(la / f"{r.label}.plist", "wb") as fh:
        plistlib.dump(R._plist(r), fh)
    runs.append({"run_id": uuid.uuid4().hex[:12], "routine": r.id, "status": status, "summary": summary,
                 "started": now - age, "duration": 74, "trigger": "schedule", "sessions": []})
(nz / "routines").mkdir()
(nz / "routines/runs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in runs))

rq = nz / "requests"
rq.mkdir()
(rq / "links.md").write_text(f"cos = {cos} — runs the week\nfinance = {fin} — books and invoices\ncos -> finance\n")
(rq / "requests.jsonl").write_text(json.dumps({
    "ts": now - 7200, "event": "sent", "id": uuid.uuid4().hex[:8], "from": "cos", "from_root": str(cos),
    "to": "finance", "to_root": str(fin),
    "body": "Budget for the Lisbon offsite\nFlights and hotel come to about 4,800 EUR for six people. "
            "Can you confirm which budget line this goes under?"}) + "\n")
print("seeded", HOME)
