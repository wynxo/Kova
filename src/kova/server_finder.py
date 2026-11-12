"""Find model servers from what a person types, or on their own network.

``expand`` turns a loose address — ``192.168.1.50``, ``homelab``,
``homelab:8080``, ``10.0.0.5:8080/v1``, ``openrouter`` — into the base URLs it
most likely means. ``connect`` probes those at once and keeps the first that
answers like a model server (Ollama's and any OpenAI-compatible API both
answer on ``/v1/models``). ``discover`` looks for such servers on this computer
and its local /24 networks, on the ports the common servers use; it only ever
runs when asked. Nothing here touches the network at import time, and proxies
are bypassed like everywhere else in Kova.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx

OLLAMA_PORT = 11434
OPENROUTER_URL = "https://openrouter.ai/api/v1"
ALIASES = {"openrouter": OPENROUTER_URL, "openrouter.ai": OPENROUTER_URL}

# The ports the usual servers listen on, and what runs on each.
PORT_KINDS = {
    11434: "Ollama",
    8080: "llama.cpp",
    1234: "LM Studio",
    8000: "vLLM",
    5001: "KoboldCpp",
    4000: "LiteLLM",
    1337: "Jan",
}
SCAN_PORTS = tuple(PORT_KINDS)

PROBE_TIMEOUT = 2.0         # seconds to ask one URL whether it is a model server
SCAN_CONNECT_TIMEOUT = 0.4  # seconds per TCP port check while scanning
SCAN_PROBE_TIMEOUT = 2.5    # seconds to identify one listening port

_SKIP_INTERFACES = ("docker", "br-", "veth", "virbr", "lo", "tun", "tap", "wg", "zt")
_IPV4_TYPED = re.compile(r"(?<![\w.,])(\d{1,3})[.,](\d{1,3})[.,](\d{1,3})[.,](\d{1,3})(?![\w.,])")


@dataclass
class Found:
    """A server that answered like a model server — or why one didn't."""

    base_url: str  # the API base URL, ready for ModelEntry
    kind: str = ""  # "Ollama", "LM Studio", "OpenRouter", …
    models: list[str] = field(default_factory=list)
    needs_key: bool = False
    error: str = ""
    reason: str = ""  # dns, refused, unreachable, invalid, http
    name: str = ""  # "this computer", or the network name of its IP address

    @property
    def answered(self) -> bool:
        """True when a server answered like a model server; a key request counts."""
        return not self.error

    @property
    def usable(self) -> bool:
        """It answered as a model server (possibly asking for a key)."""
        return self.answered

    @property
    def ok(self) -> bool:
        return self.answered and not self.needs_key

    @property
    def label(self) -> str:
        return f"{self.kind} on {self.name}" if self.name else self.kind

    @property
    def summary(self) -> str:
        if self.needs_key:
            return f"{self.kind} · needs an API key"
        count = len(self.models)
        return f"{self.kind} · {count} model{'' if count == 1 else 's'}"


# ------------------------------------------------------------------ addresses


def tidy(text: str) -> str:
    """An address as typed, with an IPv4 address's commas read as dots: numeric
    keypads on German and other keyboards type "," for the decimal key."""
    text = str(text or "").strip()
    return _IPV4_TYPED.sub(lambda match: ".".join(match.groups()), text) if "," in text else text


