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
