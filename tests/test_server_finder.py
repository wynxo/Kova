"""The model server finder: loose addresses, probing, and the network scan."""

import httpx
import pytest

from kova import server_finder as sf


def _transport(servers: dict[str, list[str]], locked: tuple[str, ...] = ()):
    def handler(request: httpx.Request) -> httpx.Response:
        base = str(request.url).removesuffix("/models")
        if base in locked:
            return httpx.Response(401)
        if base in servers:
            return httpx.Response(200, json={"data": [{"id": m} for m in servers[base]]})
        raise httpx.ConnectError("Connection refused", request=request)
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("typed, first", [
    ("192.168.1.50", "http://192.168.1.50:11434/v1"),
    ("homelab:8080", "http://homelab:8080/v1"),
    ("10.0.0.5:8080/v1", "http://10.0.0.5:8080/v1"),
    ("http://box:1234", "http://box:1234/v1"),
    ("openrouter", "https://openrouter.ai/api/v1"),
    ("192,168,1,50:8080", "http://192.168.1.50:8080/v1"),  # German numeric keypad
])
def test_expand_reads_loose_addresses(typed, first):
    assert sf.expand(typed)[0] == first


def test_expand_tries_the_usual_ports_without_one():
    assert sf.expand("pc") == [f"http://pc:{p}/v1" for p in (11434, 8080, 1234, 8000)]


@pytest.mark.parametrize("typed, first", [
    ("http://192.168.1.50:8080", "http://192.168.1.50:8080/v1"),
    ("http://polystrata:8080", "http://polystrata:8080/v1"),
    ("http://homelab:8080/api", "http://homelab:8080/api"),
    ("http://box:1234", "http://box:1234/v1"),
])
def test_expand_urls_try_the_usual_base_then_the_bare_origin(typed, first):
    assert sf.expand(typed)[0] == first


def test_connect_a_server_that_serves_models_without_a_v1_path():
    """A typed OpenAI-compatible URL with no /v1 (e.g. PolyStrata at /models) connects.

    The URL already names the API path, so it is stored and probed as given.
    """
    t = _transport({"http://polystrata:8080": ["poly-model"]})
    found = sf.connect("http://polystrata:8080", transport=t)
    assert found.ok and found.base_url == "http://polystrata:8080"
    assert found.models == ["poly-model"]


def test_connect_a_server_under_a_custom_api_path():
    """A typed API path (http://host:8080/api) is probed and stored as given."""
    t = _transport({"http://127.0.0.1:8080/api": ["poly-model"]})
    found = sf.connect("http://127.0.0.1:8080/api", transport=t)
    assert found.ok and found.base_url == "http://127.0.0.1:8080/api"
    assert found.models == ["poly-model"]


def test_normalize_base_adds_v1_only_to_a_bare_host():
    """A bare host:port gets /v1; a URL with an API path keeps its single path."""
    assert sf.normalize_base("192.168.1.50:8080") == "http://192.168.1.50:8080/v1"
    assert sf.normalize_base("http://polystrata:8080") == "http://polystrata:8080/v1"
    assert sf.normalize_base("http://homelab:8080/api") == "http://homelab:8080/api"


def test_expand_rejects_nonsense():
    with pytest.raises(ValueError):
        sf.expand("   ")
    with pytest.raises(ValueError):
        sf.expand("host:abc")


def test_connect_finds_the_port_that_answers():
    t = _transport({"http://pc:8080/v1": ["tiel"]})
    found = sf.connect("pc", transport=t)
    assert found.ok and found.base_url == "http://pc:8080/v1"
    assert found.models == ["tiel"] and found.kind == "llama.cpp"


def test_connect_reports_a_server_that_wants_a_key():
    t = _transport({}, locked=("https://openrouter.ai/api/v1",))
    found = sf.connect("openrouter", transport=t)
    assert found.needs_key and found.kind == "OpenRouter" and not found.ok


def test_connect_explains_when_nothing_answers():
    found = sf.connect("127.0.0.1:9999", transport=_transport({}))
    assert not found.answered
    assert found.error == "Nothing is listening on 127.0.0.1:9999 on this computer."


def test_discover_with_a_fake_network_keeps_only_model_servers():
    t = _transport({"http://10.9.9.2:11434/v1": ["llama"], "http://127.0.0.1:1234/v1": ["qwen"]})
    found = sf.discover(networks=["10.9.9.0/30"], ports=[11434, 1234], transport=t)
    assert [f.base_url for f in found] == ["http://127.0.0.1:1234/v1", "http://10.9.9.2:11434/v1"]
    assert found[0].name == "this computer"