def normalize_base(url: str) -> str:
    """Turn what a user types into the ``.../v1`` base URL Kova stores."""
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if url.endswith("/models"):
        url = url[: -len("/models")].rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def expand(text: str) -> list[str]:
    """The server base URLs a typed address could mean, the likeliest first."""
    text = tidy(text).rstrip("/")
    if not text:
        raise ValueError(
            "Enter an address such as 192.168.1.50, homelab:8080 or http://10.0.0.5:8080/v1"
        )
    lowered = text.lower()
    if lowered in ALIASES:
        return [ALIASES[lowered]]
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return [normalize_base(text)]
    host_port, _, path = text.partition("/")
    bracketed = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::(\d{1,5}))?", host_port)
    if bracketed:
        host, port = bracketed.group(1), bracketed.group(2)
    elif host_port.count(":") > 1:
        host, port = host_port, None  # a bare IPv6 address
    else:
        host, _, port = host_port.partition(":")
        port = port or None
    if not host or (port is not None and not port.isdigit()):
        raise ValueError(f"{text!r} doesn't look like a server address")
    host = f"[{host}]" if ":" in host else host
    number = int(port) if port else None
    scheme = "https" if number == 443 else "http"
    origin = f"{scheme}://{host}" + (f":{number}" if number else "")
    if path:
        return [f"{origin}/{path}".rstrip("/")]
    if number is None:
        # No port given: try where the usual servers listen, likeliest first.
        return [f"http://{host}:{port}/v1" for port in (OLLAMA_PORT, 8080, 1234, 8000)]
    return [f"{origin}/v1", origin]


def host_port(url: str) -> str:
    """host:port, the way a server is named when it has no other name."""
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/]+)", str(url))
    return match.group(1) if match else str(url)


# ------------------------------------------------------------------ probing


