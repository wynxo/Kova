"""MCP client: the tools of any Model Context Protocol server become Kova tools.

Kova speaks the client side of MCP (JSON-RPC 2.0) over both standard
transports, with no SDK dependency:

* **stdio**: Kova starts ``command args…`` and exchanges newline-delimited
  JSON on its stdin and stdout. The last lines of stderr are kept for errors.
* **streamable HTTP**: Kova POSTs each message to ``url``. The server answers
  with JSON or a short SSE stream and may hand out an ``Mcp-Session-Id``,
  which Kova sends back and renews when the server forgets it.

The public API is deliberately small; plugins (#136) build on it::

    from kova import mcp

    servers = mcp.servers_from_config(config)   # or McpServer(...) entries
    manager = mcp.McpManager(config.home)
    manager.reload(servers)        # (re)connect; one bad server never raises
    tools = manager.tools()        # list[Tool], named "<server>__<tool>"
    manager.status()               # servers, states, errors and tools for UIs
    manager.close()

    tools = mcp.build_mcp_tools(servers)   # the same, in one call

``McpClient`` is one connection (``list_tools``, ``call_tool``, ``close``); it
connects lazily and restarts a crashed stdio server on the next call, at most
``MAX_RESTARTS`` times per ``RESTART_WINDOW`` seconds. A call that was running
when the server died is reported as failed, never silently repeated, because
tools can have side effects.

Every MCP tool is marked ``external``: ``permissions.needs_approval`` asks
before each call unless a permission rule allows it or the chat has full
access. A server with ``sandbox = true`` runs under the Landlock sandbox
(``kova.sandbox``) and may only write to its own folder and temporary space.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import httpx

from kova import __version__, sandbox
from kova.config import Config, McpServerEntry
from kova.tools import Tool, ToolError

#: The ``[[mcp]]`` config entry is the public server description.
McpServer = McpServerEntry

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SEPARATOR = "__"
CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_RESTARTS = 3
RESTART_WINDOW = 60.0
#: OpenAI-compatible function names: letters, digits, ``_`` and ``-``, at most 64.
MAX_TOOL_NAME = 64
MAX_DESCRIPTION = 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_PAGES = 50
_STDERR_LINES = 20


class McpError(RuntimeError):
    """Talking to an MCP server failed; the message is safe to show the user."""


class _SessionExpired(McpError):
    """An HTTP server no longer knows our session; initialize again."""


def servers_from_config(config: Config) -> list[McpServer]:
    """The ``[[mcp]]`` servers of a config, in file order."""
    return list(config.mcp)


def validate_server(server: McpServer) -> None:
    """Raise ``ValueError`` with a readable reason when an entry cannot work."""
    if not _NAME.match(server.name or "") or SEPARATOR in server.name:
        raise ValueError("MCP server names use letters, digits, '-' and '_' (at most 32, "
                         f"no {SEPARATOR!r}), for example 'github'")
    if bool(server.command) == bool(server.url):
        raise ValueError(f"MCP server {server.name!r} needs exactly one of command (stdio) or url (HTTP)")
    if server.url and not re.match(r"^https?://", server.url, re.IGNORECASE):
        raise ValueError(f"MCP server {server.name!r}: url must start with http:// or https://")
    if server.sandbox and server.url:
        raise ValueError(f"MCP server {server.name!r}: sandbox applies to stdio servers only")


def tool_name(server: str, tool: str) -> str:
    """The Kova name of a server's tool: ``server__tool``, made function-name safe."""
    return f"{server}{SEPARATOR}{_UNSAFE.sub('_', tool)}"[:MAX_TOOL_NAME]


def _connection_key(server: McpServer) -> tuple:
    """The fields that need a reconnect when they change (not on/off switches)."""
    return (server.command, tuple(server.args), tuple(sorted(server.env.items())), server.url,
            tuple(sorted(server.headers.items())), server.sandbox)


@dataclass
class _Waiter:
    event: threading.Event
    message: dict | None = None


