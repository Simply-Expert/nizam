import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import providers, telegram as T
from nizam.providers.claude import Claude

ME, STRANGER = 111, 999


class FakeBoard:
    def __init__(self, agents, sessions=()):
        self.agents, self.sessions, self.followups, self.invalidated = agents, list(sessions), [], 0

    def snapshot(self):
        return {"agents": self.agents, "sessions": self.sessions, "routines": [], "followups": self.followups,
                "agent_order": []}

    def invalidate(self):
        self.invalidated += 1


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, wait=15, **params):
        self.calls.append((method, params))
        return {"message_id": len(self.calls)} if method == "sendMessage" else True

    def said(self, method=None):
        return [p for m, p in self.calls if method is None or m == method]

    def texts(self):
        return [p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText")]


def agent(root, name, awake=None, **extra):
    return {"root": root, "name": Path(root).name, "display_name": name, "display": root, "pinned": False,
            "awake": awake, "areas": [], **extra}


class Router(Claude):
    """Answers every plain-text message with `answer`, the way `claude -p --json-schema` would."""
    name = "router"
    cli = Path(sys.executable).name

    def __init__(self, answer):
        self.answer = answer

    def structured_command(self, exe, system_prompt, schema):
        self.prompt, self.schema = system_prompt, schema
        out = json.dumps({"type": "result", "is_error": False, "structured_output": self.answer})
        return [exe, "-c", f"import sys; sys.stdin.read(); print({out!r})"]


class Bot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.patches = [mock.patch.object(T, "CONFIG_FILE", self.tmp / "telegram.json"),
                        mock.patch.object(T, "STATE_FILE", self.tmp / "telegram-state.json"),
                        mock.patch.object(T, "NIZAM_DIR", self.tmp),
                        mock.patch.object(T, "_path", lambda: str(Path(sys.executable).parent)),
                        mock.patch.object(T, "load_links", lambda: mock.Mock(agents={})),
                        mock.patch.object(T.remote, "wake", self.fake_wake),
                        mock.patch.object(T.remote, "stop", self.fake_stop),
                        mock.patch.object(T.remote, "trim", lambda: None),
                        mock.patch.object(T.remote, "awake", lambda: self.awake)]
        for p in self.patches:
            p.start()
        self.woken, self.stopped, self.awake = [], [], {}
        T.save_config({"token": "TOKEN", "bot": "nizam_bot", "users": [ME]})
        self.board = FakeBoard([agent("/w/marketing", "Marketing"), agent("/w/finance", "Finance"),
                                agent("/w/fin-ops", "Fin Ops")])
        self.tg = FakeTelegram()
        self.bot = T.Bot(self.board, api=self.tg)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fake_wake(self, root, name, mode="default", provider=None):
        self.woken.append((str(root), name, mode))
        return {"root": str(root), "name": name, "started": time.time(),
                "session_url": "https://claude.ai/code/session_X", "page_url": "https://claude.ai/code?environment=env_Y"}

    def fake_stop(self, root):
        self.stopped.append(str(root))
        return True

    def message(self, text, user=ME, sent=None, chat_type="private"):
        self.bot.handle({"update_id": 1, "message": {"text": text, "date": int(sent or time.time()),
                                                      "from": {"id": user, "first_name": "Moh"},
                                                      "chat": {"id": user, "type": chat_type}}})

    def press(self, data, user=ME):
        self.bot.handle({"update_id": 2, "callback_query": {"id": "q", "data": data, "from": {"id": user},
                                                            "message": {"chat": {"id": user}}}})

    def test_wake_by_name_sends_the_links(self):
        self.message("/wake marketing")
        self.assertEqual(self.woken, [("/w/marketing", "Marketing", "acceptEdits")])
        edit = self.tg.said("editMessageText")[-1]
        self.assertEqual(edit["text"], "Marketing is awake. Open it in the Claude app.")
        urls = [b["url"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(urls, ["https://claude.ai/code/session_X", "https://claude.ai/code?environment=env_Y"])
        self.assertTrue(self.board.invalidated)

    def test_wake_without_a_name_offers_the_sleeping_agents(self):
        self.board.agents[0]["awake"] = time.time()
        self.message("/wake")
        buttons = [b["text"] for row in self.tg.said("sendMessage")[-1]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, ["Fin Ops", "Finance"])
        self.assertEqual(self.woken, [])
        self.press("w:" + T.key_of("/w/finance"))
        self.assertEqual(self.woken[0][:2], ("/w/finance", "Finance"))
        self.assertEqual(self.tg.said("answerCallbackQuery")[-1]["text"], "Waking Finance…")

    def test_an_ambiguous_name_asks(self):
        self.message("/wake fin")
        self.assertEqual(self.woken, [])
        self.assertIn("More than one", self.tg.texts()[-1])
        self.message("/wake fin ops")
        self.assertEqual(self.woken[0][0], "/w/fin-ops")

    def test_sleep(self):
        self.message("/sleep")
        self.assertEqual(self.tg.texts()[-1], "No agent is awake.")
        self.board.agents[1]["awake"] = time.time()
        self.message("/sleep finance")
        self.assertEqual(self.stopped, ["/w/finance"])
        self.assertEqual(self.tg.texts()[-1], "Finance is asleep.")

    def test_strangers_groups_and_their_buttons_are_ignored(self):
        self.message("/wake marketing", user=STRANGER)
        self.message("/wake marketing", chat_type="group")
        self.press("w:" + T.key_of("/w/marketing"), user=STRANGER)
        self.assertEqual(self.woken, [])
        self.assertEqual(self.tg.said("sendMessage"), [])

    def test_a_command_that_arrives_late_is_not_acted_on(self):
        self.message("/wake marketing", sent=time.time() - 3 * 3600)
        self.assertEqual(self.woken, [])
        self.assertIn("3h late", self.tg.texts()[-1])

    def test_plain_text_goes_through_the_router(self):
        router = Router({"action": "wake", "agent": T.key_of("/w/finance")})
        with mock.patch.dict(providers._REGISTRY, {"claude": router}):
            self.message("wake whoever handles invoices")
        self.assertEqual(self.woken[0][0], "/w/finance")
        self.assertIn(T.key_of("/w/finance"), router.schema["properties"]["agent"]["enum"])
        self.assertIn("Finance", router.prompt)

    def test_the_router_cannot_pick_what_is_not_offered(self):
        for answer in ({"action": "none", "agent": None}, {"action": "wake", "agent": "made-up"}, None):
            with mock.patch.dict(providers._REGISTRY, {"claude": Router(answer)}):
                self.message("write the newsletter")
        self.assertEqual(self.woken, [])
        texts = self.tg.texts()
        self.assertEqual(texts[0], T.NOT_FOR_ME)
        self.assertEqual(texts[1], "Which agent should I wake?")
        self.assertIn("couldn't work out", texts[2])

    def test_status(self):
        self.board.sessions = [
            {"agent": "/w/marketing", "agent_name": "Marketing", "bucket": "needs", "label": "Permission needed",
             "title": "Fix the banner", "live": True, "last_activity": time.time()},
            {"agent": "/w/finance", "agent_name": "Finance", "bucket": "working", "label": "Working",
             "title": "Q3", "live": True, "last_activity": time.time()}]
        self.message("/status")
        self.assertEqual(self.tg.texts()[-1],
                         "Needs you (1)\n• Marketing: Permission needed, Fix the banner\n\nWorking: 1")
        self.board.sessions = []
        self.message("/status")
        self.assertEqual(self.tg.texts()[-1], "Nothing needs you.")

    def test_followups(self):
        self.board.agents[1]["awake"] = time.time()
        self.board.followups = [
            {"agent": "/w/marketing", "area": "newsletter", "title": "Send the draft", "due": "overdue", "days": -2,
             "when": "2d late"},
            {"agent": "/w/finance", "area": None, "title": "Pay VAT", "due": "today", "days": 0, "when": "today"},
            {"agent": "/w/marketing", "area": None, "title": "Book the venue", "due": "upcoming", "days": 3,
             "when": "in 3d"},
            {"agent": "/w/finance", "area": None, "title": "Year end", "due": "upcoming", "days": 40,
             "when": "in 40d"}]
        self.message("/followups")
        sent = self.tg.said("sendMessage")[-1]
        self.assertEqual(sent["text"], "Follow-ups\n\nDue now (2)\n• Marketing: Send the draft [newsletter], 2d late"
                                       "\n• Finance: Pay VAT, today\n\nComing up (1)\n• Marketing: Book the venue, in 3d")
        self.assertEqual(sent["reply_markup"]["inline_keyboard"],
                         [[{"text": "Wake Marketing", "callback_data": "w:" + T.key_of("/w/marketing")}]])
        self.message("/followups finance")
        self.assertEqual(self.tg.texts()[-1], "Follow-ups for Finance\n\nDue now (1)\n• Pay VAT, today")
        self.message("/followups fin ops")
        self.assertEqual(self.tg.texts()[-1], "No follow-ups for Fin Ops in the next 7 days.")

    def test_pairing(self):
        T.save_config({"token": "TOKEN", "bot": "nizam_bot"})
        self.message("hello", user=STRANGER)
        self.message("hello again", user=STRANGER)
        codes = [t.rsplit(" ", 1)[-1] for t in self.tg.texts()]
        self.assertEqual(len(set(codes)), 1)
        self.assertEqual(self.woken, [])
        with mock.patch.object(T, "call", lambda *a, **k: True):
            self.assertRaises(ValueError, T.pair, "NOPE00")
            T.pair(codes[0].lower())
        self.assertEqual(T.load_config()["users"], [STRANGER])
        self.message("/wake marketing", user=STRANGER)
        self.assertEqual(self.woken[0][0], "/w/marketing")
        self.message("hello", user=ME)
        self.assertNotIn("nizam telegram pair", self.tg.texts()[-1])

    def blocked(self, sid, title):
        return {"id": sid, "agent": "/w/marketing", "agent_name": "Marketing", "bucket": "needs",
                "label": "Permission needed", "title": title, "live": True, "last_activity": time.time()}

    def test_tells_what_newly_needs_you_once(self):
        self.board.sessions = [self.blocked("s1", "Old")]
        self.bot.tell(idle=0)
        self.board.sessions.append(self.blocked("s2", "Fix the banner"))
        self.bot.tell(idle=0)
        self.assertEqual(self.tg.texts(), [])          # off by default, and no backlog when turned on
        T.configure(notify="always")
        self.bot.tell(idle=0)
        self.assertEqual(self.tg.texts(), [])
        self.board.sessions.append(self.blocked("s3", "Ship it"))
        self.bot.tell(idle=0)
        self.bot.tell(idle=0)
        self.assertEqual(self.tg.texts(), ["Marketing needs you\nPermission needed: Ship it"])
        self.assertNotIn("reply_markup", self.tg.said("sendMessage")[-1])

    def test_away_waits_until_the_mac_is_untouched(self):
        T.configure(notify="away")
        self.bot.tell(idle=0)
        self.board.sessions = [self.blocked("s1", "Fix the banner")]
        self.bot.tell(idle=60)
        self.assertEqual(self.tg.texts(), [])
        self.bot.tell(idle=6 * 60)
        self.bot.tell(idle=7 * 60)
        self.assertEqual(len(self.tg.texts()), 1)

    def test_a_routine_that_found_something_offers_to_wake_its_agent(self):
        T.configure(notify="always")
        self.bot.tell()
        routine = {"id": "r:x", "agent": "/w/finance", "name": "inbox", "status": "no_change", "failing": False,
                   "attention": {"run_id": "1", "summary": "Acme wants a refund\n\nDetails."}}
        self.board.snapshot = lambda: {"agents": self.board.agents, "sessions": [], "followups": [],
                                       "routines": [routine], "agent_order": []}
        self.bot.tell()
        sent = self.tg.said("sendMessage")[-1]
        self.assertEqual(sent["text"], "Finance needs you\ninbox: Acme wants a refund")
        self.assertEqual(sent["reply_markup"]["inline_keyboard"],
                         [[{"text": "Wake Finance", "callback_data": "w:" + T.key_of("/w/finance")}]])
        self.message("/status")
        self.assertEqual(self.tg.texts()[-1], "Needs you (1)\n• Finance: inbox, Acme wants a refund")

    def test_notify_command(self):
        self.message("/notify on")
        self.assertEqual(T.load_config()["notify"], "away")
        self.assertIn("untouched for 5 minutes", self.tg.texts()[-1])
        self.message("/notify")
        self.assertEqual(T.load_config()["notify"], "away")
        self.message("/notify off")
        self.assertEqual(T.load_config()["notify"], "off")

    def test_an_idle_agent_goes_back_to_sleep(self):
        now = time.time()
        self.awake = {"/w/marketing": {"name": "Marketing", "started": now - 5 * 3600}}
        self.board.sessions = [{"agent": "/w/marketing", "bucket": "inbox", "live": True,
                                "last_activity": now - 1 * 3600}]
        self.bot.config()
        self.bot.housekeep(now)
        self.assertEqual(self.stopped, [])
        self.bot._next_housekeep = 0
        self.bot.housekeep(now + 3 * 3600)
        self.assertEqual(self.stopped, ["/w/marketing"])
        self.assertIn("went back to sleep after 3 hours", self.tg.texts()[-1])

    def test_a_busy_agent_stays_awake(self):
        now = time.time()
        self.awake = {"/w/marketing": {"name": "Marketing", "started": now - 9 * 3600}}
        self.board.sessions = [{"agent": "/w/marketing", "bucket": "working", "live": True,
                                "last_activity": now - 8 * 3600}]
        self.bot.config()
        self.bot.housekeep(now)
        self.assertEqual(self.stopped, [])

    def test_a_new_bot_forgets_the_old_one(self):
        self.bot.config()
        self.bot.state["offset"] = 42
        with mock.patch.object(T, "call", lambda token, method, **k: {"username": "other_bot"} if method == "getMe" else True):
            T.setup("NEW")
        self.assertNotIn("users", T.load_config())
        self.bot.config()
        self.assertEqual(self.bot.state, {"bot": "other_bot"})

    def test_config_is_private(self):
        self.assertEqual(T.CONFIG_FILE.stat().st_mode & 0o777, 0o600)

    def test_match(self):
        agents = self.board.agents
        self.assertEqual([a["root"] for a in T.match(agents, "FINANCE")], ["/w/finance"])
        self.assertEqual([a["root"] for a in T.match(agents, "finops")], ["/w/fin-ops"])
        self.assertEqual([a["root"] for a in T.match(agents, "keting")], ["/w/marketing"])
        self.assertEqual(T.match(agents, ""), [])


class Structured(unittest.TestCase):
    def test_command_has_no_tools_and_saves_nothing(self):
        argv = Claude().structured_command("/bin/claude", "route it", {"type": "object"})
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), {"type": "object"})

    def test_result(self):
        c = Claude()
        self.assertEqual(c.structured_result('{"structured_output": {"action": "status"}}'), {"action": "status"})
        self.assertIsNone(c.structured_result('{"is_error": true, "structured_output": {"action": "status"}}'))
        self.assertIsNone(c.structured_result("Not logged in"))


if __name__ == "__main__":
    unittest.main()
