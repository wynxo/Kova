"""Scripted OpenAI-compatible model responses for tests."""

import json

import httpx


class FakeModel:
    def __init__(self, scripts: list[list[dict]]):
        self.scripts = scripts
        self.requests: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        self.requests.append(json.loads(request.content))
        script = self.scripts[len(self.requests) - 1]
        lines = [f'data: {json.dumps({"choices": [{"delta": delta}]})}\n\n' for delta in script]
        return httpx.Response(200, text="".join(lines) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})


def text(s: str) -> list[dict]:
    return [{"content": s[i:i + 3]} for i in range(0, len(s), 3)]


def tool(name: str, args: dict, id: str = "call_1") -> list[dict]:
    arguments = json.dumps(args)
    middle = len(arguments) // 2
    return [
        {"tool_calls": [{"index": 0, "id": id, "type": "function", "function": {"name": name, "arguments": arguments[:middle]}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": arguments[middle:]}}]},
    ]
