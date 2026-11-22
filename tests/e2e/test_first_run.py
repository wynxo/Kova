"""Fresh installs against an actual loopback OpenAI-compatible SSE endpoint."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Thread
import time

import httpx
import pytest

from tests.fake_kova import Terminal

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not sys.platform.startswith("linux") or os.getenv("KOVA_E2E") != "1",
                       reason="Linux first-run E2E; enable with KOVA_E2E=1"),
]
REPO = Path(__file__).resolve().parents[2]
REPLY = "Hello from the stub."
REASONING = "A reasoning delta from the stub."


class OpenAIStub(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_body(self, body, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self.send_body(json.dumps({"data": [{"id": "stub-model"}]}).encode())

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not body.get("stream"):
            self.send_body(json.dumps({"choices": [{"message": {"content": "Demo chat"}}]}).encode())
            return
        tools = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "reminder_list" in tools and not any(m["role"] == "tool" for m in body["messages"]):
            deltas = [{"tool_calls": [{"index": 0, "id": "call_demo", "type": "function",
                                       "function": {"name": "reminder_list", "arguments": "{}"}}]}]
            finish = "tool_calls"
        else:
            deltas = [{"reasoning_content": REASONING}, {"content": "Hello "},
                      {"content": "from the stub."}]
            finish = "stop"
        frames = [{"choices": [{"delta": d, "finish_reason": None}]} for d in deltas]
        frames.append({"choices": [{"delta": {}, "finish_reason": finish}]})
        payload = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        self.send_body((payload + "data: [DONE]\n\n").encode(), "text/event-stream")


@pytest.fixture
def stub():
    with ThreadingHTTPServer(("127.0.0.1", 0), OpenAIStub) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_http(url, process=None):
    with httpx.Client(trust_env=False, timeout=1) as client:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                pytest.fail(f"Process exited early with {process.returncode}")
            try:
                response = client.get(url)
                if response.is_success:
                    return response
            except httpx.TransportError:
                pass
            time.sleep(0.1)
    pytest.fail("Local HTTP endpoint did not become ready")


def wait_for_port_to_close(port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as conn:
            conn.settimeout(1)
            if conn.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)
    pytest.fail("Background server did not release its port")


@contextmanager
def running(command, cwd, env):
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=8)
        finally:
            # A failed startup may leave a browser or backend descendant.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


@pytest.fixture(params=["clone", "piped"])
def install(request, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    data = home / ".kova"
    clone = tmp_path / "checkout"
    # Do not inherit any Kova/uv/XDG overrides from the development shell.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("KOVA_", "UV_", "XDG_", "GIT_", "NPM_CONFIG_"))
           and key not in {"PYTHONPATH", "VIRTUAL_ENV", "ELECTRON_RUN_AS_NODE"}}
    env.update(HOME=str(home), KOVA_HOME=str(data), GIT_CONFIG_NOSYSTEM="1",
               XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_DATA_HOME=str(home / ".local/share"), TERM="xterm-256color",
               UV_PYTHON=str(Path(sys.executable).resolve()),
               PATH=f"{home / '.local/bin'}:{env['PATH']}")
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(clone)],
                   cwd=home, env=env, check=True, timeout=30)
    if request.param == "clone":
        subprocess.run(["./install"], cwd=clone, env=env, check=True, timeout=240)
    else:
        with (clone / "install.sh").open("rb") as script:
            subprocess.run(["sh", "-s", "--", "--no-desktop"], cwd=home,
                           env={**env, "KOVA_SOURCE": str(clone)}, stdin=script,
                           check=True, timeout=240)
    cli = home / ".local/bin/kova"
    assert cli.is_file()
    data.mkdir(exist_ok=True)
    port = free_port()
    config = data / "config.toml"
    config.write_text(f"port = {port}\n")

    def kova(*args, check=True):
        result = subprocess.run([cli, *args], cwd=home, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    try:
        yield request.param, home, data, config, port, env, cli, kova
    finally:
        kova("stop", check=False)


def test_first_run(stub, install):
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

    kind, home, data, config, port, env, cli, kova = install
    base = f"http://127.0.0.1:{port}"
    config.write_text(config.read_text() + f'\n[[models]]\nname = "local"\nbase_url = "{stub}/v1"\nmodel = ""\n')
    term = Terminal(home, home, env=env, executable=cli)
    try:
        term.screen("Run the setup wizard first?")
        term.type(b"n\r")
        term.screen("Name your assistant")
        term.type(b"Demo Assistant\r")
        term.screen("Choose a tone")
        term.type(b"\r")
        term.screen('Try "')
        term.type(b"/exit\r")
        assert term.exit_status() == 0
    finally:
        term.close()

    token = (data / "token").read_text().strip()
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base, headers=headers, trust_env=False, timeout=5) as api:
        assert api.get("/health?probe=1").json()["model_ok"] is False
        assert "not configured" in kova("status").stdout
        before = api.get("/chats").json()
        failed = kova("ask", "hi", check=False)
        assert failed.returncode == 1 and "kova setup" in failed.stderr
        assert api.get("/chats").json() == before

        kova("setup", "--url", stub, "--model", "stub-model", "--yes")
        assert api.get("/persona").json()["name"] == "Demo Assistant"
        assert REPLY in kova("ask", "hi").stdout
        result = json.loads(kova("ask", "--json", "hi").stdout)
        assert result["answer"] == REPLY
        assert result["tools"] == ["reminder_list"]

        term = Terminal(home, home, "--new", env=env,
                        executable=cli)
        try:
            term.screen('Try "')
            term.type(b"terminal hello\r")
            term.screen(REPLY)
            term.type(b"/exit\r")
            assert term.exit_status() == 0
        finally:
            term.close()

        kova("stop")
        wait_for_port_to_close(port)
        with running([cli, "serve"], home, env) as server:
            wait_http(base + "/health?probe=0", server)
            assert api.get("/").headers["location"] == "/app"
            ticket = api.post("/app/session").json()["ticket"]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(env=env)
                try:
                    page = browser.new_page()
                    with page.expect_response(base + "/app/login") as login:
                        assert page.goto(f"{base}/#ticket={ticket}").ok
                    assert login.value.ok and page.url.startswith(base + "/app")
                    prompt = page.locator("#prompt")
                    expect(prompt).to_be_focused()
                    prompt.fill("browser hello")
                    with page.expect_response(lambda response: response.request.method == "POST"
                                              and response.url.endswith("/messages")) as turn:
                        page.locator("#send").click()
                    expect(page.get_by_text(REPLY, exact=True)).to_be_visible()
                    assert turn.value.ok
                    try:
                        body = turn.value.text()
                    except PlaywrightError:
                        # Chromium sometimes drops a finished stream's body; the page still has it.
                        body = page.content()
                    assert REASONING in body
                finally:
                    browser.close()

        if kind == "clone":
            launch = data / "desktop/launch"
            assert launch.is_file(), "Installer did not register the desktop launcher"
            desktop_env = {**env, "PATH": os.defpath}
            for expected in (base + "/app", "status.html?state=error"):
                debug_port = free_port()
                debug = f"http://127.0.0.1:{debug_port}"
                with running([launch, f"--remote-debugging-port={debug_port}"], home, desktop_env) as electron:
                    wait_http(debug + "/json/version", electron)
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.connect_over_cdp(debug)
                        context = browser.contexts[0]
                        page = context.pages[0] if context.pages else context.wait_for_event("page")
                        page.wait_for_url(f"**{expected}**", timeout=15000)
                        if expected.startswith(base):
                            expect(page.locator("#prompt")).to_be_focused()
                        else:
                            expect(page.get_by_text("Couldn’t start Kova", exact=True)).to_be_visible()
                # Occupy the configured port with an unrelated HTTP server.
                config.write_text(config.read_text().replace(f"port = {port}\n",
                                                             f"port = {stub.rsplit(':', 1)[1]}\n", 1))
