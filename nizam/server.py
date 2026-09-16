"""Local HTTP server: JSON API + the static board UI. Binds 127.0.0.1 only."""
from __future__ import annotations

import json
import mimetypes
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

    def _session(self, sid: str) -> dict | None:
        return next((s for s in self.board.snapshot()["sessions"] if s["id"] == sid), None)

    def do_GET(self):
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
                    ok = terminal.focus(s["pid"])
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
            sid = terminal.start(cwd, prompt=str(body.get("prompt") or ""),
                                 permission_mode=str(body.get("permission_mode") or "acceptEdits"),
                                 worktree=bool(body.get("worktree")), paste_only=bool(body.get("paste")),
                                 launcher=launcher)
            b.persist.set_pref("last_start", {"cwd": cwd, "permission_mode": body.get("permission_mode"),
                                              "at": time.time()})
            if body.get("title"):
                b.persist.rename(sid, str(body["title"]))
            return self._json({"ok": True, "session_id": sid})

        if parts == ["api", "prefs"]:
            for k, v in body.items():
                if k in ("launcher", "show_done", "density", "selected", "theme"):
                    b.persist.set_pref(k, v)
            return self._json({"ok": True, "prefs": b.persist.data["prefs"]})

        return self._json({"error": "not found"}, 404)


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    ensure_dirs()
    Handler.board = Board()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    PORT_FILE.write_text(str(port))
    if open_browser:
        import subprocess
        threading.Timer(0.5, lambda: subprocess.Popen(["open", f"http://127.0.0.1:{port}/"])).start()
    print(f"nizam: http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
