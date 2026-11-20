import unittest

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import chip


def agent(root, name=None, pinned=False):
    return {"root": root, "display_name": name or root.strip("/"), "pinned": pinned}


def session(sid, root, bucket, label="", ago="2m", starred=False):
    return {"id": sid, "agent": root, "bucket": bucket, "label": label or bucket, "ago": ago,
            "title": sid, "starred": starred}


def followup(fid, root, due="upcoming", at=0.0, in_session=False):
    return {"id": fid, "agent": root, "title": fid, "due": due, "at": at, "when": "Mon", "in_session": in_session}


def routine(rid, root, failing=False, attention=None, status="failed"):
    return {"id": "r:" + rid, "agent": root, "name": rid, "failing": failing, "attention": attention, "status": status}


def snap(agents, sessions=(), followups=(), routines=(), order=()):
    return {"agents": list(agents), "sessions": list(sessions), "followups": list(followups),
            "routines": list(routines), "agent_order": list(order)}


class Agents(unittest.TestCase):
    def test_keeps_the_board_order_not_urgency(self):
        s = snap([agent("/a"), agent("/b"), agent("/c", pinned=True)],
                 [session("1", "/a", "working"), session("2", "/b", "needs")], order=["/a", "/b"])
        self.assertEqual([a["root"] for a in chip.agents(s)], ["/c", "/a", "/b"])

    def test_state_is_the_most_pressing_session(self):
        s = snap([agent("/a")], [session("1", "/a", "working"), session("2", "/a", "needs", "Permission", "4m")])
        a = chip.agents(s)[0]
        self.assertEqual((a["state"], a["status"]), ("needs", "Permission · 4m"))

    def test_a_broken_routine_counts_as_needing_you(self):
        s = snap([agent("/a")], [session("1", "/a", "working")], routines=[routine("x", "/a", failing=True, status="timed_out")])
        self.assertEqual((chip.agents(s)[0]["state"], chip.agents(s)[0]["status"]), ("needs", "Routine timed out"))

    def test_quiet_agents_drop_out_unless_pinned_or_something_is_due(self):
        s = snap([agent("/idle"), agent("/pinned", pinned=True), agent("/due")],
                 [session("1", "/idle", "closed")], [followup("f", "/due", "today")])
        got = {a["root"]: a for a in chip.agents(s)}
        self.assertNotIn("/idle", got)
        self.assertEqual((got["/pinned"]["compact"], got["/pinned"]["due"]), (True, 0))
        self.assertEqual((got["/due"]["compact"], got["/due"]["due"]), (True, 1))

    def test_a_follow_up_already_started_is_not_due(self):
        s = snap([agent("/a")], followups=[followup("f", "/a", "today", in_session=True)])
        self.assertEqual(chip.agents(s), [])

    def test_initials(self):
        self.assertEqual(chip.initials("Noory backend"), "NB")
        self.assertEqual(chip.initials("growth"), "GR")
        self.assertEqual(chip.initials("books-curator"), "BC")


class Rows(unittest.TestCase):
    def test_sessions_then_broken_routines_then_due_follow_ups_only(self):
        s = snap([agent("/a")],
                 [session("1", "/a", "needs"), session("old", "/a", "done"), session("2", "/a", "inbox", starred=True)],
                 [followup("later", "/a", "upcoming", at=1), followup("due", "/a", "overdue", at=2)],
                 [routine("r", "/a", failing=True)])
        got = chip.rows(s, "/a")
        self.assertEqual([r["id"] for r in got], ["1", "2", "r:r", "due"])
        self.assertEqual(got[1]["title"], "★ 2")
        self.assertEqual([r["icon"] for r in got], ["needs", "inbox", "routine", "followup"])

    def test_closed_sessions_drop_out_unless_starred(self):
        s = snap([agent("/a")], [session("open", "/a", "inbox"), session("gone", "/a", "closed"),
                                 session("kept", "/a", "closed", starred=True)])
        self.assertEqual([r["id"] for r in chip.rows(s, "/a")], ["open", "kept"])

    def test_a_triage_card_explains_what_a_blocked_session_needs(self):
        s = snap([agent("/a")], [session("1", "/a", "needs")])
        got = chip.rows(s, "/a", {"1": {"headline": "Refund?", "needs": "Pick refund or ask why"}})[0]
        self.assertEqual((got["title"], got["sub"]), ("Refund?", "Pick refund or ask why"))

    def test_long_lists_end_with_a_way_to_the_board(self):
        s = snap([agent("/a")], [session(str(i), "/a", "inbox") for i in range(14)])
        got = chip.rows(s, "/a")
        self.assertEqual(len(got), chip.ROWS_MAX + 1)
        self.assertEqual(got[-1]["title"], "+4 more")


if __name__ == "__main__":
    unittest.main()
