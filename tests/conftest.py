"""Shared test setup.

Real installs start with a placeholder model (no model chosen until `kova setup`).
Most tests talk to a scripted FakeModel and need a configured model, so they get a
stand-in default; tests marked ``real_default`` see the shipped one.
"""

import pytest

import kova.config


def pytest_configure(config):
    config.addinivalue_line("markers", "real_default: use the shipped placeholder default model")


@pytest.fixture(autouse=True)
def _test_default_model(request, monkeypatch):
    if request.node.get_closest_marker("real_default"):
        return
    monkeypatch.setattr(kova.config, "DEFAULT_MODEL", {
        "name": "local", "base_url": "http://model.test/v1", "model": "test-model", "context": 32768,
    })


@pytest.fixture
def no_sandbox(monkeypatch):
    """Pretend the OS sandbox is unavailable (for approval-flow tests)."""
    from kova import sandbox
    monkeypatch.setattr(sandbox, "available", lambda: False)
