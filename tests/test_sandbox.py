"""OS sandbox: the inspection allowlist, confinement, and the fail-closed bootstrap."""

import asyncio
import os
from datetime import datetime
from pathlib import Path
import sys

import pytest

from kova import sandbox
from kova.agent import Agent
from kova.config import load_config
from kova.memory import Memory
from kova.store import Store
from kova.tools import ToolContext, build_tools
from kova.tools import core
from kova.tools.commands import run_command
from tests.fake_model import FakeModel, text, tool

PROBE = '''\
import os, socket
from pathlib import Path

print("marker", os.environ.get("KOVA_SANDBOXED"))
try:
    Path("inside.txt").write_text("ok")
    print("inside write ok")
except OSError as exc:
    print("inside write failed", exc.errno)
try:
    Path(os.path.expanduser("~/.kova-sandbox-probe.txt")).write_text("no")
    print("home write allowed")
except OSError as exc:
    print("home write blocked", exc.errno)
try:
    print("home read", len(Path(os.path.expanduser("~/.bashrc")).read_text()))
except OSError as exc:
    print("home read blocked", exc.errno)
try:
    socket.create_connection(("1.1.1.1", 80), timeout=3)
    print("network allowed")
except OSError as exc:
    print("network blocked", exc.errno)
'''

needs_backend = pytest.mark.skipif(sandbox.backend() is None, reason="no OS sandbox on this machine")


@pytest.mark.parametrize("command, safe", [
    ("ls -la", True),
    ("cat README.md", True),
    ("rg -n pattern src", True),
    ("git status", True),
    ("git diff --stat", True),
    ("git", True),
    ("python3 -m pytest -q", True),
    ("echo hi", False),
    ("npm install", False),
    ("git push --force", False),
    ("git reset --hard HEAD", False),
    ("rm -rf build", False),
    ("sudo ls", False),
    ("/usr/bin/ls", False),
    ("cat a | grep b", False),
    ("ls && rm x", False),
    ("ls > out.txt", False),
    ("echo $HOME", False),
    ('python3 -c "print(1)"', False),
    ("", False),
])
def test_only_listed_inspection_commands_may_skip_approval(command, safe):
    assert sandbox.is_safe(command) is safe


def test_disabling_the_sandbox_returns_to_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_SANDBOX", "0")
    sandbox.backend.cache_clear()
    try:
        assert sandbox.backend() is None
        assert sandbox.wrap("ls", tmp_path) is None
        assert sandbox.auto_runs("run_command", {"command": "ls"}, tmp_path) is False
    finally:
        sandbox.backend.cache_clear()


def test_auto_runs_never_covers_other_tools(tmp_path):
    assert sandbox.auto_runs("write_file", {"path": "x"}, tmp_path) is False
    assert sandbox.auto_runs("read_file", {"path": "x"}, tmp_path) is False


def test_bootstrap_refuses_to_run_when_the_domain_fails(tmp_path, monkeypatch, capsys):
    taken = []

    def unavailable(*args, **kwargs):
        raise sandbox.SandboxUnavailable("no landlock support here")

    monkeypatch.setattr(sandbox, "restrict", unavailable)
    monkeypatch.setattr(sandbox.os, "execvp", lambda *args: taken.append(args))
    assert sandbox.main(["--cwd", str(tmp_path), "--", "echo", "hi"]) == 126
    assert taken == [] and "The command was not run" in capsys.readouterr().err


def test_bootstrap_execs_the_command_once_the_domain_is_up(tmp_path, monkeypatch):
    taken = []
    monkeypatch.setattr(sandbox, "restrict", lambda cwd, **kwargs: None)
    monkeypatch.setattr(sandbox.os, "execvp", lambda program, argv: taken.append((program, argv)))
    monkeypatch.setattr(os, "environ", os.environ.copy())
    assert sandbox.main(["--cwd", str(tmp_path), "--", "echo", "hi"]) == 127
    assert taken == [("echo", ["echo", "hi"])]
    assert os.environ["KOVA_SANDBOXED"] == "landlock"


def test_bootstrap_without_a_command_does_not_restrict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "restrict", lambda *args, **kwargs: pytest.fail("restricted"))
    assert sandbox.main(["--cwd", str(tmp_path)]) == 2
    assert "usage" in capsys.readouterr().err


def test_readable_paths_exclude_the_home_folder(tmp_path):
    home = Path.home().resolve()
    readable = sandbox.readable_paths(tmp_path)
    assert tmp_path.resolve() in readable
    assert Path("/usr").resolve() in readable
    # The user's home is not readable unless it is the working folder itself.
    assert not any(path == home or path.is_relative_to(home) for path in readable
                   if path != tmp_path.resolve() and tmp_path.resolve().is_relative_to(home))


