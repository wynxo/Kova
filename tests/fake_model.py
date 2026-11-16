"""Scripted OpenAI-compatible model responses for tests."""

import json

import httpx


class FakeModel:
    def __init__(self, scripts: list[list[dict]], usage: list[dict | None] | None = None):
        self.scripts = scripts
        # Optional per-request usage blocks, sent as a final frame with no
        # choices, the way OpenAI-compatible servers answer include_usage.
        self.usage = usage or []
        self.requests: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        self.requests.append(json.loads(request.content))
        script = self.scripts[len(self.requests) - 1]
        lines = [f'data: {json.dumps({"choices": [{"delta": delta}]})}\n\n' for delta in script]
        index = len(self.requests) - 1
        if index < len(self.usage) and self.usage[index] is not None:
            lines.append(f'data: {json.dumps({"choices": [], "usage": self.usage[index]})}\n\n')
        return httpx.Response(200, text="".join(lines) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})


def text(s: str) -> list[dict]:
    return [{"content": s[i:i + 3]} for i in range(0, len(s), 3)]


def tool(name: str, args: dict, id: str = "call_1", chunk_size: int | None = None) -> list[dict]:
    arguments = json.dumps(args)
    if chunk_size is not None:
        return [{"tool_calls": [{"index": 0, **({"id": id} if i == 0 else {}),
                                 "function": {**({"name": name} if i == 0 else {}),
                                              "arguments": arguments[i:i + chunk_size]}}]}
                for i in range(0, len(arguments), chunk_size)]
    middle = len(arguments) // 2
    return [
        {"tool_calls": [{"index": 0, "id": id, "type": "function", "function": {"name": name, "arguments": arguments[:middle]}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": arguments[middle:]}}]},
    ]
