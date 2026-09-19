"""Local HTTP server: JSON API + the static board UI. Binds 127.0.0.1 only."""
from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import terminal
from .agents import display_path, load_agent
from .paths import PORT_FILE, ensure_dirs
from .state import Board

UI_DIR = Path(__file__).parent / "ui"
DEFAULT_PORT = 7331


class Handler(BaseHTTPRequestHandler):
    board: Board

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _local(self, post: bool = False) -> bool:
        """Refuse other sites: a web page can reach 127.0.0.1, directly or through a DNS-rebound name."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            return False
        if not post:
            return True
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            return False
        # A JSON content type forces a CORS preflight, which is never answered.
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"

    def _session(self, sid: str) -> dict | None:
        return next((s for s in self.board.snapshot()["sessions"] if s["id"] == sid), None)

    def do_GET(self):
        if not self._local():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file(UI_DIR / "index.html")
        if u.path == "/api/board":
            return self._json(self.board.snapshot())
        if u.path.startswith("/api/agents/areas"):
            from urllib.parse import parse_qs
            root = parse_qs(u.query).get("root", [""])[0]
            if not root:
                return self._json({"error": "root required"}, 400)
            return self._json(load_agent(Path(root)).to_dict())
        if u.path.startswith("/static/"):
            rel = u.path[len("/static/"):]
            target = (UI_DIR / rel).resolve()
            if UI_DIR.resolve() not in target.parents:
                return self._json({"error": "forbidden"}, 403)
            return self._file(target)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._local(post=True):
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        body = self._body()
        b = self.board
        launcher = b.persist.data["prefs"].get("launcher", "Terminal")

        if parts[:2] == ["api", "sessions"] and len(parts) == 4:
            sid, action = parts[2], parts[3]
            s = self._session(sid)
            if not s:
                return self._json({"error": "unknown session"}, 404)
            if action == "done":
                b.persist.mark_done(sid, by="user")
            elif action == "reopen":
                b.persist.reopen(sid)
            elif action == "rename":
                b.persist.rename(sid, str(body.get("name", "")))
            elif action == "open":
                ok = False
                if s["live"] and s["pid"]:
                    ok = terminal.focus(s["pid"], s["cwd"], [s.get("auto_title", ""), s["title"]])
                if not ok:
                    ok = terminal.resume(sid, s["cwd"], bool(body.get("paste")), launcher)
                b.invalidate()
                return self._json({"ok": ok})
            else:
                return self._json({"error": "unknown action"}, 400)
            b.invalidate()
            return self._json({"ok": True})

        if parts == ["api", "start"]:
            cwd = str(body.get("cwd") or "")
            if not cwd or not Path(cwd).is_dir():
                return self._json({"error": "cwd must be an existing folder"}, 400)
            if body.get("worktree") and not terminal.git_root(cwd):
                return self._json({"error": "Not a git repository, so no worktree can be created"}, 400)
            sid = terminal.start(cwd, prompt=str(body.get("prompt") or ""),
                                 permission_mode=str(body.get("permission_mode") or "acceptEdits"),
                                 worktree=bool(body.get("worktree")), paste_only=bool(body.get("paste")),
                                 launcher=launcher)
            b.persist.set_pref("last_start", {"cwd": cwd, "permission_mode": body.get("permission_mode"),
                                              "at": time.time()})
            if body.get("title"):
                b.persist.rename(sid, str(body["title"]))
            return self._json({"ok": True, "session_id": sid})

        if parts == ["api", "sessions", "done-all"]:
            want_agent = body.get("agent")
            want_area = body.get("area")
            n = 0
            for s in b.snapshot()["sessions"]:
                if s["bucket"] != "closed":
                    continue
                if want_agent and s["agent"] != want_agent:
                    continue
                if want_area and not (s["area"] and (s["area"] == want_area or s["area"].startswith(want_area + "/"))):
                    continue
                b.persist.mark_done(s["id"], by="user")
                n += 1
            b.invalidate()
            return self._json({"ok": True, "marked": n})

        if parts == ["api", "agents", "set"]:
            root = str(body.get("root") or "")
            if not root:
                return self._json({"error": "root required"}, 400)
            fields = {}
            if "name" in body:
                fields["name"] = str(body["name"]).strip()[:60]
            if "pinned" in body:
                fields["pinned"] = bool(body["pinned"])
            b.persist.set_agent(root, **fields)
            b.invalidate()
            return self._json({"ok": True})

        if parts == ["api", "agents", "order"]:
            b.persist.set_agent_order(list(body.get("roots") or []))
            b.invalidate()
            return self._json({"ok": True})

        if parts[:2] == ["api", "routines"] and len(parts) == 3:
            from . import routines
            r = next((x for x in b.snapshot()["routines"] if x["id"] == body.get("id")), None)
            if not r:
                return self._json({"error": "unknown routine"}, 404)
            if parts[2] == "run":
                if r["status"] == "running":
                    return self._json({"error": "This routine is already running"}, 409)
                routines.ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
                with open(routines.RUNNER_LOG, "ab") as log:
                    subprocess.Popen([sys.executable, "-m", "nizam", "run", r["file"]], cwd=str(routines.SRC_ROOT),
                                     env=terminal.clean_env(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
            elif parts[2] == "sync":
                msgs = routines.sync([Path(r["agent"])])
                b.invalidate()
                return self._json({"ok": not any(m.startswith("!") for m in msgs), "messages": msgs})
            elif parts[2] == "ack" and r["last"]:
                b.persist.ack_routine(r["id"][2:], r["last"]["run_id"])
            elif parts[2] == "open" and r["last"] and r["last"]["session_id"]:
                ok = terminal.resume(r["last"]["session_id"], r["cwd"], bool(body.get("paste")), launcher)
                return self._json({"ok": ok})
            else:
                return self._json({"error": "unknown action"}, 400)
            b.invalidate()
            return self._json({"ok": True})

        if parts == ["api", "prefs"]:
            for k, v in body.items():
                if k in ("launcher", "show_done", "density", "selected", "theme"):
                    b.persist.set_pref(k, v)
            return self._json({"ok": True, "prefs": b.persist.data["prefs"]})

        return self._json({"error": "not found"}, 404)


def make_server(board: Board, port: int) -> ThreadingHTTPServer:
    Handler.board = board
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    PORT_FILE.write_text(str(port))
    return httpd


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    ensure_dirs()
    httpd = make_server(Board(), port)
    if open_browser:
        threading.Timer(0.5, lambda: subprocess.Popen(["open", f"http://127.0.0.1:{port}/"])).start()
    print(f"nizam: http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
