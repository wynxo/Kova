"""Credential-safe first-run diagnostics; never starts a server itself."""

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import kova.diagnostics as diag
from kova.config import Config, ModelEntry, ProviderEntry
from kova.providers import ProviderError


def _cfg(tmp_path, model="", models=None):
    models = models or [
        ModelEntry(name="local", base_url="http://127.0.0.1:11434/v1",
                   model=model, provider="local")
    ]
    return Config(home=tmp_path, models=models, active="local",
                  memory_path=tmp_path / "memory", files_path=tmp_path,
                  port=7700,
                  providers=[ProviderEntry(id="local", name="Local",
                                           base_url="http://127.0.0.1:11434/v1")])


class _Resp:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


def _fake_httpx(respond):
    """A drop-in for the httpx module: Client is a no-network context manager."""
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            return respond(url, **kwargs)

    return type("FakeHttpx", (), {
        "Client": _Client,
        "ConnectError": httpx.ConnectError,
        "HTTPError": httpx.HTTPError,
    })


def _install(monkeypatch, tmp_path, *, make_response, active_model="", available=None,
             owner_status=200):
    """Patch diagnose()'s external deps so no network touches disk or wire."""
    available = list(available or [])
    token = tmp_path / "token"
    token.write_text("test-token")
    os.chmod(token, 0o600)

    def respond(url, **kwargs):
        path, _, query = url.partition("?")
        if path.endswith("/health"):
            # kova doctor asks for the live answer, not the cached one (#98).
            assert query == "probe=1", url
            return make_response()
        if path.endswith("/models"):
            return _Resp(owner_status, {})
        raise AssertionError(url)

    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model=active_model))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(respond))
    monkeypatch.setattr(diag, "list_models", lambda provider, timeout=None: available)


def test_fails_when_config_cannot_be_read(monkeypatch, tmp_path):
    def boom():
        raise OSError("no such file")
    monkeypatch.setattr(diag, "load_config", boom)
    checks = diag.diagnose()
    assert checks == [diag.Check(
        "config", False,
        "Cannot read config.toml. Fix its TOML and provider references, then run kova doctor.",
        "Fix config.toml, then run kova doctor.",
    )]


def test_reports_server_when_running(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert diag.Check("server", True, "Kova server is running.") in checks


def test_reports_port_occupied_when_health_is_bad(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(503, {"nope": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert diag.Check("server", False,
                      "Port is occupied by another service. Choose a free port in config.toml.",
                      "Choose another port in config.toml") in checks


def test_reports_port_occupied_when_health_is_not_a_dict(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, ["not", "a", "dict"]),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert diag.Check("server", False,
                      "Port is occupied by another service. Choose a free port in config.toml.",
                      "Choose another port in config.toml") in checks


def test_reports_stopped_server_on_connect_error(monkeypatch, tmp_path):
    def boom():
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model="llama3"))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(lambda url, **kwargs: boom()))
    monkeypatch.setattr(diag, "list_models", lambda provider, timeout=None: ["llama3"])
    checks = diag.diagnose()
    assert diag.Check("server", True, "Server is stopped; kova starts it automatically.") in checks


def test_reports_not_responding_on_http_error(monkeypatch, tmp_path):
    def boom():
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model="llama3"))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(lambda url, **kwargs: boom()))
    monkeypatch.setattr(diag, "list_models", lambda provider, timeout=None: ["llama3"])
    checks = diag.diagnose()
    assert diag.Check("server", False,
                      "Server is not responding correctly. Check server.log and the configured port.",
                      "Check server.log") in checks


def test_reports_no_active_model_when_blank(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="", available=[])
    checks = diag.diagnose()
    assert diag.Check("model", False, "No active model configured. Run kova setup.", "kova setup") in checks


def test_reports_model_unavailable_when_not_in_provider(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["qwen"])  # active "llama3" is not listed
    checks = diag.diagnose()
    assert diag.Check("model", False,
                      "Active model is not available. Load it or choose another with kova setup.",
                      "kova setup") in checks


