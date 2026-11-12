"""A scripted stand-in for the Kova server, and a pseudo-terminal to drive the
real ``kova`` chat against it.

``FakeKova`` speaks the chat API the terminal chat uses, including the agent
events (``approval``, ``plan``, ``stopped``) and chat fields (``permission``,
``cwd``) newer servers add. ``permissions=False`` plays an older server that
ignores those fields. What the chat sends ends up in ``sent``, ``decisions``,
``notes`` and ``stops`` for the test to check.

Message scripts (by the text sent):
  ``run it``  plan, a run_command that needs approval, its output, the plan done
  ``rm it``   a destructive run_command that needs approval
  ``slow``    a reply that streams until stopped
  ``count``   a reply that takes about a second, then ends
  anything else: "Hello from the fake server."
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeKova:
    def __init__(self, *, permissions: bool = True):
        self.permissions = permissions
        self.chats: dict[str, dict] = {}
        self.sent: list[tuple[str, str]] = []
        self.decisions: list[dict] = []
        self.notes: list[dict] = []
        self.stops: list[str] = []
        self.models = [{"name": "local", "model": "fake-model", "base_url": "http://x/v1"},
                       {"name": "other", "model": "other-model", "base_url": "http://y/v1"}]
        self.active = "local"
        self._answers: dict[str, threading.Event] = {}
        self._stopping: dict[str, threading.Event] = {}
        self.closed = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.closed.set()
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ chats
    def _chat(self, body: dict) -> dict:
        chat_id = f"chat{len(self.chats) + 1}"
        chat = {"id": chat_id, "title": f"Chat {len(self.chats) + 1}",
                "mode": body.get("mode") or "work", "web": body.get("web", True),
                "messages": [], "message_count": 0}
        if self.permissions:
            chat["permission"] = body.get("permission") or "manual"
            chat["cwd"] = body.get("cwd") or str(Path.home())
        self.chats[chat_id] = chat
        return chat

    def _update(self, chat: dict, body: dict) -> dict:
        for field in ("title", "mode", "web") + (("permission", "cwd") if self.permissions else ()):
            if body.get(field) is not None:
                chat[field] = body[field]
        return chat

    # ------------------------------------------------------------ turns
    def _turn(self, chat_id: str, text: str, emit) -> None:
        self.sent.append((chat_id, text))
        stop = self._stopping.setdefault(chat_id, threading.Event())
        stop.clear()
        # The typed message may follow shell output the chat sends along with it.
        script = next((name for name in ("run it", "rm it", "slow", "count")
                       if re.search(rf"(^|\n){name}", text)), "")
        if script in ("run it", "rm it"):
            destructive = script == "rm it"
            command = "rm -rf build" if destructive else "git status"
            plan = [{"text": "Look at the repository", "status": "in_progress"},
                    {"text": "Report what changed", "status": "pending"}]
            emit({"type": "plan", "items": plan})
            emit({"type": "tool_call", "name": "run_command", "args": {"command": command}})
            approval = f"a{len(self._answers) + 1}"
            answered = self._answers.setdefault(approval, threading.Event())
            emit({"type": "approval", "id": approval, "tool": "run_command",
                  "args": {"command": command}, "risk": "destructive" if destructive else "normal",
                  "summary": f"Run {command} in the chat folder"})
            if not answered.wait(30):
                emit({"type": "error", "message": "approval timed out"})
                return
            decision = next(d for d in self.decisions if d["id"] == approval)
            if decision["decision"] == "deny":
                emit({"type": "tool_result", "name": "run_command", "ok": False,
                      "summary": "Declined: " + (decision.get("message") or "no reason")})
                emit({"type": "text", "text": "Okay, I won't run it."})
            else:
                output = "On branch main\nnothing to commit\nthree\nfour\nfive\nsix"
                emit({"type": "tool_result", "name": "run_command", "ok": True,
                      "summary": output})
                emit({"type": "plan", "items": [{"text": item["text"], "status": "done"}
                                                for item in plan]})
                emit({"type": "text", "text": "The tree is **clean**."})
            emit({"type": "done"})
            return
        if script in ("slow", "count"):
            for index in range(60 if script == "slow" else 6):
                if stop.wait(0.2):
                    emit({"type": "stopped"})
                    return
                emit({"type": "text", "text": f"tick{index} "})
            emit({"type": "done"})
            return
        emit({"type": "reasoning", "text": "Thinking it over."})
        for chunk in ("Hello from ", "the **fake** ", "server."):
            emit({"type": "text", "text": chunk})
        emit({"type": "done"})

    # ------------------------------------------------------------ HTTP
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, data, status=200):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                size = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(size) or b"{}") if size else {}

            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def emit(event):
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                return emit

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/health":
                    return self._json({"model": fake.active, "model_ok": True, "version": "0"})
                if path == "/v1/models":
                    return self._json({"data": [{"id": "fake-model"}]})
                if path == "/chats":
                    return self._json(list(reversed(fake.chats.values())))
                if path == "/models":
                    return self._json(fake.models)
                if path == "/memory":
                    return self._json(fake.notes)
                if path == "/events":
                    self._sse()
                    while not fake.closed.wait(0.5):
                        pass
                    return None
                match = re.fullmatch(r"/chats/([^/]+)", path)
                if match and match.group(1) in fake.chats:
                    return self._json(fake.chats[match.group(1)])
                return self._json({"detail": "not found"}, 404)

            def do_PATCH(self):  # noqa: N802
                match = re.fullmatch(r"/chats/([^/]+)", self.path)
                if not match or match.group(1) not in fake.chats:
                    return self._json({"detail": "not found"}, 404)
                return self._json(fake._update(fake.chats[match.group(1)], self._body()))

            def do_POST(self):  # noqa: N802
                path = self.path
                body = self._body()
                if path == "/chats":
                    return self._json(fake._chat(body))
                if path == "/memory":
                    fake.notes.append(body)
                    return self._json({"slug": "n", **body, "updated": "now"})
                if path == "/models/active":
                    if not any(m["name"] == body.get("name") for m in fake.models):
                        return self._json({"detail": "unknown model"}, 404)
                    fake.active = body["name"]
                    return self._json({"ok": True})
                match = re.fullmatch(r"/chats/([^/]+)/(messages|stop|approvals/([^/]+))", path)
                if not match or match.group(1) not in fake.chats:
                    return self._json({"detail": "not found"}, 404)
                chat_id = match.group(1)
                if match.group(2) == "messages":
                    emit = self._sse()
                    try:
                        fake._turn(chat_id, body.get("text", ""), emit)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return None
                if match.group(2) == "stop":
                    fake.stops.append(chat_id)
                    fake._stopping.setdefault(chat_id, threading.Event()).set()
                    return self._json({"stopped": True})
                approval = match.group(3)
                fake.decisions.append({"id": approval, **body})
                fake._answers.setdefault(approval, threading.Event()).set()
                return self._json({"ok": True})

        return Handler


def write_config(home: Path, port: int) -> None:
    """A Kova home whose server and model are the fake on ``port``."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f'active = "local"\nport = {port}\n\n[memory]\npath = "{home / "memory"}"\n\n'
        f'[[models]]\nname = "local"\nbase_url = "http://127.0.0.1:{port}/v1"\n'
        f'model = "fake-model"\ncontext = 32768\n')