def _model_ids(payload: object) -> list[str]:
    """Pull model ids out of an OpenAI-style or Ollama-style listing."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("data", payload.get("models"))
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
            if value:
                ids.append(str(value))
    return ids


def _kind(base_url: str) -> str:
    """What runs on the other end, as far as its port or host says."""
    try:
        parsed = httpx.URL(base_url)
        host = (parsed.host or "").lower()
        if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
            return "OpenRouter"
        if parsed.port in PORT_KINDS:
            return PORT_KINDS[parsed.port]
    except Exception:
        pass
    return "OpenAI-compatible"


def _connect_reason(exc: httpx.ConnectError) -> str:
    text = str(exc).lower()
    if any(
        part in text
        for part in (
            "name or service not known",
            "name resolution",
            "nodename nor servname",
            "no address associated",
        )
    ):
        return "dns"
    if "refused" in text:
        return "refused"
    return "unreachable"


def _connect_error(exc: httpx.ConnectError) -> str:
    reason = _connect_reason(exc)
    if reason == "dns":
        return "no computer has that name"
    if reason == "refused":
        return "nothing is listening there"
    return "no route to that computer"


def probe(
    url: str,
    *,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> Found:
    """Ask one URL whether it is a model server, and which models it has.

    A 401 still counts as found: the server is there, it just wants a key.
    """
    url = url.rstrip("/")
    kind = _kind(url)
    try:
        host = httpx.URL(url).host or ""
    except Exception:
        return Found(url, kind=kind, error="not a server address", reason="invalid")
    if not host or any(character.isspace() for character in host):
        return Found(url, kind=kind, error="not a server address", reason="invalid")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 1.5)),
            headers=headers,
        ) as client:
            response = client.get(f"{url}/models")
    except httpx.ConnectError as exc:
        return Found(url, kind=kind, error=_connect_error(exc), reason=_connect_reason(exc))
    except httpx.TimeoutException:
        return Found(url, kind=kind, error="no answer", reason="unreachable")
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        return Found(url, kind=kind, error=str(exc) or type(exc).__name__, reason="http")
    if response.status_code in (401, 403):
        return Found(url, kind=kind, needs_key=True)
    if response.status_code != 200:
        return Found(url, kind=kind, error=f"HTTP {response.status_code}", reason="http")
    try:
        payload = response.json()
    except ValueError:
        return Found(url, kind=kind, error="not a model server", reason="http")
    return Found(url, kind=kind, models=_model_ids(payload))


def connect(
    text: str,
    *,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> Found:
    """The first of the URLs a typed address could mean that answers.

    A server that answers but wants a key is returned with ``needs_key`` set.
    When nothing answers, the returned :class:`Found` carries one actionable
    sentence in ``error``.
    """
    candidates = expand(text)
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        results = list(
            pool.map(
                lambda url: probe(url, api_key=api_key, timeout=timeout, transport=transport),
                candidates,
            )
        )
    for wanted in (lambda found: found.ok, lambda found: found.needs_key):
        for result in results:
            if wanted(result):
                result.name = host_name(result.base_url)
                return result
    return Found(
        candidates[0],
        kind=_kind(candidates[0]),
        error=explain(text, candidates, results),
        reason=results[0].reason,
    )


OLLAMA_OPEN_UP = (
    "If it runs Ollama, let other computers in: on that machine run "
    "`sudo systemctl edit ollama`, add `[Service]` and "
    '`Environment="OLLAMA_HOST=0.0.0.0"`, then `sudo systemctl restart ollama`.'
)


def explain(text: str, candidates: list[str], results: list[Found]) -> str:
    """Why nothing answered, in a sentence someone can act on."""
    where = host_port(candidates[0])
    host = where if where.endswith("]") else where.rsplit(":", 1)[0]
    reasons = {result.reason for result in results}
    if reasons == {"invalid"}:
        return (
            f"“{text.strip()}” isn't a server address. Try an IP such as 192.168.1.50, "
            "a name such as homelab:8080, or a URL such as http://10.0.0.5:8080/v1."
        )
    if reasons == {"dns"}:
        return f"No computer on your network is called {host}."
    if reasons <= {"unreachable", "invalid"}:
        return (
            f"{host} didn't answer. Check that it is switched on and on this network, "
            "and that its firewall allows the port (11434 for Ollama)."
        )
    ollama = next((result for result in results if _port_of(result.base_url) == OLLAMA_PORT), None)
    here = host.strip("[]") in ("127.0.0.1", "localhost", "::1")
    if ollama is not None and ollama.reason == "refused" and here:
        return (
            f"Nothing is listening on port {OLLAMA_PORT} on this computer. Start Ollama "
            "with `ollama serve` or `sudo systemctl start ollama`."
        )
    if ollama is not None and ollama.reason == "refused":
        return (
            f"{host} is on the network, but nothing is listening on port {OLLAMA_PORT}. "
            + OLLAMA_OPEN_UP
        )
    if reasons <= {"refused", "unreachable", "invalid"}:
        if here:
            return f"Nothing is listening on {where} on this computer."
        return f"{host} is on the network, but nothing is listening on {where}."
    tried = "; ".join(f"{host_port(result.base_url)}: {result.error}" for result in results[:4])
    return f"No model server answered ({tried})."


def _port_of(url: str) -> int | None:
    try:
        return httpx.URL(url).port
    except Exception:
        return None


def _host_of(url: str) -> str:
    try:
        return httpx.URL(url).host or ""
    except Exception:
        return ""


def host_name(url: str, timeout: float = 0.8) -> str:
    """A friendly name for a server: "this computer", or the reverse-DNS name
    for an IP address when the network knows one quickly ("" otherwise)."""
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\[?([^\]/]+?)\]?(?::\d+)?(?:/|$)", str(url))
    host = match.group(1) if match else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""  # it already has a name
    if address.is_loopback:
        return "this computer"
    found: list[str] = []

    def look_up() -> None:
        with contextlib.suppress(OSError):
            found.append(socket.gethostbyaddr(host)[0])

    worker = threading.Thread(target=look_up, daemon=True)
    worker.start()
    worker.join(timeout)
    name = found[0].rstrip(".") if found else ""
    if name.endswith((".lan", ".local", ".home", ".home.arpa", ".internal", ".localdomain", ".fritz.box", ".speedport.ip")):
        name = name.split(".", 1)[0]
    return name[:32]


# ------------------------------------------------------------------ the local network


def local_addresses() -> list[ipaddress.IPv4Interface]:
    """This computer's private IPv4 addresses on real network interfaces."""
    found: list[ipaddress.IPv4Interface] = []
    try:
        import fcntl
        import struct

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as query:
            for _index, name in socket.if_nameindex():
                if name.startswith(_SKIP_INTERFACES):
                    continue
                request = struct.pack("256s", name[:15].encode())
                try:
                    address = fcntl.ioctl(query.fileno(), 0x8915, request)[20:24]  # SIOCGIFADDR
                    mask = fcntl.ioctl(query.fileno(), 0x891B, request)[20:24]  # SIOCGIFNETMASK
                except OSError:
                    continue  # no IPv4 address on this interface
                found.append(
                    ipaddress.IPv4Interface(f"{socket.inet_ntoa(address)}/{socket.inet_ntoa(mask)}")
                )
    except (ImportError, OSError, ValueError):
        pass
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(("10.254.254.254", 1))  # picks a route; sends nothing
                found.append(ipaddress.IPv4Interface(route.getsockname()[0] + "/24"))
        except OSError:
            pass
    return [
        item for item in found if item.ip.is_private and not item.ip.is_loopback and not item.ip.is_link_local
    ]


