import os
import tempfile
import time
import unittest
from pathlib import Path

import fixtures as fx
from nizam import state

SID = "11111111-1111-4111-8111-111111111111"


class BoardCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.agent = self.home / "work" / "finance"
        self.agent.mkdir(parents=True)
        (self.agent / "CLAUDE.md").write_text("finance")
        self.cwd = str(self.agent)
        self.now = time.time()
        for p in fx.point_at(self.home):
            p.start()
            self.addCleanup(p.stop)
        self.board = state.Board()

    def chat(self, sid=SID, age=10.0, entries=None, cwd=None):
        cwd = cwd or self.cwd
        at = self.now - age
        entries = entries or [fx.user("Reconcile March", at - 5, cwd), fx.assistant("Done.", at, cwd)]
        return fx.write_transcript(self.home, sid, cwd, entries, mtime=at)

    def live(self, status="idle", age=10.0, sid=SID):
        fx.write_runtime(self.home, sid, self.cwd, os.getpid(), status, updated=self.now - age)

    def event(self, name, age=5.0, sid=SID, **fields):
        fx.append_event(self.home, sid, name, self.now - age, **fields)

    def rows(self):
        return self.board._build(self.now)["sessions"]

    def row(self, sid=SID):
        return next(s for s in self.rows() if s["id"] == sid)

    def state_of(self, sid=SID):
        s = self.row(sid)
        return s["bucket"], s["label"], s["detail"]


class FromHooks(BoardCase):
    def test_permission_prompt(self):
        self.chat(); self.live()
        self.event("UserPromptSubmit", age=8)
        self.event("Notification", notification_type="permission_prompt", message="Claude needs permission to use Bash")
        self.assertEqual(self.state_of(), ("needs", "Permission needed", "Claude needs permission to use Bash"))

    def test_question_until_answered(self):
        self.chat(); self.live("busy")
        self.event("PreToolUse", age=6, tool_name="AskUserQuestion", question="Which ledger?")
        self.assertEqual(self.state_of(), ("needs", "Asking you", "Which ledger?"))
        self.event("PostToolUse", age=3, tool_name="AskUserQuestion")
        self.assertEqual(self.state_of(), ("working", "Working", ""))

    def test_plan_to_review(self):
        self.chat(); self.live()
        self.event("PreToolUse", tool_name="ExitPlanMode")
        self.assertEqual(self.state_of(), ("needs", "Plan to review", ""))

    def test_input_needed_until_result(self):
        for kind in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            with self.subTest(kind):
                self.chat(); self.live()
                self.event("Notification", age=6, notification_type=kind, message="pick one")
                self.assertEqual(self.state_of(), ("needs", "Input needed", "pick one"))
                self.event("ElicitationResult", age=3)
                self.assertEqual(self.state_of(), ("inbox", "Turn finished", ""))

    def test_idle_prompt_is_not_a_need(self):
        self.chat(); self.live()
        self.event("Notification", notification_type="idle_prompt", message="Claude is waiting")
        self.assertEqual(self.state_of(), ("inbox", "Turn finished", ""))

    def test_stop_clears_a_need(self):
        self.chat(); self.live()
        self.event("Notification", age=6, notification_type="permission_prompt", message="m")
        self.event("Stop", age=3)
        self.assertEqual(self.state_of(), ("inbox", "Turn finished", ""))

    def test_a_need_on_a_dead_session_is_closed(self):
        self.chat()
        fx.write_runtime(self.home, SID, self.cwd, fx.dead_pid())
        self.event("Notification", notification_type="permission_prompt", message="m")
        self.assertEqual(self.state_of(), ("closed", "Closed", ""))

    def test_working_inferred_without_a_runtime_file(self):
        self.chat()
        self.event("Stop", age=9)
        self.event("UserPromptSubmit", age=4)
        self.assertEqual(self.state_of(), ("working", "Working", ""))
        self.event("Stop", age=2)
        self.assertEqual(self.state_of(), ("closed", "Closed", ""))

    def test_session_end_stops_inferred_work(self):
        self.chat()
        self.event("UserPromptSubmit", age=4)
        self.event("SessionEnd", age=2)
        self.assertEqual(self.state_of(), ("closed", "Closed", ""))

    def test_session_start_counts_as_working(self):
        self.chat()
        self.event("SessionStart", age=2, source="resume")
        self.assertEqual(self.state_of(), ("working", "Working", ""))

    def test_hooked_flag_and_last_activity(self):
        self.chat(age=100); self.live(age=100)
        self.assertFalse(self.row()["hooked"])
        self.event("Stop", age=7)
        s = self.row()
        self.assertTrue(s["hooked"])
        self.assertAlmostEqual(s["last_activity"], self.now - 7, places=3)


