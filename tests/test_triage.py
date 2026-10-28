import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import triage

NOW = 1_000_000.0


def session(sid, bucket="inbox", age=60.0, live=True, **extra):
    return {"id": sid, "bucket": bucket, "live": live, "last_activity": NOW - age, "agent_name": "Backend",
            "area": None, "title": sid, "label": "Turn finished", "ago": "1m", "detail": "", "preview": "", **extra}


class FakeBoard:
    def __init__(self):
        self.sessions: list[dict] = []
        self.persist = mock.Mock(data={"prefs": {"triage": True}})

    def snapshot(self):
        return {"sessions": self.sessions, "prefs": self.persist.data["prefs"]}


class FakeModel:
    """Cards say the session's id back; the ranking is whatever `order` holds."""

    def __init__(self):
        self.order: list[str] = []
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, system, schema, text, model):
        self.calls.append(model)
        if model == triage.CARD_MODEL:
            ids = [line.split()[2] for line in text.splitlines() if line.startswith("=== SESSION ")]
            return {"cards": [{"id": i, "headline": f"about {i}", "needs": f"answer {i}", "kind": "decide",
                               "effort": "quick", "stakes": "normal", "options": []} for i in ids if i not in self.fail]}
        return {"order": [{"id": i, "why": f"because {i}"} for i in self.order]}


class Triage(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self.model = FakeModel()
        provider = mock.Mock()
        provider.conversation_tail.return_value = "CLAUDE:\nDone."
        for p in (mock.patch.object(triage, "STATE_FILE", Path(tempfile.mkdtemp()) / "triage.json"),
                  mock.patch.object(triage, "_ask", self.model),
                  mock.patch.object(triage.providers, "get", return_value=provider)):
            p.start()
            self.addCleanup(p.stop)
        self.t = triage.Triage(self.board)

    def view(self):
        return self.t.view(self.board.snapshot())

    def ids(self):
        return [i["id"] for i in self.view()["items"]]

    def test_off_means_no_calls(self):
        self.board.persist.data["prefs"]["triage"] = False
        self.board.sessions = [session("a")]
        self.t.tick(NOW)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.view(), {"enabled": False})

    def test_settled_sessions_share_one_card_call_then_get_ranked(self):
        self.board.sessions = [session("a"), session("b"), session("c", age=2)]
        self.model.order = ["b", "a"]
        self.t.tick(NOW)
        self.assertEqual(self.model.calls, [triage.CARD_MODEL])   # c just stopped, so no ranking yet
        self.t.tick(NOW + triage.SETTLE_SECS)
        self.model.order = ["b", "a", "c"]
        self.t.tick(NOW + triage.SETTLE_SECS)
        self.assertEqual(self.ids(), ["b", "a", "c"])
        self.assertEqual(self.view()["items"][0]["card"]["needs"], "answer b")

    def test_the_order_holds_until_applied(self):
        self.board.sessions = [session("a"), session("b")]
        self.model.order = ["a", "b"]
        self.t.tick(NOW)
        self.board.sessions.append(session("c"))
        self.model.order = ["c", "b", "a"]
        self.t.tick(NOW)
        self.assertEqual(self.ids(), ["a", "b", "c"])
        self.assertTrue(self.view()["reorder"])
        self.t.apply()
        self.assertEqual(self.ids(), ["c", "b", "a"])
        self.assertFalse(self.view()["reorder"])

    def test_an_order_whose_sessions_all_left_gives_way_on_its_own(self):
        self.board.sessions = [session("a")]
        self.model.order = ["a"]
        self.t.tick(NOW)
        self.board.sessions = [session("b"), session("c")]
        self.model.order = ["c", "b"]
        self.t.tick(NOW)
        self.assertEqual(self.ids(), ["c", "b"])
        self.assertFalse(self.view()["reorder"])
        self.assertFalse(any(i["new"] for i in self.view()["items"]))

    def test_activity_that_changes_nothing_does_not_rerank(self):
        self.board.sessions = [session("a")]
        self.model.order = ["a"]
        self.t.tick(NOW)
        self.board.sessions = [session("a", age=30)]
        self.t.tick(NOW)
        self.assertEqual(self.model.calls, [triage.CARD_MODEL, triage.RANK_MODEL, triage.CARD_MODEL])

    def test_a_newcomer_goes_last_and_says_so(self):
        self.board.sessions = [session("a")]
        self.model.order = ["a"]
        self.t.tick(NOW)
        self.board.sessions.append(session("b", age=1))
        item = self.view()["items"][1]
        self.assertEqual((item["id"], item["new"]), ("b", True))

    def test_a_busy_session_keeps_its_card_and_returns_with_it_marked_old(self):
        self.board.sessions = [session("a")]
        self.t.tick(NOW)
        self.board.sessions = [session("a", bucket="working")]
        self.t.tick(NOW)
        self.board.sessions = [session("a", age=1)]
        item = self.view()["items"][0]
        self.assertEqual(item["card"]["headline"], "about a")
        self.assertTrue(item["stale"])

    def test_a_session_gone_from_the_board_loses_its_card(self):
        self.board.sessions = [session("a")]
        self.t.tick(NOW)
        self.board.sessions = []
        self.t.tick(NOW)
        self.assertEqual(self.t.cards, {})

    def test_a_failed_card_waits_before_it_is_asked_again(self):
        self.model.fail = {"a"}
        self.board.sessions = [session("a")]
        self.t.tick(NOW)
        self.t.tick(NOW + 5)
        self.assertEqual(self.model.calls.count(triage.CARD_MODEL), 1)
        self.t.tick(NOW + triage.RETRY_SECS + 1)
        self.assertEqual(self.model.calls.count(triage.CARD_MODEL), 2)

    def test_cards_and_order_survive_a_restart(self):
        self.board.sessions = [session("a"), session("b")]
        self.model.order = ["b", "a"]
        self.t.tick(NOW)
        again = triage.Triage(self.board)
        self.assertEqual([i["id"] for i in again.view(self.board.snapshot())["items"]], ["b", "a"])
        again.tick(NOW)
        self.assertEqual(self.model.calls, [triage.CARD_MODEL, triage.RANK_MODEL])

    def test_closed_sessions_are_not_waiting_on_you(self):
        self.board.sessions = [session("a", live=False), session("b", bucket="working")]
        self.t.tick(NOW)
        self.assertEqual(self.model.calls, [])


if __name__ == "__main__":
    unittest.main()