class _Stdio:
    """One spawned server process speaking newline-delimited JSON-RPC."""

    def __init__(self, server: McpServer, workdir: Path | None, on_message: Callable[[Any, dict], None]):
        self.server = server
        self.on_message = on_message
        self.stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_LINES)
        self.waiters: dict[Any, _Waiter] = {}
        self.lock = threading.Lock()
        self.dead: str | None = None
        env = {**os.environ, **server.env}
        command = shutil.which(server.command, path=env.get("PATH")) or server.command
        argv = [command, *server.args]
        cwd = None
        if workdir is not None:
            workdir.mkdir(parents=True, exist_ok=True)
            cwd = workdir
        if server.sandbox:
            wrapped = sandbox.wrap(argv, workdir or Path.cwd())
            if wrapped is None:
                # Fail closed, like run_command: no sandbox means do not start.
                raise McpError(f"{server.name}: sandbox = true, but the Landlock sandbox is "
                               "unavailable on this system; the server was not started")
            argv = wrapped
        options: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                   if os.name == "nt" else {"start_new_session": True})
        try:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, cwd=cwd, env=env, **options)
        except OSError as exc:
            raise McpError(f"{server.name}: could not start {server.command!r}: {exc.strerror or exc}") from None
        threading.Thread(target=self._read, daemon=True, name=f"mcp-{server.name}-out").start()
        threading.Thread(target=self._drain, daemon=True, name=f"mcp-{server.name}-err").start()

    @property
    def alive(self) -> bool:
        return self.dead is None and self.process.poll() is None

    def _read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        for raw in stream:
            try:
                message = json.loads(raw)
            except ValueError:
                continue  # some servers print logs to stdout; they are not protocol
            if isinstance(message, dict):
                self._dispatch(message)
            elif isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        self._dispatch(item)
        code = self.process.wait()
        detail = f" (exit code {code})" if code is not None else ""
        tail = " ".join(line for line in self.stderr if line)[-300:]
        self.dead = f"{self.server.name} stopped{detail}" + (f": {tail}" if tail else "")
        with self.lock:
            waiters = list(self.waiters.values())
        for waiter in waiters:
            waiter.event.set()

    def _dispatch(self, message: dict) -> None:
        if "method" not in message and "id" in message:
            with self.lock:
                waiter = self.waiters.get(message["id"])
            if waiter is not None:
                waiter.message = message
                waiter.event.set()
            return
        self.on_message(self, message)

    def _drain(self) -> None:
        stream = self.process.stderr
        assert stream is not None
        for raw in stream:
            self.stderr.append(raw.decode("utf-8", "replace").strip()[:500])

    def send(self, message: dict) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        try:
            with self.lock:
                assert self.process.stdin is not None
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError):
            raise McpError(self.dead or f"{self.server.name} is not running") from None

    def request(self, message: dict, timeout: float) -> dict:
        waiter = _Waiter(threading.Event())
        with self.lock:
            self.waiters[message["id"]] = waiter
        try:
            self.send(message)
            if not waiter.event.wait(timeout):
                raise McpError(f"{self.server.name} did not answer {message['method']} within {timeout:g} s")
            if waiter.message is None:
                raise McpError(self.dead or f"{self.server.name} stopped")
            return waiter.message
        finally:
            with self.lock:
                self.waiters.pop(message["id"], None)

    def close(self) -> None:
        process = self.process
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill(process)
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        else:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _sse_messages(response: httpx.Response) -> Iterator[dict]:
    """JSON-RPC messages from the ``data:`` lines of an SSE response."""
    data: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data:
                try:
                    message = json.loads("\n".join(data))
                except ValueError:
                    message = None
                data = []
                if isinstance(message, dict):
                    yield message
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        try:
            message = json.loads("\n".join(data))
        except ValueError:
            return
        if isinstance(message, dict):
            yield message