class WithoutHooks(BoardCase):
    def test_runtime_status(self):
        self.chat(); self.live("busy")
        self.assertEqual(self.state_of(), ("working", "Working", ""))
        self.live("idle")
        self.assertEqual(self.state_of(), ("inbox", "Turn finished", ""))

    def test_pending_tool_in_the_transcript(self):
        for tool, label in (("ExitPlanMode", "Plan to review"), ("AskUserQuestion", "Asking you")):
            with self.subTest(tool):
                at = self.now - 10
                sid = f"{len(tool):08d}" + SID[8:]
                self.chat(sid=sid, entries=[fx.user("go", at - 5, self.cwd), fx.assistant("", at, self.cwd, tools=(tool,))])
                self.live(sid=sid)
                self.assertEqual(self.state_of(sid), ("needs", label, ""))

    def test_other_tools_are_not_a_need(self):
        at = self.now - 10
        self.chat(entries=[fx.user("go", at - 5, self.cwd), fx.assistant("", at, self.cwd, tools=("Bash",))])
        self.live()
        self.assertEqual(self.state_of(), ("inbox", "Turn finished", ""))

    def test_maybe_stuck(self):
        self.chat(age=630); self.live("busy", age=630)
        self.assertEqual(self.state_of(), ("needs", "Maybe stuck", "No progress for 10m"))

    def test_fresh_prompt_with_no_signals_is_working(self):
        at = self.now - 10
        self.chat(entries=[fx.user("a", at - 9, self.cwd), fx.assistant("b", at - 5, self.cwd), fx.user("c", at, self.cwd)])
        self.assertEqual(self.state_of(), ("working", "Working", ""))

    def test_dead_process_is_closed(self):
        self.chat()
        fx.write_runtime(self.home, SID, self.cwd, fx.dead_pid(), "busy")
        s = self.row()
        self.assertEqual((s["bucket"], s["live"], s["runtime_status"]), ("closed", False, "exited"))


class Rows(BoardCase):
    def test_agent_area_and_text(self):
        area = self.agent / "taxes"
        area.mkdir()
        (area / "INSTRUCTIONS.md").write_text("rules")
        at = self.now - 10
        fx.write_transcript(self.home, SID, str(area), [
            fx.user("File the return\nwith details", at - 5, str(area)),
            fx.assistant("Filed it. Should I send the receipt?", at, str(area))], mtime=at)
        s = self.row()
        self.assertEqual((s["agent"], s["agent_name"], s["area"], s["cwd"]),
                         (self.cwd, "finance", "taxes", str(area)))
        self.assertEqual((s["title"], s["last_prompt"], s["preview"], s["question"]),
                         ("File the return", "File the return\nwith details",
                          "Filed it. Should I send the receipt?", True))

    def test_title_precedence(self):
        at = self.now - 10
        base = [fx.user("first prompt", at - 5, self.cwd), fx.assistant("ok", at, self.cwd)]
        self.chat(entries=base + [{"type": "summary", "summary": "A summary"}])
        self.assertEqual(self.row()["title"], "A summary")
        self.chat(age=9, entries=base + [{"type": "summary", "summary": "A summary"},
                                         {"type": "custom-title", "customTitle": "Named"}])
        self.assertEqual(self.row()["title"], "Named")
        self.board.persist.rename(SID, "Mine")
        s = self.row()
        self.assertEqual((s["title"], s["auto_title"]), ("Mine", "Named"))

    def test_star_is_a_toggle(self):
        self.chat()
        self.assertFalse(self.row()["starred"])
        self.board.persist.star(SID, True)
        self.assertTrue(self.row()["starred"])
        self.board.persist.star(SID, False)
        self.assertFalse(self.row()["starred"])
        self.assertNotIn(SID, self.board.persist.data["starred"])

    def test_launch_cwd_wins_over_a_later_cd(self):
        elsewhere = self.home / "work" / "legal"
        elsewhere.mkdir()
        self.chat(cwd=str(elsewhere)); self.live()
        self.assertEqual(self.row()["agent"], self.cwd)

    def test_stray_and_short_sessions_are_left_out(self):
        self.chat(sid="a" * 8 + SID[8:], cwd="/")
        self.chat(sid="b" * 8 + SID[8:], cwd=str(self.home.home()))
        self.chat(sid="c" * 8 + SID[8:], entries=[fx.user("only one entry", self.now - 5, self.cwd)])
        self.assertEqual(self.rows(), [])

    def test_order(self):
        closed, inbox = "0" * 8 + SID[8:], "1" * 8 + SID[8:]
        self.chat(sid=closed)
        self.chat(sid=inbox); self.live(sid=inbox)
        self.assertEqual([s["bucket"] for s in self.rows()], ["inbox", "closed"])


class Done(BoardCase):
    def test_done_until_new_activity(self):
        path = self.chat(age=100)
        self.board.persist.mark_done(SID, at=self.now - 50)
        self.assertEqual(self.state_of()[:2], ("done", "Done"))
        os.utime(path, (self.now - 5, self.now - 5))
        self.assertEqual(self.state_of()[0], "closed")
        self.assertNotIn(SID, self.board.persist.data["done"])

    def test_auto_done_after_two_days(self):
        self.chat(age=3 * 86400)
        s = self.row()
        self.assertEqual((s["bucket"], s["label"]), ("done", "Auto-done"))
        self.assertAlmostEqual(s["done"]["at"], self.now - 86400, places=1)

    def test_done_drops_off_after_five_days(self):
        self.chat(age=8 * 86400)
        self.assertEqual(self.rows(), [])


class Events(BoardCase):
    def test_rotation_keeps_the_window_and_the_state(self):
        self.chat(); self.live()
        self.event("Stop", age=state.EVENTS_KEEP + 60)
        self.event("Notification", age=5, notification_type="permission_prompt", message="m")
        self.assertEqual(self.state_of()[0], "needs")
        lines = (self.home / ".nizam" / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.event("Stop", age=1)
        self.assertEqual(self.state_of()[0], "inbox")

    def test_events_without_a_session_are_ignored(self):
        self.chat(); self.live()
        fx.append_event(self.home, "", "Notification", self.now, notification_type="permission_prompt")
        self.assertEqual(self.state_of()[0], "inbox")


if __name__ == "__main__":
    unittest.main()