def local_networks(limit: int = 3) -> list[ipaddress.IPv4Network]:
    """The /24 around each local address (a /16 home network is not swept whole)."""
    networks: list[ipaddress.IPv4Network] = []
    for interface in local_addresses():
        network = ipaddress.ip_network(f"{interface.ip}/24", strict=False)
        if network not in networks:
            networks.append(network)
    return networks[:limit]


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover(
    cancel: threading.Event | None = None,
    *,
    networks: list | None = None,
    ports: list[int] | None = None,
    connect_timeout: float = SCAN_CONNECT_TIMEOUT,
    probe_timeout: float = SCAN_PROBE_TIMEOUT,
    workers: int = 192,
    transport: httpx.BaseTransport | None = None,
) -> list[Found]:
    """Model servers on this computer and the local network, closest first.

    This only ever runs when the user asks for it. Each listening port on the
    usual server ports is asked whether it is a model server; an injected
    ``transport`` (tests) stands in for the network, so no TCP pre-scan runs.
    """
    ports = list(ports or SCAN_PORTS)
    if networks is None:
        networks = local_networks()
    else:
        networks = [ipaddress.ip_network(item) if isinstance(item, str) else item for item in networks]
    own = {str(item.ip) for item in local_addresses()}
    hosts = ["127.0.0.1"] + [
        str(host) for network in networks for host in network.hosts() if str(host) not in own
    ]
    targets = [(host, port) for host in hosts for port in ports]
    if transport is not None:
        listening = targets
    else:
        listening = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_open, host, port, connect_timeout): (host, port) for host, port in targets}
            for future in as_completed(futures):
                if cancel is not None and cancel.is_set():
                    pool.shutdown(cancel_futures=True)
                    return []
                if future.result():
                    listening.append(futures[future])

    def identify(target: tuple[str, int]) -> Found | None:
        host, port = target
        for url in expand(f"{host}:{port}"):
            found = probe(url, timeout=probe_timeout, transport=transport)
            if found.usable:
                found.name = host_name(found.base_url)
                return found
        return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        found = [result for result in pool.map(identify, listening) if result is not None]
    order = {host: index for index, host in enumerate(hosts)}
    found.sort(key=lambda item: (order.get(_host_of(item.base_url), len(order)), item.base_url))
    return found


__all__ = [
    "ALIASES",
    "OLLAMA_OPEN_UP",
    "OLLAMA_PORT",
    "OPENROUTER_URL",
    "PORT_KINDS",
    "SCAN_PORTS",
    "Found",
    "connect",
    "discover",
    "expand",
    "explain",
    "host_name",
    "host_port",
    "local_addresses",
    "local_networks",
    "normalize_base",
    "probe",
    "tidy",
]
