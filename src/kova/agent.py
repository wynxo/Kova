"""Run one model-assisted chat turn."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import httpx

from kova.config import Config
from kova.memory import Memory, Note
from kova.models import ModelClient, ModelError
from kova.store import Store
from kova.tools import Tool, openai_schemas


_IDENTITY = """You are Kova, a private AI that runs on the user's own computer. Everything stays on their machine.
Be warm, direct and genuinely helpful. Use tools when they help: search the web for current facts, work with files in the Kova files folder, and set reminders.
When you learn a lasting fact about the user (name, preferences, projects, people), save it with memory_save.
Never say you are a different assistant or made by another company. You are Kova."""


def build_system_prompt(now: datetime, tool_names: list[str], memories: list[Note]) -> str:
    """Build the identity, current context, and relevant memory prompt."""
    prompt = f"{_IDENTITY}\nCurrent time: {now.isoformat()}\nAvailable tools: {', '.join(tool_names)}"
    if memories:
        notes = "\n\n".join(f"## {note.title}\n{note.body}" for note in memories)
        prompt += f"\n\nWhat you remember about the user:\n{notes}"
    return prompt


MAX_TOOL_RESULT_CHARS = 32_000


def derive_title(text: str, limit: int = 40) -> str:
    """A short chat title from the first user message, cut at a word boundary."""
    words = text.split()
    title = ""
    for word in words:
        candidate = f"{title} {word}".strip()
        if len(candidate) > limit:
            break
        title = candidate
    if not title:
        title = (words[0] if words else "New chat")[:limit]
    return title


def repair_tool_pairs(messages: list[dict]) -> list[dict]:
    """Give every assistant tool call a tool reply, so a turn that was cut off
    (crash, disconnect) can't make the model server reject the next request."""
    out: list[dict] = []
    for i, msg in enumerate(messages):
        out.append(msg)
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        answered = set()
        for later in messages[i + 1:]:
            if later.get("role") != "tool":
                break
            answered.add(later.get("tool_call_id"))
        missing = [c["id"] for c in msg["tool_calls"] if c["id"] not in answered]
        # Insert the stand-ins right after the existing replies of this call batch.
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            j += 1
        out_tail = [{"role": "tool", "tool_call_id": cid, "content": "Error: interrupted"} for cid in missing]
        if out_tail:
            out.extend(messages[i + 1:j])
            out.extend(out_tail)
            return out + repair_tool_pairs(messages[j:])
    return out


def trim_history(messages: list[dict], context_tokens: int, reserved_chars: int) -> list[dict]:
    """Keep recent history within the budget, retaining the latest user message."""
    budget = context_tokens * 4 * 0.75 - reserved_chars
    latest_user = next((i for i in range(len(messages) - 1, -1, -1)
                        if messages[i].get('role') == 'user'), None)
    start = 0
    size = sum(len(m.get('content') or '') for m in messages)
    while start < len(messages) and size > budget and start != latest_user:
        size -= len(messages[start].get('content') or '')
        start += 1
    while start < len(messages) and messages[start].get('role') == 'tool':
        start += 1
    return messages[start:]


class Agent:
    """Coordinate model streaming, tools, and persistent chat history."""

    def __init__(self, config: Config, store: Store, memory: Memory,
                 tools: dict[str, Tool], transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.config = config
        self.store = store
        self.memory = memory
        self.tools = tools
        self.transport = transport
        self.now = now

    async def run_turn(self, chat_id: str, text: str) -> AsyncIterator[dict]:
        """Persist and stream a turn, executing at most ten rounds of tools."""
        self.store.add_message(chat_id, 'user', text)
        chat = self.store.get_chat(chat_id)
        needs_title = chat is not None and not chat['title']
        title_source = text
        if chat is not None:
            for message in chat['messages']:
                if message.get('role') == 'user' and message.get('content'):
                    title_source = message['content']
                    break
        try:
            memories = await asyncio.to_thread(self.memory.search, text, 5)
        except Exception:  # memory trouble must never block a chat
            memories = []
        prompt = build_system_prompt(self.now(), list(self.tools), memories)
        entry = self.config.active_model()
        client = ModelClient(entry, transport=self.transport)
        schemas = openai_schemas(self.tools)
        try:
            for step in range(11):
                history = trim_history(repair_tool_pairs(self.store.get_messages(chat_id)),
                                       entry.context, len(prompt))
                messages = [{'role': 'system', 'content': prompt}, *history]
                content: list[str] = []
                calls: list[dict] = []
                async for event in client.stream(messages, schemas if step < 10 else None):
                    if event.type in ('text', 'reasoning'):
                        if event.type == 'text':
                            content.append(event.text)
                        yield {'type': event.type, 'text': event.text}
                    elif event.type == 'tool_call' and event.tool_call is not None and step < 10:
                        calls.append(event.tool_call)
                answer = ''.join(content)
                if not calls:
                    message_id = self.store.add_message(chat_id, 'assistant', answer)
                    if needs_title:
                        current = self.store.get_chat(chat_id)
                        if current is not None and not current['title']:
                            self.store.set_title(chat_id, derive_title(title_source))
                    yield {'type': 'done', 'message_id': message_id}
                    return
                formatted = [{'id': call['id'], 'type': 'function', 'function': {
                    'name': call['name'], 'arguments': call['arguments']}} for call in calls]
                self.store.add_message(chat_id, 'assistant', answer or None, tool_calls=formatted)
                for call in calls:
                    name = call['name']
                    try:
                        args = json.loads(call['arguments'])
                    except (json.JSONDecodeError, TypeError):
                        args = None
                        result, ok = 'Error: arguments were not valid JSON', False
                    else:
                        result, ok = '', True
                    yield {'type': 'tool_call', 'name': name, 'args': args}
                    if ok:
                        if name not in self.tools:
                            result, ok = f'Error: unknown tool {name}', False
                        else:
                            try:
                                result = await asyncio.to_thread(self.tools[name].run, args)
                                if inspect.isawaitable(result):
                                    result = await result
                            except Exception as exc:
                                result, ok = f'Error: {exc}', False
                    result = str(result)
                    if len(result) > MAX_TOOL_RESULT_CHARS:
                        result = result[:MAX_TOOL_RESULT_CHARS] + '\n[truncated]'
                    self.store.add_message(chat_id, 'tool', result, tool_call_id=call['id'])
                    yield {'type': 'tool_result', 'name': name, 'ok': ok, 'summary': result[:200]}
        except ModelError as exc:
            yield {'type': 'error', 'message': str(exc)}