class _Http:
    """Streamable HTTP: one POST per message, JSON or SSE in reply."""

    alive = True

    def __init__(self, server: McpServer, on_message: Callable[[Any, dict], None],
                 transport: httpx.BaseTransport | None = None):
        self.server = server
        self.on_message = on_message
        self.session: str | None = None
        self.version: str | None = None
        self.http = httpx.Client(timeout=httpx.Timeout(CALL_TIMEOUT, connect=10), transport=transport,
                                 follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        headers = {**self.server.headers, "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        return headers

    def send(self, message: dict) -> None:
        self._post(message, None)

    def request(self, message: dict, timeout: float) -> dict:
        reply = self._post(message, timeout)
        if reply is None:
            raise McpError(f"{self.server.name} returned no reply to {message['method']}")
        return reply

    def _post(self, message: dict, timeout: float | None) -> dict | None:
        try:
            with self.http.stream("POST", self.server.url, json=message, headers=self._headers(),
                                  timeout=timeout or CALL_TIMEOUT) as response:
                if response.status_code == 404 and self.session and message.get("method") != "initialize":
                    raise _SessionExpired(f"{self.server.name}: session expired")
                if response.status_code == 202 or "id" not in message or "method" not in message:
                    return None
                if response.is_error:
                    raise McpError(f"{self.server.name} answered HTTP {response.status_code}")
                if message.get("method") == "initialize":
                    self.session = response.headers.get("mcp-session-id") or None
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    for item in _sse_messages(response):
                        if item.get("id") == message["id"] and "method" not in item:
                            return item
                        self.on_message(self, item)
                    raise McpError(f"{self.server.name} closed the stream before answering")
                response.read()
                try:
                    body = response.json()
                except ValueError:
                    raise McpError(f"{self.server.name} returned a reply that is not JSON") from None
                for item in body if isinstance(body, list) else [body]:
                    if isinstance(item, dict) and item.get("id") == message["id"] and "method" not in item:
                        return item
                raise McpError(f"{self.server.name} returned no reply to {message['method']}")
        except httpx.TimeoutException:
            raise McpError(f"{self.server.name} did not answer {message.get('method')} in time") from None
        except httpx.HTTPError as exc:
            raise McpError(f"{self.server.name}: cannot reach {self.server.url} ({type(exc).__name__})") from None

    def close(self) -> None:
        if self.session:
            try:
                self.http.delete(self.server.url, headers=self._headers(), timeout=5)
            except httpx.HTTPError:
                pass
        self.http.close()


class McpClient:
    """One MCP server connection. Thread-safe; connects on first use."""

    def __init__(self, server: McpServer, workdir: Path | None = None, *,
                 http_transport: httpx.BaseTransport | None = None):
        validate_server(server)
        self.server = server
        self.workdir = workdir
        self.http_transport = http_transport
        self.info: dict = {}
        self._link: _Stdio | _Http | None = None
        self._starting: _Stdio | _Http | None = None
        self._lock = threading.RLock()
        self._ids = iter(range(1, 1 << 62))
        self._starts: collections.deque[float] = collections.deque(maxlen=MAX_RESTARTS)
        self._closed = False

    # -- connection ---------------------------------------------------------------
    def _connect(self, timeout: float) -> _Stdio | _Http:
        with self._lock:
            if self._closed:
                raise McpError(f"{self.server.name} is closed")
            if self._link is not None and self._link.alive:
                return self._link
            if self._link is not None:
                self._link.close()
                self._link = None
            now = time.monotonic()
            if len(self._starts) == MAX_RESTARTS and now - self._starts[0] < RESTART_WINDOW:
                raise McpError(f"{self.server.name} keeps stopping; not restarting it for a minute. "
                               f"Check it with: kova mcp test {self.server.name}")
            self._starts.append(now)
            if self.server.url:
                link: _Stdio | _Http = _Http(self.server, self._on_message, self.http_transport)
            else:
                link = _Stdio(self.server, self.workdir, self._on_message)
            self._starting = link
            try:
                self._initialize(link, timeout)
            except BaseException:
                link.close()
                raise
            finally:
                self._starting = None
            if self._closed:
                link.close()
                raise McpError(f"{self.server.name} is closed")
            self._link = link
            return link

    def _initialize(self, link: _Stdio | _Http, timeout: float) -> None:
        reply = link.request({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "kova", "version": __version__}}}, timeout)
        result = self._result(reply, "initialize")
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise McpError(f"{self.server.name} speaks MCP {version!r}, which Kova does not support")
        if isinstance(link, _Http):
            link.version = version
        info = result.get("serverInfo")
        self.info = {"name": str(info.get("name", "")), "version": str(info.get("version", ""))} \
            if isinstance(info, dict) else {}
        link.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _on_message(self, link: _Stdio | _Http, message: dict) -> None:
        """Server-to-client requests: answer ping, decline everything else."""
        if "id" not in message or "method" not in message:
            return  # notifications such as tools/list_changed apply on reload
        if message["method"] == "ping":
            reply: dict = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32601, "message": "Kova does not offer this to MCP servers"}}
        # Reply off the reader thread: a stdio write must never block reading.
        threading.Thread(target=self._reply, args=(link, reply), daemon=True).start()

    @staticmethod
    def _reply(link: _Stdio | _Http, reply: dict) -> None:
        try:
            link.send(reply)
        except McpError:
            pass

    def _result(self, reply: dict, method: str) -> dict:
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            raise McpError(f"{self.server.name}: {method} failed: {str(error.get('message') or 'error')[:500]}")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{self.server.name}: {method} returned no result")
        return result

    def request(self, method: str, params: dict | None = None, *, timeout: float = CALL_TIMEOUT) -> dict:
        """Send one request and return its ``result``; reconnects when needed."""
        for attempt in range(2):
            link = self._connect(min(timeout, CONNECT_TIMEOUT))
            message: dict = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
            if params is not None:
                message["params"] = params
            try:
                return self._result(link.request(message, timeout), method)
            except _SessionExpired:
                with self._lock:
                    if self._link is link:
                        self._link = None
                if isinstance(link, _Http):
                    link.session = None  # already gone on the server: no DELETE
                link.close()
                if attempt:
                    raise
        raise McpError(f"{self.server.name}: {method} failed")  # pragma: no cover

    def start(self, timeout: float = CONNECT_TIMEOUT) -> None:
        """Connect now instead of on first use."""
        self._connect(timeout)

    def list_tools(self, timeout: float = CONNECT_TIMEOUT) -> list[dict]:
        """Every tool the server offers, following pagination."""
        tools: list[dict] = []
        cursor = None
        for _ in range(_MAX_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict) and isinstance(item.get("name"), str))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float = CALL_TIMEOUT) -> dict:
        """Run one tool and return the raw MCP result (``content``, ``isError``…)."""
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def stderr_tail(self) -> list[str]:
        link = self._link
        return list(link.stderr) if isinstance(link, _Stdio) else []

    def close(self) -> None:
        self._closed = True
        # A handshake in progress holds the lock; stopping its process (or
        # HTTP client) ends the wait at once instead of after the timeout.
        starting = self._starting
        if starting is not None:
            starting.close()
        with self._lock:
            link, self._link = self._link, None
        if link is not None:
            link.close()