def test_reports_provider_reachable_when_model_present(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert diag.Check("model", True, "Provider reachable; active model is listed.") in checks


def test_reports_provider_error(monkeypatch, tmp_path):
    def boom(provider, timeout=None):
        raise ProviderError("Bad API key")
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model="llama3"))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(
        lambda url, **kwargs: _Resp(200, {"version": "0.1.0", "model_ok": True})))
    monkeypatch.setattr(diag, "list_models", boom)
    checks = diag.diagnose()
    assert diag.Check("model", False, "Bad API key Run kova setup.", "kova setup") in checks


def test_reports_writable_memory_dir(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert diag.Check("memory", True,
                      "Memory directory is writable (or can be created).") in checks


def test_load_config_error_is_first_check(monkeypatch, tmp_path):
    def boom():
        raise ValueError("bad toml")
    monkeypatch.setattr(diag, "load_config", boom)
    checks = diag.diagnose()
    assert checks[0].ok is False
    assert "Cannot read config" in checks[0].detail


def test_checks_keep_the_existing_order(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert [check.name for check in checks] == [
        "config", "server", "model", "memory", "port-owner", "sandbox", "node",
        "token", "disk", "schema", "tools",
    ]


def test_port_owner_is_this_home(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"], owner_status=200)
    checks = diag.diagnose()
    assert diag.Check("port-owner", True, "Port is owned by this KOVA_HOME.") in checks


def test_port_owner_is_a_different_kova(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"], owner_status=401)
    checks = diag.diagnose()
    assert diag.Check(
        "port-owner", False,
        "Port is held by a different Kova (its token does not match this KOVA_HOME).",
        "Stop the other server or choose another port",
    ) in checks


def test_port_owner_without_a_token(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    (tmp_path / "token").unlink()
    checks = diag.diagnose()
    assert diag.Check(
        "port-owner", False,
        "A Kova server is running without this KOVA_HOME token; another Kova probably owns the port.",
        "Stop the other server or choose another port",
    ) in checks


def test_sandbox_reports_landlock_abi(monkeypatch):
    monkeypatch.setattr(diag.sandbox, "available", lambda: True)
    monkeypatch.setattr(diag.sandbox, "abi_version", lambda: 6)
    assert diag._sandbox_check() == diag.Check("sandbox", True, "landlock abi 6")


def test_sandbox_disabled_by_env_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(diag.sandbox, "available", lambda: False)
    monkeypatch.setattr(diag.sys, "platform", "linux")
    monkeypatch.setenv("KOVA_SANDBOX", "off")
    check = diag._sandbox_check()
    assert check.ok and "Disabled by KOVA_SANDBOX" in check.detail


def test_sandbox_missing_on_linux_needs_attention(monkeypatch):
    monkeypatch.setattr(diag.sandbox, "available", lambda: False)
    monkeypatch.setattr(diag.sys, "platform", "linux")
    monkeypatch.delenv("KOVA_SANDBOX", raising=False)
    check = diag._sandbox_check()
    assert not check.ok and "Landlock is unavailable" in check.detail


def test_sandbox_absent_on_other_platforms_is_expected(monkeypatch):
    monkeypatch.setattr(diag.sandbox, "available", lambda: False)
    monkeypatch.setattr(diag.sys, "platform", "darwin")
    assert diag._sandbox_check().ok


def test_node_missing_falls_back_to_the_web_ui(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)
    check = diag._node_check()
    assert check.ok and "web UI" in check.detail


def test_node_without_npm_needs_attention(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    check = diag._node_check()
    assert not check.ok and "npm is missing" in check.detail


def test_node_too_old_needs_attention(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(diag.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(stdout="v20.11.0"))
    check = diag._node_check()
    assert not check.ok and "too old" in check.detail


def test_node_22_is_enough(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(diag.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(stdout="v22.1.0"))
    check = diag._node_check()
    assert check.ok and "Node.js 22.1.0" in check.detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are not exposed on Windows")
def test_token_file_mode_is_checked(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret")
    os.chmod(token, 0o600)
    assert diag._token_check(_cfg(tmp_path)).ok
    os.chmod(token, 0o644)
    check = diag._token_check(_cfg(tmp_path))
    assert not check.ok and "0644" in check.detail


def test_missing_token_is_created_on_first_start(tmp_path):
    assert diag._token_check(_cfg(tmp_path)).ok


def test_disk_space_needs_attention_when_low(monkeypatch, tmp_path):
    monkeypatch.setattr(diag.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=50 * 1024 * 1024))
    check = diag._disk_check(_cfg(tmp_path))
    assert not check.ok and "Only 50 MB free" in check.detail


def test_disk_space_reports_gigabytes(monkeypatch, tmp_path):
    monkeypatch.setattr(diag.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=10 * 1024 ** 3))
    check = diag._disk_check(_cfg(tmp_path))
    assert check.ok and check.detail == "10.0 GB free in KOVA_HOME."


def test_schema_version_and_unknown_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('schema_version = 2\nactive = "local"\n')
    assert diag._schema_check(_cfg(tmp_path)).ok

    path.write_text('schema_version = 2\nwrong_key = 1\n')
    check = diag._schema_check(_cfg(tmp_path))
    assert not check.ok and "wrong_key" in check.detail

    path.write_text('schema_version = 3\n')
    check = diag._schema_check(_cfg(tmp_path))
    assert not check.ok and "newer" in check.detail

    path.write_text('active = "local"\n')
    check = diag._schema_check(_cfg(tmp_path))
    assert check.ok and "version 1" in check.detail


def test_tools_check_reads_the_adapter_capability(monkeypatch):
    entry = SimpleNamespace(_provider=SimpleNamespace(adapter="anthropic", kind="anthropic"))
    assert diag._tools_check(entry).ok
    monkeypatch.setattr(diag, "supports", lambda provider, capability: False)
    check = diag._tools_check(entry)
    assert not check.ok and "does not carry tool calls" in check.detail


def test_tools_check_without_an_active_model():
    check = diag._tools_check(None)
    assert not check.ok and "No active model" in check.detail


def test_doctor_json_is_stable(monkeypatch):
    from typer.testing import CliRunner

    from kova import cli
    monkeypatch.setattr("kova.diagnostics.diagnose", lambda: [
        diag.Check("sandbox", True, "landlock abi 6"),
        diag.Check("model", False, "No active model configured. Run kova setup.", "kova setup"),
    ])
    result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == [
        {"ok": True, "check": "sandbox", "detail": "landlock abi 6"},
        {"ok": False, "check": "model", "detail": "No active model configured. Run kova setup."},
    ]


def test_doctor_human_output_ends_with_a_verdict(monkeypatch):
    from typer.testing import CliRunner

    from kova import cli
    monkeypatch.setattr("kova.diagnostics.diagnose", lambda: [
        diag.Check("sandbox", True, "landlock abi 6"),
    ])
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "All checks passed." in result.output and "(1 ok)" in result.output


def test_doctor_failure_prints_the_next_step(monkeypatch):
    from typer.testing import CliRunner

    from kova import cli
    monkeypatch.setattr("kova.diagnostics.diagnose", lambda: [
        diag.Check("model", False, "No active model configured. Run kova setup.", "kova setup"),
    ])
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "Next: kova setup" in result.output and "1 needs attention" in result.output


def test_every_run_kova_doctor_message_has_a_doctor_hint():
    """A message telling the user to run doctor must also show how to run it."""
    from kova.cli import _hint_for
    pattern = re.compile(r"""(['"])([^'"]*Run kova doctor[^'"]*)\1""")
    source = Path(__file__).resolve().parents[1] / "src" / "kova"
    messages = [
        match.group(2)
        for path in source.rglob("*.py")
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert messages
    for message in messages:
        hint = _hint_for(message)
        assert hint and "kova doctor" in hint, message


def test_sandbox_failure_also_points_at_doctor():
    from kova.cli import _hint_for
    hint = _hint_for("kova: could not establish the command sandbox: permission denied")
    assert hint and "kova doctor" in hint
