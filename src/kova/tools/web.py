"""Web search and bounded page fetching."""

from __future__ import annotations

import ipaddress
import json
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from kova import egress

from . import Tool, ToolError

_transport: httpx.BaseTransport | None = None


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current = None
        self.capture = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'a' and 'result__a' in classes:
            href = urljoin('https://html.duckduckgo.com', attrs.get('href', ''))
            target = urlsplit(href)
            if target.hostname and target.hostname.endswith('duckduckgo.com'):
                href = parse_qs(target.query).get('uddg', [href])[0]
            self.current = {'title': '', 'url': href, 'snippet': ''}
            self.results.append(self.current)
            self.capture, self.depth = 'title', 1
        elif self.capture:
            self.depth += 1
        elif 'result__snippet' in classes and self.current is not None:
            self.capture, self.depth = 'snippet', 1

    def handle_endtag(self, tag):
        if self.capture:
            self.depth -= 1
            if self.depth == 0:
                self.capture = None

    def handle_data(self, data):
        if self.capture and self.current is not None:
            self.current[self.capture] += data


@egress.attributed('search')
def search(query: str) -> list[dict]:
    """Fetch up to five DuckDuckGo HTML results through the audited HTTP client."""
    url = 'https://html.duckduckgo.com/html/'
    egress.check_url(url)
    with egress.Client(transport=_transport, timeout=15) as client:
        with client.stream('GET', url, params={'q': query}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk[:1024 * 1024 - len(data)])
                if len(data) >= 1024 * 1024:
                    break
    parser = _SearchParser()
    parser.feed(data.decode('utf-8', errors='replace'))
    return [{key: ' '.join(value.split()) for key, value in row.items()}
            for row in parser.results[:5]]


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _resolve(host: str) -> list[str]:
    """Every address a host name resolves to (module-level so tests can replace it)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _check_public(url: str) -> str:
    """Refuse non-http(s) URLs and hosts on this machine or the local network.

    A web page could otherwise trick the model into reading the user's own
    services (router, homelab, Kova's API) and leaking them.
    """
    egress.check_url(url)
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ToolError("Only HTTP and HTTPS URLs are supported")
    host = parts.hostname or ""
    try:
        addrs = _resolve(host)
    except OSError as exc:
        raise ToolError(f"Can't resolve {host}") from exc
    if not addrs:
        raise ToolError(f"Can't resolve {host}")
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise ToolError("Kova only fetches public web pages, not local or private addresses")

    return addrs[0]

@egress.attributed('fetch')
def _fetch(args: dict) -> str:
    url = args["url"]
    egress.check_url(url)
    with egress.Client(transport=_transport, timeout=15, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):  # follow up to 5 redirects, checking each hop
            address = _check_public(url)
            request = client.build_request("GET", url)
            # Keep HTTP authority and TLS identity, but never resolve the name
            # again at connection time. Environment proxies are disabled above.
            hostname = request.url.host
            request.url = request.url.copy_with(host=address)
            request.extensions["sni_hostname"] = hostname
            request.extensions["egress_host"] = hostname
            request.extensions["egress_address"] = address
            response = client.send(request, stream=True)
            if response.is_redirect and "location" in response.headers:
                response.close()
                url = urljoin(url, response.headers["location"])
                continue
            break
        else:
            raise ToolError("Too many redirects")
        try:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk[: 1024 * 1024 - len(data)])
                if len(data) >= 1024 * 1024:
                    break
            encoding = response.encoding or "utf-8"
        finally:
            response.close()
    parser = _TextParser()
    parser.feed(bytes(data).decode(encoding, errors="replace"))
    return " ".join(" ".join(parser.parts).split())[:8000]


def tools() -> list[Tool]:
    """Create the web tools."""
    return [
        Tool("web_search", "Search the web for current information. Returns up to 5 results with title, url and snippet.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, lambda args: json.dumps(search(args["query"]))),
        Tool("web_fetch", "Download a web page (http/https) and return its readable text, up to 8000 characters. Use after web_search to read a result.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, _fetch),
    ]
