"""A tiny MCP server for tests.

Run as a script it speaks stdio (newline-delimited JSON-RPC). ``serve_http``
runs the same tools behind a streamable-HTTP endpoint on 127.0.0.1 with
sessions and optional SSE replies. Only the standard library is used, so the
stdio server starts quickly under ``sys.executable``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "fail", "description": "Always reports a tool error."},
    # Second page (tests pagination):
    {"name": "pid", "description": "The server's process id.", "inputSchema": {"type": "object"}},
    {"name": "env", "description": "Read FAKE_MCP_TOKEN.", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Exit at once without answering.", "inputSchema": {"type": "object"}},
    {"name": "ask.ping/x", "description": "Ping the client, then answer.", "inputSchema": {"type": "object"}},
]
PAGE = 3


def text(value: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": value}], "isError": error}


class Fake:
    """Protocol logic shared by both transports."""

    def __init__(self, ping=None):
        self.ping = ping  # callable that pings the client and returns its reply
        self.calls: list[str] = []

    def handle(self, message: dict) -> dict | None:
        if "method" not in message or "id" not in message:
            return None  # notifications and replies to our own requests
        method, ident, params = message["method"], message["id"], message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {"listChanged": True}},
                      "serverInfo": {"name": "fake-mcp", "version": "1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            start = int(params.get("cursor") or 0)
            result = {"tools": TOOLS[start:start + PAGE]}
            if start + PAGE < len(TOOLS):
                result["nextCursor"] = str(start + PAGE)
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            self.calls.append(name)
            if name == "echo":
                result = text(str(args.get("text", "")))
            elif name == "add":
                total = args.get("a", 0) + args.get("b", 0)
                result = {"content": [], "structuredContent": {"sum": total}}
            elif name == "fail":
                result = text("the fake tool failed on purpose", error=True)
            elif name == "pid":
                result = text(str(os.getpid()))
            elif name == "env":
                result = text(os.environ.get("FAKE_MCP_TOKEN", "(unset)"))
            elif name == "crash":
                os._exit(3)
            elif name == "ask.ping/x":
                reply = self.ping() if self.ping else None
                result = text("pong" if reply is not None and reply.get("result") == {} else "no pong")
            else:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}


def main() -> None:
    lines = iter(sys.stdin.readline, "")

    def write(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def ping() -> dict | None:
        write({"jsonrpc": "2.0", "id": "server-1", "method": "ping"})
        for line in lines:
            message = json.loads(line)
            if message.get("id") == "server-1":
                return message
        return None

    fake = Fake(ping)
    print("fake MCP server ready", file=sys.stderr, flush=True)
    print("this log line is not JSON", flush=True)  # tolerated noise on stdout
    for line in lines:
        reply = fake.handle(json.loads(line))
        if reply is not None:
            write(reply)


class HttpFake:
    """Streamable HTTP fake. ``sse`` answers with event streams; sessions can expire."""

    def __init__(self, sse: bool = False, require: dict[str, str] | None = None):
        self.sse = sse
        self.require = require or {}
        self.sessions: set[str] = set()
        self.initializes = 0
        self.deleted: list[str] = []
        self.fake = Fake()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _send(self, status: int, body: bytes = b"", kind: str = "application/json", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self):
                outer.deleted.append(self.headers.get("Mcp-Session-Id", ""))
                outer.sessions.discard(self.headers.get("Mcp-Session-Id", ""))
                self._send(200)

            def do_POST(self):
                for key, value in outer.require.items():
                    if self.headers.get(key) != value:
                        return self._send(401, b'{"error":"unauthorized"}')
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                headers = {}
                if message.get("method") == "initialize":
                    outer.initializes += 1
                    session = uuid.uuid4().hex
                    outer.sessions.add(session)
                    headers["Mcp-Session-Id"] = session
                elif self.headers.get("Mcp-Session-Id") not in outer.sessions:
                    return self._send(404, b'{"error":"unknown session"}')
                elif message.get("method") != "notifications/initialized" and \
                        self.headers.get("MCP-Protocol-Version") != "2025-06-18":
                    return self._send(400, b'{"error":"missing protocol version"}')
                reply = outer.fake.handle(message)
                if reply is None:
                    return self._send(202)
                if outer.sse and message.get("method") != "initialize":
                    note = {"jsonrpc": "2.0", "method": "notifications/message",
                            "params": {"level": "info", "data": "working"}}
                    body = (f"event: message\ndata: {json.dumps(note)}\n\n"
                            f"event: message\ndata: {json.dumps(reply)}\n\n").encode()
                    return self._send(200, body, "text/event-stream", headers)
                self._send(200, json.dumps(reply).encode(), headers=headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def expire(self) -> None:
        self.sessions.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    main()