def format_result(result: dict) -> str:
    """MCP content blocks as model-readable text; ``isError`` raises ``ToolError``."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind in {"image", "audio"}:
            size = len(str(block.get("data", ""))) * 3 // 4
            parts.append(f"[{kind}: {block.get('mimeType', 'unknown type')}, about {size} bytes, not shown]")
        elif kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            parts.append(str(resource["text"]) if "text" in resource else f"[resource: {resource.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[link: {block.get('uri', '')}] {block.get('name', '')}".strip())
        else:
            parts.append(json.dumps(block, ensure_ascii=False)[:2000])
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts) or "(no output)"
    if result.get("isError"):
        raise ToolError(text)
    return text


def _schema(raw: dict) -> dict:
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    schema = dict(schema)
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


class McpManager:
    """Owns one client per enabled server and turns their tools into Kova tools.

    ``reload`` blocks while servers connect (in parallel); run it in a worker
    thread from async code. A server that fails is recorded in ``status`` and
    contributes no tools; it never stops the others.
    """

    def __init__(self, home: Path | None = None, *,
                 http_transport: httpx.BaseTransport | None = None):
        self.home = Path(home) if home else None
        self.http_transport = http_transport
        self._servers: list[McpServer] = []
        self._clients: dict[str, McpClient] = {}
        self._keys: dict[str, tuple] = {}
        self._tools: dict[str, list[dict]] = {}
        self._errors: dict[str, str] = {}
        self._connecting: set[McpClient] = set()
        self._lock = threading.RLock()
        self._closed = False

    def _workdir(self, server: McpServer) -> Path | None:
        return self.home / "mcp" / server.name if self.home else None

    def reload(self, servers: Iterable[McpServer], *, timeout: float = CONNECT_TIMEOUT,
               restart: Iterable[str] = ()) -> None:
        """Match the connections to ``servers``; unchanged servers stay connected.

        Names in ``restart`` are reconnected even when unchanged (``kova mcp test``).
        """
        servers = list(servers)
        restart = set(restart)
        with self._lock:
            if self._closed:
                return
            self._servers = servers
            wanted = {s.name: s for s in servers if s.enabled}
            stale = [name for name, client in self._clients.items()
                     if name not in wanted or name in restart or self._keys[name] != _connection_key(wanted[name])]
            for name in stale:
                self._clients.pop(name).close()
                self._keys.pop(name, None)
                self._tools.pop(name, None)
            for name in list(self._errors):
                if name not in wanted or name in restart or name in stale:
                    self._errors.pop(name)
            pending = []
            for server in wanted.values():
                if server.name in self._clients or server.name in self._errors:
                    continue
                try:
                    validate_server(server)
                except ValueError as exc:
                    self._errors[server.name] = str(exc)
                else:
                    pending.append(server)
        if not pending:
            return

        def connect(server: McpServer) -> tuple[McpServer, McpClient | None, list[dict], str]:
            client = McpClient(server, self._workdir(server), http_transport=self.http_transport)
            with self._lock:
                if self._closed:
                    return server, None, [], f"{server.name}: Kova is shutting down"
                self._connecting.add(client)
            try:
                client.start(timeout)
                return server, client, client.list_tools(timeout), ""
            except McpError as exc:
                client.close()
                return server, None, [], str(exc)
            except Exception as exc:  # a broken server must never break Kova
                client.close()
                return server, None, [], f"{server.name}: {type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._connecting.discard(client)

        with ThreadPoolExecutor(max_workers=min(8, len(pending)), thread_name_prefix="mcp-connect") as pool:
            results = list(pool.map(connect, pending))
        with self._lock:
            for server, client, tools, error in results:
                if self._closed or server.name in self._clients:
                    if client is not None:
                        client.close()
                    continue
                if client is None:
                    self._errors[server.name] = error
                else:
                    self._clients[server.name] = client
                    self._keys[server.name] = _connection_key(server)
                    self._tools[server.name] = tools

    def client(self, name: str) -> McpClient:
        with self._lock:
            client = self._clients.get(name)
        if client is None:
            raise ToolError(f"MCP server {name!r} is not connected")
        return client

    def _tool(self, server: McpServer, raw: dict) -> Tool:
        original = raw["name"]
        description = str(raw.get("description") or raw.get("title") or original).strip()
        description = f"[{server.name} MCP] {description}"[:MAX_DESCRIPTION]

        def run(args: dict, _server: str = server.name, _tool: str = original) -> str:
            try:
                return format_result(self.client(_server).call_tool(_tool, args))
            except McpError as exc:
                raise ToolError(str(exc)) from None

        return Tool(tool_name(server.name, original), description, _schema(raw), run, external=True)

    def tools(self) -> list[Tool]:
        """The enabled tools of every connected server, ``server__tool`` named."""
        tools: list[Tool] = []
        seen: set[str] = set()
        with self._lock:
            for server in self._servers:
                if not server.enabled or server.name not in self._clients:
                    continue
                for raw in self._tools.get(server.name, []):
                    name = tool_name(server.name, raw["name"])
                    if raw["name"] in server.disabled_tools or name in seen:
                        continue
                    seen.add(name)
                    tools.append(self._tool(server, raw))
        return tools

    def status(self) -> list[dict]:
        """One JSON-safe row per configured server; never includes env or headers."""
        rows = []
        with self._lock:
            for server in self._servers:
                connected = server.name in self._clients
                state = ("off" if not server.enabled else "connected" if connected
                         else "error" if server.name in self._errors else "connecting")
                client = self._clients.get(server.name)
                rows.append({
                    "name": server.name,
                    "transport": server.transport,
                    "target": server.url or " ".join([server.command, *server.args]),
                    "enabled": server.enabled,
                    "sandbox": server.sandbox,
                    "state": state,
                    "error": self._errors.get(server.name),
                    "server_info": client.info if client else {},
                    "tools": [{
                        "name": raw["name"],
                        "kova_name": tool_name(server.name, raw["name"]),
                        "description": str(raw.get("description") or "")[:300],
                        "enabled": raw["name"] not in server.disabled_tools,
                    } for raw in self._tools.get(server.name, [])],
                })
        return rows

    def close(self) -> None:
        """Stop every server; later reloads do nothing."""
        with self._lock:
            self._closed = True
            clients = [*self._clients.values(), *self._connecting]
            self._clients.clear()
        for client in clients:
            client.close()


def build_mcp_tools(servers: Iterable[McpServer], manager: McpManager | None = None, *,
                    home: Path | None = None, timeout: float = CONNECT_TIMEOUT) -> list[Tool]:
    """Connect ``servers`` and return their Kova tools.

    Pass a ``manager`` to own (and later ``close``) the connections; otherwise a
    new one is created and lives as long as the returned tools.
    """
    manager = manager or McpManager(home)
    manager.reload(servers, timeout=timeout)
    return manager.tools()
