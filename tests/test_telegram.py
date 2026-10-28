import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fixtures  # noqa: F401  (puts the repo on sys.path)
from nizam import providers, telegram as T
from nizam.providers.claude import Claude

ME, STRANGER = 111, 999


class FakeBoard:
    def __init__(self, agents, sessions=()):
        self.agents, self.sessions, self.followups, self.requests, self.invalidated = agents, list(sessions), [], [], 0
        self.persist = SimpleNamespace(data={"prefs": {}})

    def snapshot(self):
        return {"agents": self.agents, "sessions": self.sessions, "routines": [], "followups": self.followups,
                "requests": self.requests, "agent_order": []}

    def invalidate(self):
        self.invalidated += 1


class FakeTelegram:
    def __init__(self):
        self.calls, self.refuse = [], {}

    def __call__(self, token, method, wait=15, **params):
        self.calls.append((method, params))
        if method in self.refuse:
            raise self.refuse[method]
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
                        mock.patch.object(T.remote, "start_in_terminal", self.fake_start_in_terminal),
                        mock.patch.object(T.remote, "stop", self.fake_stop),
                        mock.patch.object(T.remote, "trim", lambda: None),
                        mock.patch.object(T.remote, "awake", lambda: self.awake)]
        for p in self.patches:
            p.start()
        self.woken, self.stopped, self.awake, self.in_terminal = [], [], {}, []
        T.save_config({"token": "TOKEN", "bot": "nizam_bot", "users": [ME]})
        self.board = FakeBoard([agent("/w/marketing", "Marketing"), agent("/w/finance", "Finance"),
                                agent("/w/fin-ops", "Fin Ops")])
        self.tg = FakeTelegram()
        self.bot = T.Bot(self.board, api=self.tg)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fake_wake(self, root, name, mode="default", provider=None, **task):
        self.woken.append((str(root), name, mode, *([task] if task else [])))
        if task:
            return {"root": str(root), "name": name, "started": time.time(),
                    "session_url": "https://claude.ai/code/session_T", "page_url": None}
        return {"root": str(root), "name": name, "started": time.time(),
                "session_url": "https://claude.ai/code/session_X", "page_url": "https://claude.ai/code?environment=env_Y"}

    def fake_start_in_terminal(self, root, name, mode, provider=None, **task):
        self.in_terminal.append((str(root), name, mode, task))
        return {"root": str(root), "name": name, "started": time.time(), "session_url": "https://claude.ai/code/session_G"}

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

    def test_a_tap_that_arrives_late_is_not_acted_on(self):
        self.tg.refuse["answerCallbackQuery"] = T.TelegramError(
            "Bad Request: query is too old and response timeout expired or query ID is invalid", 400)
        self.press("w:" + T.key_of("/w/marketing"))
        self.assertEqual(self.woken, [])
        self.assertIn("reached me late", self.tg.texts()[-1])

    def test_a_tap_whose_answer_fails_otherwise_still_counts(self):
        self.tg.refuse["answerCallbackQuery"] = T.TelegramError("timed out")
        self.press("w:" + T.key_of("/w/marketing"))
        self.assertEqual([w[0] for w in self.woken], ["/w/marketing"])

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

    def followup(self, fid, root, title, due, days, when, area=None):
        return {"id": "f:" + fid, "agent": root, "area": area, "cwd": root + (f"/{area}" if area else ""),
                "title": title, "body": "Details.", "date": "2026-10-20", "time": None, "due": due, "days": days,
                "when": when}

    def test_followups(self):
        self.board.followups = [
            self.followup("a1", "/w/marketing", "Send the draft", "overdue", -2, "2d late", area="newsletter"),
            self.followup("b2", "/w/finance", "Pay VAT", "today", 0, "today"),
            self.followup("c3", "/w/marketing", "Book the venue", "upcoming", 3, "in 3d"),
            self.followup("d4", "/w/finance", "Year end", "upcoming", 40, "in 40d")]
        self.message("/followups")
        sent = self.tg.said("sendMessage")[-1]
        self.assertEqual(sent["text"], "Follow-ups\n\nDue now (2)\n1. Marketing: Send the draft [newsletter], 2d late"
                                       "\n2. Finance: Pay VAT, today\n\nComing up (1)\n• Marketing: Book the venue, in 3d"
                                       "\n\nTap one to start its agent on it.")
        self.assertEqual(sent["reply_markup"]["inline_keyboard"],
                         [[{"text": "1. Send the draft", "callback_data": "f:a1"}],
                          [{"text": "2. Pay VAT", "callback_data": "f:b2"}]])
        self.message("/followups finance")
        self.assertEqual(self.tg.texts()[-1], "Follow-ups for Finance\n\nDue now (1)\n1. Pay VAT, today"
                                              "\n\nTap one to start its agent on it.")
        self.message("/followups fin ops")
        self.assertEqual(self.tg.texts()[-1], "No follow-ups for Fin Ops in the next 7 days, and no requests.")

    def test_a_followup_button_starts_its_agent_on_it(self):
        self.board.persist.data["prefs"]["launcher"] = "Claude"
        self.board.followups = [self.followup("a1", "/w/marketing", "Send the draft", "overdue", -2, "2d late",
                                              area="newsletter")]
        self.press("f:a1")
        root, name, mode, task = self.woken[0]
        self.assertEqual((root, name, mode), ("/w/marketing", "Marketing", "acceptEdits"))
        self.assertEqual((task["task"], task["title"], task["cwd"]), ("a1", "Send the draft",
                                                                      Path("/w/marketing/newsletter")))
        self.assertTrue(task["prompt"].startswith("Follow-up due 2026-10-20: Send the draft\n\nDetails."))
        self.assertEqual(self.tg.said("answerCallbackQuery")[-1]["text"], "Starting Marketing…")
        edit = self.tg.said("editMessageText")[-1]
        self.assertEqual(edit["text"], "Marketing is on “Send the draft”. Open it in the Claude app.")
        self.assertEqual(edit["reply_markup"]["inline_keyboard"],
                         [[{"text": "Open session", "url": "https://claude.ai/code/session_T"}]])
        self.board.followups = []
        self.press("f:a1")
        self.assertEqual(len(self.woken), 1)
        self.assertIn("no longer in its FOLLOWUPS.md", self.tg.texts()[-1])
        self.press("f:a1", user=STRANGER)
        self.assertEqual(len(self.woken), 1)

    def test_a_followup_opens_in_the_terminal_new_sessions_open_in(self):
        self.board.persist.data["prefs"]["launcher"] = "Ghostty"
        self.board.followups = [self.followup("a1", "/w/marketing", "Send the draft", "overdue", -2, "2d late",
                                              area="newsletter")]
        with mock.patch.object(T, "has_display", lambda: True):
            self.press("f:a1")
        self.assertEqual(self.woken, [])
        root, name, mode, task = self.in_terminal[0]
        self.assertEqual((root, name, mode, task["launcher"], task["task"], task["cwd"]),
                         ("/w/marketing", "Marketing", "acceptEdits", "Ghostty", "a1", Path("/w/marketing/newsletter")))
        edit = self.tg.said("editMessageText")[-1]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"],
                         [[{"text": "Open session", "url": "https://claude.ai/code/session_G"}]])

    def test_a_followup_runs_in_the_background_while_the_lid_is_shut(self):
        self.board.persist.data["prefs"]["launcher"] = "Ghostty"
        self.board.followups = [self.followup("a1", "/w/marketing", "Send the draft", "overdue", -2, "2d late")]
        with mock.patch.object(T, "has_display", lambda: False):
            self.press("f:a1")
        self.assertEqual(self.in_terminal, [])
        self.assertEqual(self.woken[0][3]["task"], "a1")

    def request(self, rid, root, frm, title, kind="request", status="sent"):
        return {"id": f"q:{rid}" + (":reply" if kind == "reply" else ""), "rid": rid, "kind": kind, "agent": root,
                "cwd": root, "from": frm, "title": title, "body": title, "status": status, "ago": "2h"}

    def test_requests_are_listed_with_the_followups(self):
        self.board.followups = [self.followup("a1", "/w/marketing", "Send the draft", "overdue", -2, "2d late")]
        self.board.requests = [self.request("r1", "/w/finance", "marketing", "Need the Q3 numbers"),
                               self.request("r2", "/w/marketing", "finance", "Numbers attached", kind="reply")]
        self.message("/followups")
        sent = self.tg.said("sendMessage")[-1]
        self.assertEqual(sent["text"], "Follow-ups\n\nDue now (1)\n1. Marketing: Send the draft, 2d late"
                                       "\n\nRequests (2)\n2. Finance: from marketing: Need the Q3 numbers, 2h ago"
                                       "\n3. Marketing: reply from finance: Numbers attached, 2h ago"
                                       "\n\nTap one to start its agent on it.")
        self.assertEqual(sent["reply_markup"]["inline_keyboard"],
                         [[{"text": "1. Send the draft", "callback_data": "f:a1"}],
                          [{"text": "2. Need the Q3 numbers", "callback_data": "q:r1"}],
                          [{"text": "3. Numbers attached", "callback_data": "q:r2:reply"}]])
        self.board.followups = []
        self.message("/requests finance")
        self.assertEqual(self.tg.texts()[-1], "Follow-ups for Finance\n\nRequests (1)\n1. from marketing: "
                                              "Need the Q3 numbers, 2h ago\n\nTap one to start its agent on it.")

    def test_a_request_button_starts_its_agent_in_plan_mode(self):
        self.board.persist.data["prefs"]["launcher"] = "Claude"
        self.board.requests = [self.request("r1", "/w/finance", "marketing", "Need the Q3 numbers")]
        record = {"id": "r1", "from": "marketing", "to": "finance", "body": "Need the Q3 numbers", "sent_at": 0}
        with mock.patch.object(T.requests_mod, "index", lambda: {"r1": record}), \
             mock.patch.object(T.requests_mod, "set_status") as set_status:
            self.press("q:r1")
        root, name, mode, task = self.woken[0]
        self.assertEqual((root, name, mode, task["task"]), ("/w/finance", "Finance", "plan", "r1"))
        self.assertIn("<request from=\"marketing\"", task["prompt"])
        set_status.assert_called_once_with("r1", "started", by="user")
        self.board.requests = []
        self.press("q:r1")
        self.assertEqual(len(self.woken), 1)
        self.assertIn("no longer open", self.tg.texts()[-1])

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

    def blocked(self, sid, title, url=None):
        return {"id": sid, "agent": "/w/marketing", "agent_name": "Marketing", "bucket": "needs",
                "label": "Permission needed", "title": title, "live": True, "last_activity": time.time(),
                "remote_url": url}

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

    def test_a_blocked_session_the_phone_can_reach_comes_with_its_link(self):
        T.configure(notify="always")
        self.bot.tell(idle=0)
        self.board.sessions = [self.blocked("s1", "Ship it", "https://claude.ai/code/session_A")]
        self.bot.tell(idle=0)
        self.assertEqual(self.tg.said("sendMessage")[-1]["reply_markup"]["inline_keyboard"],
                         [[{"text": "Open session", "url": "https://claude.ai/code/session_A"}]])
        self.board.sessions += [self.blocked("s2", "Fix the banner", "https://claude.ai/code/session_B"),
                                self.blocked("s3", "At the Mac only")]
        self.bot.tell(idle=0)
        self.assertEqual(self.tg.said("sendMessage")[-1]["reply_markup"]["inline_keyboard"],
                         [[{"text": "Open “Fix the banner”", "url": "https://claude.ai/code/session_B"}]])

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

    def test_a_silent_poller_is_reported_until_a_poll_comes_back(self):
        class Stop(BaseException):
            pass

        polls = []

        def api(token, method, wait=15, **params):
            if method != "getUpdates":
                return True
            polls.append(method)
            if len(polls) > 1:
                raise Stop
            return []

        pid = self.tmp / "app.pid"
        pid.write_text("1")
        os.utime(pid, (time.time() - 3600,) * 2)
        with mock.patch.object(T, "PID_FILE", pid), mock.patch.object(T, "app_running", lambda: True):
            self.assertGreater(T.stalled_for(), 3500)
            self.bot.api = api
            with self.assertRaises(Stop):
                self.bot.run()
            self.assertIsNone(T.stalled_for())
            self.assertIsNotNone(T.stalled_for(now=time.time() + T.STALLED_AFTER + 60))

    def test_config_is_private(self):
        self.assertEqual(T.CONFIG_FILE.stat().st_mode & 0o777, 0o600)

    def test_match(self):
        agents = self.board.agents
        self.assertEqual([a["root"] for a in T.match(agents, "FINANCE")], ["/w/finance"])
        self.assertEqual([a["root"] for a in T.match(agents, "finops")], ["/w/fin-ops"])
        self.assertEqual([a["root"] for a in T.match(agents, "keting")], ["/w/marketing"])
        self.assertEqual(T.match(agents, ""), [])


