"""A second provider that names nothing of Claude Code, to keep the seam honest."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fixtures as fx
from nizam import providers, state, terminal
from nizam.providers.base import ACTIVITY, NEEDS, TURN_DONE, Provider, Runtime, Signal, Transcript

SID = "33333333-3333-4333-8333-333333333333"


class Fake(Provider):
    name = "fake"
    cli = "fakecli"

    def __init__(self, cwd: str, now: float):
        self.cwd, self.now = cwd, now

    def transcripts(self, max_age_secs):
        return [Transcript(session_id=SID, path=Path("/nowhere"), cwd=self.cwd, mtime=self.now - 10, provider="fake",
                           first_user_prompt="hello", last_role="assistant", last_assistant_text="hi", entries=2)]

    def runtimes(self):
        return {SID: Runtime(os.getpid(), SID, self.cwd, "idle", None, self.now - 10, self.now - 60)}

    def signal(self, event):
        return {"blocked": Signal(NEEDS, "Approve?", event.get("why", "")),
                "finished": Signal(TURN_DONE)}.get(event.get("what"), Signal(ACTIVITY))

    def start_command(self, session_id, prompt, permission_mode):
        return f"fakecli new {session_id}"

    def resume_command(self, session_id):
        return f"fakecli continue {session_id}"

    def clean_env(self, env):
        return {k: v for k, v in env.items() if k != "FAKECLI_SESSION"}

    def session_id(self, env):
        return env.get("FAKECLI_SESSION")


class SecondProvider(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        agent = self.home / "work" / "ops"
        agent.mkdir(parents=True)
        (agent / "AGENTS.md").write_text("ops")
        self.now = time.time()
        fake = Fake(str(agent), self.now)
        for p in (*fx.point_at(self.home), mock.patch.dict(providers._REGISTRY, {"fake": fake})):
            p.start()
            self.addCleanup(p.stop)

    def row(self, board):
        return next(s for s in board._build(self.now)["sessions"] if s["id"] == SID)

    def test_on_the_board_with_its_own_signals(self):
        board = state.Board()
        s = self.row(board)
        self.assertEqual((s["provider"], s["agent_name"], s["bucket"]), ("fake", "ops", "inbox"))
        fx.append_event(self.home, SID, "anything", self.now - 5, provider="fake", what="blocked", why="rm -rf build")
        s = self.row(board)
        self.assertEqual((s["bucket"], s["label"], s["detail"]), ("needs", "Approve?", "rm -rf build"))
        fx.append_event(self.home, SID, "anything", self.now - 2, provider="fake", what="finished")
        self.assertEqual(self.row(board)["bucket"], "inbox")

    def test_launches_with_its_own_commands(self):
        with mock.patch.object(terminal, "run_in_new_terminal") as run:
            terminal.resume(SID, "/w", provider="fake")
            sid = terminal.start("/w", provider="fake")
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [f"cd /w && fakecli continue {SID}", f"cd /w && fakecli new {sid}"])

    def test_env_and_fallback(self):
        with mock.patch.dict(os.environ, {"FAKECLI_SESSION": "f-1", "CLAUDECODE": "1"}):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertEqual(providers.current_session_id(), "f-1")
            env = providers.clean_env()
        self.assertFalse({"FAKECLI_SESSION", "CLAUDECODE"} & set(env))
        self.assertEqual(providers.get("no such tool").name, "claude")
        self.assertEqual(providers.get(None).name, "claude")


if __name__ == "__main__":
    unittest.main()
