import unittest
from datetime import datetime, timedelta

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import home

NOW = datetime(2026, 11, 9, 15, 0).timestamp()


def session(sid, bucket="inbox", age=60.0, live=True, remind_at=None, starred=False):
    return {"id": sid, "agent": "/a", "title": sid, "bucket": bucket, "live": live, "last_activity": NOW - age,
            "remind_at": remind_at, "remind_label": "", "starred": starred}


def followup(fid, due, at, in_session=False):
    return {"id": fid, "agent": "/a", "title": fid, "due": due, "at": at, "when": "", "in_session": in_session}


def routine(rid, failing=False, attention=None, next_at=None):
    return {"id": "r:" + rid, "agent": "/a", "name": rid, "failing": failing, "attention": attention,
            "next": "", "next_at": next_at}


def request(qid, kind="request", status="sent"):
    return {"id": qid, "kind": kind, "status": status}


def build(tri=None, runs=(), **snap):
    return home.build({"sessions": [], **snap}, tri or {"enabled": False}, list(runs), NOW)


class Queue(unittest.TestCase):
    def ids(self, **kw):
        return [i["id"] for i in build(**kw)["queue"]]

    def test_blocked_work_comes_first_then_routines_and_finished_turns_last(self):
        got = self.ids(sessions=[session("turn"), session("blocked", "needs")],
                       routines=[routine("found", attention={"run_id": "1"}), routine("broken", failing=True)])
        self.assertEqual(got, ["blocked", "r:broken", "r:found", "turn"])

    def test_a_starred_session_comes_first(self):
        got = self.ids(sessions=[session("blocked", "needs"), session("turn"), session("mine", starred=True)],
                       routines=[routine("broken", failing=True)])
        self.assertEqual(got, ["mine", "blocked", "r:broken", "turn"])

    def test_what_is_already_in_hand_stays_out(self):
        got = self.ids(sessions=[session("working", "working"), session("exited", "inbox", live=False)],
                       routines=[routine("fine")])
        self.assertEqual(got, [])

    def test_follow_ups_and_requests_live_on_their_own_page(self):
        got = build(followups=[followup("f:late", "overdue", NOW - 90000),
                               followup("f:soon", "upcoming", NOW + 3600)],
                    requests=[request("q:1"), request("q:note", "reply", "started")])
        self.assertEqual((got["queue"], got["upcoming"]), ([], []))

    def test_check_first_orders_the_sessions_within_their_place(self):
        tri = {"enabled": True, "items": [{"id": "old"}, {"id": "new"}]}
        sessions = [session("new", age=10), session("old", age=500), session("blocked", "needs")]
        self.assertEqual(self.ids(sessions=sessions), ["blocked", "new", "old"])
        self.assertEqual(self.ids(sessions=sessions, tri=tri), ["blocked", "old", "new"])


class Strips(unittest.TestCase):
    def recent(self, runs, *routines):
        return build(runs=runs, routines=list(routines))["recent"]

    def test_recent_counts_only_the_last_day_of_routines_still_on_the_board(self):
        r = self.recent([{"routine": "scan", "status": "no_change", "started": NOW - 3600},
                         {"routine": "scan", "status": "completed", "started": NOW - 7200},
                         {"routine": "scan", "status": "completed", "started": NOW - 2 * 86400},
                         {"routine": "deleted", "status": "completed", "started": NOW - 60}], routine("scan"))
        self.assertEqual((r["total"], r["quiet"], r["ok"], r["routines"]), (2, 1, 1, 1))
        self.assertEqual(r["notable"], [])
        self.assertEqual([(c["id"], c["runs"]) for c in r["clean"]], [("r:scan", 2)])

    def test_a_routine_is_shown_by_where_it_stands_now(self):
        runs = [{"routine": "broken", "status": "errored", "started": NOW - 60, "summary": "\nboom\nmore"},
                {"routine": "flaky", "status": "completed", "started": NOW - 100},
                {"routine": "flaky", "status": "no_change", "started": NOW - 200},
                {"routine": "flaky", "status": "timed_out", "started": NOW - 300},
                {"routine": "inbox", "status": "no_change", "started": NOW - 100},
                {"routine": "inbox", "status": "needs_attention", "started": NOW - 400},
                {"routine": "seen", "status": "needs_attention", "started": NOW - 500},
                {"routine": "busy", "status": "running", "started": NOW - 5}]
        r = self.recent(runs, routine("broken", failing=True), routine("flaky"), routine("seen"), routine("busy"),
                        routine("inbox", attention={"run_id": "1"}))
        self.assertEqual([(i["id"], i["state"], i["open"]) for i in r["notable"]],
                         [("r:broken", "failing", True), ("r:inbox", "found", True), ("r:busy", "running", False),
                          ("r:flaky", "recovered", False), ("r:seen", "handled", False)])
        by = {i["id"]: i for i in r["notable"]}
        self.assertEqual(by["r:broken"]["summary"], "boom")
        self.assertEqual((by["r:flaky"]["since"], by["r:flaky"]["runs"], by["r:flaky"]["at"]), (2, 3, NOW - 300))
        self.assertEqual(r["clean"], [])

    def test_upcoming_runs_to_the_end_of_tomorrow(self):
        tomorrow = (datetime.fromtimestamp(NOW) + timedelta(days=1)).replace(hour=23).timestamp()
        got = build(routines=[routine("digest", next_at=NOW + 600), routine("nightly", next_at=tomorrow),
                              routine("weekly", next_at=tomorrow + 2 * 3600), routine("manual")])["upcoming"]
        self.assertEqual([u["id"] for u in got], ["r:digest", "r:nightly"])

    def test_snoozed_sessions_come_up_beside_the_next_few_routine_runs(self):
        got = build(sessions=[session("parked", "later", remind_at=NOW + 150), session("turn")],
                    routines=[routine(f"hourly{n}", next_at=NOW + 60 * n) for n in range(1, 8)])
        self.assertEqual([u["id"] for u in got["upcoming"]], ["r:hourly1", "r:hourly2", "parked", "r:hourly3"])
        self.assertEqual(got["upcoming"][2]["kind"], "session")
        self.assertEqual(got["upcoming_more"], 4)

if __name__ == "__main__":
    unittest.main()