class Dial(unittest.TestCase):
    def test_ipv4_goes_first_and_a_dead_address_costs_only_the_connect_wait(self):
        import socket
        tried = []

        class Sock:
            def __init__(self, family, kind, proto):
                self.family, self.timeouts = family, []

            def settimeout(self, t):
                self.timeouts.append(t)

            def connect(self, addr):
                tried.append(addr[0])
                if addr[0] == "1.2.3.4":
                    raise TimeoutError

            def close(self):
                pass

        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001::9", 443, 0, 0)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("5.6.7.8", 443))]
        with mock.patch.object(T.socket, "getaddrinfo", lambda *a, **k: infos), \
             mock.patch.object(T.socket, "socket", Sock):
            sock = T._dial(("api.telegram.org", 443), 65)
        self.assertEqual(tried, ["1.2.3.4", "5.6.7.8"])
        self.assertEqual(sock.timeouts, [T.CONNECT_SECS, 65])


class Wake(unittest.TestCase):
    def test_waking_drops_connections_and_cuts_the_wait_short(self):
        idle, polling = mock.Mock(), mock.Mock()
        with mock.patch.object(T, "_idle", [idle]), mock.patch.object(T, "_polls", {polling}):
            T.woke()
            self.assertEqual(T._idle, [])
        idle.close.assert_called_once()
        polling.sock.shutdown.assert_called_once_with(T.socket.SHUT_RDWR)
        start = time.time()
        self.assertTrue(T.Bot.pause(30))
        self.assertLess(time.time() - start, 1)
        self.assertFalse(T.Bot.pause(0))


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