class Terminal:
    """``kova`` running in a pseudo-terminal, read through a VT100 screen."""

    def __init__(self, home: Path, cwd: Path, *args: str, columns: int = 100, rows: int = 40,
                 env: dict | None = None):
        import codecs
        import fcntl
        import pty
        import struct
        import termios

        from tests.vt import VT
        self.vt = VT(columns, rows)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.raw = ""
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("NO_COLOR", "KOVA_SIMPLE", "KOVA_REDUCED_MOTION")}
        environment.update(KOVA_HOME=str(home), HOME=str(home), TERM="xterm-256color",
                           COLUMNS=str(columns), LINES=str(rows), PYTHONPATH=os.pathsep.join(
                               [str(Path(__file__).resolve().parents[1] / "src"),
                                environment.get("PYTHONPATH", "")]))
        environment.update(env or {})
        # A pty pair and Popen rather than pty.fork(): this process runs the
        # fake server's threads, and forking a threaded process is unsafe.
        self.fd, child = pty.openpty()
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.process = subprocess.Popen([sys.executable, "-m", "kova", *args], cwd=cwd,
                                        env=environment, stdin=child, stdout=child, stderr=child,
                                        start_new_session=True)
        os.close(child)

    def read(self, seconds: float = 0.1) -> None:
        ready, _, _ = select.select([self.fd], [], [], seconds)
        if ready:
            try:
                text = self.decoder.decode(os.read(self.fd, 65536))
            except OSError:
                return
            self.raw += text
            self.vt.feed(text)

    def settle(self, quiet: float = 0.3, limit: float = 2.0) -> None:
        """Read until the program has drawn its frame (nothing new for ``quiet`` seconds)."""
        deadline, seen = time.monotonic() + limit, len(self.raw)
        while time.monotonic() < deadline:
            self.read(quiet)
            if len(self.raw) == seen:
                return
            seen = len(self.raw)

    def wait(self, predicate, what: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.read()
        raise AssertionError(f"never saw {what}; screen:\n{self.vt.text()}")

    def screen(self, text: str, timeout: float = 20.0) -> None:
        self.wait(lambda: text in self.vt.text(), repr(text), timeout)

    def gone(self, text: str, timeout: float = 20.0) -> None:
        self.wait(lambda: text not in self.vt.text(), f"{text!r} go away", timeout)

    def history(self) -> str:
        return "\n".join(self.vt.history())

    def type(self, data: bytes) -> None:
        os.write(self.fd, data)

    def exit_status(self, timeout: float = 15.0) -> int | None:
        """The exit code once the program ends, or None if it is still running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read(0.05)
            if self.process.poll() is not None:
                for _ in range(5):
                    self.read(0.05)
                return self.process.returncode
        return None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        os.close(self.fd)


__all__ = ["FakeKova", "Terminal", "write_config"]
