"""Web search and bounded page fetching."""

from __future__ import annotations

import ipaddress
import json
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from ddgs import DDGS

from . import Tool, ToolError

_transport: httpx.BaseTransport | None = None


def search(query: str) -> list[dict]:
    """Return up to five DuckDuckGo results."""
    results = DDGS(timeout=15).text(query, max_results=5)
    return [{"title": r.get("title", ""), "url": r.get("href", ""),
             "snippet": r.get("body", "")} for r in results][:5]


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


def _check_public(url: str) -> None:
    """Refuse non-http(s) URLs and hosts on this machine or the local network.

    A web page could otherwise trick the model into reading the user's own
    services (router, homelab, Kova's API) and leaking them.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ToolError("Only HTTP and HTTPS URLs are supported")
    host = parts.hostname or ""
    try:
        addrs = _resolve(host)
    except OSError as exc:
        raise ToolError(f"Can't resolve {host}") from exc
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise ToolError("Kova only fetches public web pages, not local or private addresses")


def _fetch(args: dict) -> str:
    url = args["url"]
    with httpx.Client(transport=_transport, timeout=15, follow_redirects=False) as client:
        for _ in range(6):  # follow up to 5 redirects, checking each hop
            _check_public(url)
            response = client.send(client.build_request("GET", url), stream=True)
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
