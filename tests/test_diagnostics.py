"""Credential-safe first-run diagnostics; never starts a server itself."""

import httpx

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


def _fake_httpx(make_response):
    """A drop-in for the httpx module: Client is a no-network context manager."""
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return make_response()

    return type("FakeHttpx", (), {
        "Client": _Client,
        "ConnectError": httpx.ConnectError,
        "HTTPError": httpx.HTTPError,
    })


def _install(monkeypatch, tmp_path, *, make_response, active_model="", available=None):
    """Patch diagnose()'s three external deps so no network touches disk or wire."""
    available = list(available or [])
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model=active_model))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(make_response))
    monkeypatch.setattr(diag, "list_models", lambda provider, timeout=None: available)


def test_fails_when_config_cannot_be_read(monkeypatch, tmp_path):
    def boom():
        raise OSError("no such file")
    monkeypatch.setattr(diag, "load_config", boom)
    checks = diag.diagnose()
    assert checks == [(False, 'Cannot read config.toml. Fix its TOML and provider references, then run kova doctor.')]


def test_reports_server_when_running(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert (True, "Kova server is running.") in checks


def test_reports_port_occupied_when_health_is_bad(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(503, {"nope": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert (False, "Port is occupied by another service. Choose a free port in config.toml.") in checks


def test_reports_port_occupied_when_health_is_not_a_dict(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, ["not", "a", "dict"]),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert (False, "Port is occupied by another service. Choose a free port in config.toml.") in checks


def test_reports_stopped_server_on_connect_error(monkeypatch, tmp_path):
    def boom():
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model="llama3"))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(boom))
    monkeypatch.setattr(diag, "list_models", lambda provider, timeout=None: ["llama3"])
    checks = diag.diagnose()
    assert (True, "Server is stopped; kova starts it automatically.") in checks


def test_reports_not_responding_on_http_error(monkeypatch, tmp_path):
    def boom():
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model="llama3"))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(boom))
    monkeypatch.setattr(diag, "list_models", lambda provider, timeout=None: ["llama3"])
    checks = diag.diagnose()
    assert (False, "Server is not responding correctly. Check server.log and the configured port.") in checks


def test_reports_no_active_model_when_blank(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="", available=[])
    checks = diag.diagnose()
    assert (False, "No active model configured. Run kova setup.") in checks


def test_reports_model_unavailable_when_not_in_provider(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["qwen"])  # active "llama3" is not listed
    checks = diag.diagnose()
    assert (False, "Active model is not available. Load it or choose another with kova setup.") in checks


def test_reports_provider_reachable_when_model_present(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert (True, "Provider reachable; active model is listed.") in checks


def test_reports_provider_error(monkeypatch, tmp_path):
    def boom(provider, timeout=None):
        raise ProviderError("Bad API key")
    monkeypatch.setattr(diag, "load_config", lambda: _cfg(tmp_path, model="llama3"))
    monkeypatch.setattr(diag, "httpx", _fake_httpx(lambda: _Resp(200, {"version": "0.1.0", "model_ok": True})))
    monkeypatch.setattr(diag, "list_models", boom)
    checks = diag.diagnose()
    assert (False, "Bad API key Run kova setup.") in checks


def test_reports_writable_memory_dir(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, make_response=lambda: _Resp(200, {"version": "0.1.0", "model_ok": True}),
             active_model="llama3", available=["llama3"])
    checks = diag.diagnose()
    assert (True, "Memory directory is writable (or can be created).") in checks


def test_load_config_error_is_first_check(monkeypatch, tmp_path):
    def boom():
        raise ValueError("bad toml")
    monkeypatch.setattr(diag, "load_config", boom)
    checks = diag.diagnose()
    assert checks[0][0] is False
    assert "Cannot read config" in checks[0][1]