@needs_backend
async def test_a_sandboxed_command_is_confined(tmp_path):
    working = tmp_path / "work"
    working.mkdir()
    (working / "probe.py").write_text(PROBE, encoding="utf-8")
    command = "python3 probe.py"
    argv = sandbox.wrap(command, working)
    assert argv is not None
    result = await run_command(command, working, argv=argv)
    assert result["ok"], result["output"]
    assert f"marker {sandbox.backend()}" in result["output"]
    assert "inside write ok" in result["output"]
    assert "home write blocked" in result["output"]
    assert "network blocked" in result["output"] or "network allowed" not in result["output"]
    assert not (Path.home() / ".kova-sandbox-probe.txt").exists()
    if sandbox.backend() == "landlock":
        # Bubblewrap keeps the file system readable; landlock does not.
        assert "home read blocked" in result["output"]


@needs_backend
async def test_a_sandboxed_command_cannot_signal_outside_its_domain(tmp_path):
    if sandbox.landlock_abi() < 6:
        pytest.skip("signal scoping needs landlock ABI 6")
    working = tmp_path / "work"
    working.mkdir()
    victim = await asyncio.create_subprocess_exec("sleep", "20")
    try:
        (working / "kill.py").write_text(
            "import os, signal, sys\ntry:\n    os.kill(int(sys.argv[1]), signal.SIGKILL)\n"
            "    print('signal allowed')\nexcept OSError as exc:\n    print('signal blocked', exc.errno)\n",
            encoding="utf-8")
        command = f"python3 kill.py {victim.pid}"
        result = await run_command(command, working, argv=sandbox.wrap(command, working))
        assert "signal blocked" in result["output"], result["output"]
        assert victim.returncode is None
    finally:
        victim.kill()
        await victim.wait()


@needs_backend
async def test_a_command_outside_the_allowlist_runs_without_the_sandbox(tmp_path):
    assert sandbox.wrap("echo hi", tmp_path) is None
    result = await run_command("echo hi", tmp_path)
    assert result["ok"] and "hi" in result["output"]


async def _turn(tmp_path, monkeypatch, scripts, answers):
    """Drive one Agent turn, answering approval prompts as they arrive."""
    cfg = load_config(tmp_path / "home")
    store = Store(tmp_path / "db.sqlite")
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    working = tmp_path / "work"
    working.mkdir(exist_ok=True)
    chat = store.create_chat(cwd=str(working))
    agent = Agent(cfg, store, memory, tools, transport=FakeModel(scripts).transport)
    events = []
    async for event in agent.run_turn(chat["id"], "go"):
        events.append(event)
        if event["type"] == "approval":
            agent.approvals.answer(chat["id"], event["id"], *answers)
    return events


async def test_a_sandboxed_command_skips_approval(tmp_path, monkeypatch):
    marker = tmp_path / "sandbox-used"
    wrapped = []

    def fake_wrap(command, cwd):
        wrapped.append(command)
        return [sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('yes'); print('sandbox ran')"]

    monkeypatch.setattr(sandbox, "wrap", fake_wrap)
    monkeypatch.setattr(core, "sandbox_argv", fake_wrap)
    events = await _turn(tmp_path, monkeypatch,
                         [tool("run_command", {"command": "ls -la"}), text("Done")], ("deny", None))
    assert not any(event["type"] == "approval" for event in events)
    assert any(event["type"] == "tool_result" and event["ok"] for event in events)
    assert marker.exists() and set(wrapped) == {"ls -la"}   # decision, then execution


async def test_a_command_outside_the_allowlist_still_asks(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "backend", lambda: "landlock")
    events = await _turn(tmp_path, monkeypatch,
                         [tool("run_command", {"command": "echo hi"}), text("Done")],
                         ("deny", "Use read_file instead"))
    approvals = [event for event in events if event["type"] == "approval"]
    assert len(approvals) == 1 and approvals[0]["tool"] == "run_command"
    assert any(event["type"] == "tool_result" and not event["ok"]
               and "Use read_file instead" in event["output"] for event in events)


async def test_an_unavailable_sandbox_never_runs_unsandboxed(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_SANDBOX", "0")
    sandbox.backend.cache_clear()
    try:
        events = await _turn(tmp_path, monkeypatch,
                             [tool("run_command", {"command": "ls -la"}), text("Done")], ("deny", None))
    finally:
        sandbox.backend.cache_clear()
    approvals = [event for event in events if event["type"] == "approval"]
    assert len(approvals) == 1
    assert not any(event["type"] == "tool_result" and event["ok"] for event in events)
