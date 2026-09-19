"""What Nizam needs from an agentic CLI, in terms that name no tool."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

NEEDS = "needs"            # blocked on the user; carries a label and the text to show
WORKING = "working"        # processing again; any need is answered
TURN_DONE = "turn_done"    # finished its turn and is waiting for the next prompt
ENDED = "ended"            # the session exited
ANSWERED = "answered"      # the need was answered, nothing else is known
ACTIVITY = "activity"      # a sign of life that changes nothing else


@dataclass
class Signal:
    kind: str
    label: str = ""
    text: str = ""


@dataclass
class Runtime:
    pid: int
    session_id: str
    cwd: str
    status: str            # busy | idle | exited
    name: str | None
    updated_at: float
    started_at: float

    @property
    def alive(self) -> bool:
        return self.status != "exited"


@dataclass
class Transcript:
    session_id: str
    path: Path
    cwd: str
    mtime: float
    provider: str = ""
    first_ts: float | None = None
    last_ts: float | None = None
    last_role: str | None = None
    last_user_prompt: str = ""
    first_user_prompt: str = ""
    last_assistant_text: str = ""
    last_assistant_tools: list[str] = field(default_factory=list)
    pending_label: str | None = None   # set when the last turn ends on a tool that waits for the user
    summary: str | None = None
    custom_title: str | None = None
    entries: int = 0


@dataclass
class HeadlessResult:
    text: str
    is_error: bool = False
    cost: float | None = None
    turns: int | None = None


class Provider:
    name = ""
    cli = ""
    cli_dirs: tuple[Path, ...] = ()           # where it installs itself, beyond the login PATH
    desktop_app: str | None = None            # macOS app to raise for a session with no tty
    permission_modes: tuple[str, ...] = ()
    takes_session_id = True                   # False when the tool mints its own

    def transcripts(self, max_age_secs: float) -> list[Transcript]:
        """Top-level sessions touched within max_age_secs."""
        raise NotImplementedError

    def runtimes(self) -> dict[str, Runtime]:
        """Running processes by session id. A session with no runtime can only
        show as working or closed: needs and inbox are for live sessions."""
        return {}

    def marks_agent(self, folder: Path) -> bool:
        """True when this tool's own files make `folder` an agent."""
        return False

    def install_hooks(self) -> list[str]:
        """Wire the tool to append events to events.jsonl; returns lines to print."""
        return []

    def uninstall_hooks(self) -> list[str]:
        return []

    def install_skill(self, src: Path) -> list[str]:
        return []

    def uninstall_skill(self) -> list[str]:
        return []

    def doctor(self) -> tuple[bool, list[str]]:
        return True, []

    def signal(self, event: dict) -> Signal:
        """One line of events.jsonl, as this tool's hook wrote it."""
        return Signal(ACTIVITY)

    def start_command(self, session_id: str | None, prompt: str, permission_mode: str) -> str:
        """Shell command for a new interactive session; it carries session_id when one is given."""
        raise NotImplementedError

    def resume_command(self, session_id: str) -> str:
        raise NotImplementedError

    def desktop_url(self, session_id: str) -> str | None:
        """Deep link that opens this session in desktop_app, if the app holds it."""
        return None

    def clean_env(self, env: dict[str, str]) -> dict[str, str]:
        """env without the markers a session of this tool leaves for its children."""
        return env

    def session_id(self, env: dict[str, str]) -> str | None:
        """The session this process runs inside, if any."""
        return None

    def headless_command(self, exe: str, session_id: str | None, system_prompt: str, prompt: str,
                         meta: dict) -> tuple[list[str], str]:
        """argv and stdin for one unattended run. system_prompt goes wherever the tool takes one."""
        raise NotImplementedError

    def headless_session_id(self, line: str) -> str | None:
        """The run's session id, if this line of output announces it."""
        return None

    def headless_result(self, line: str) -> HeadlessResult | None:
        """The final result, if this line of output is it."""
        return None
