"""Run one model-assisted chat turn."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import unicodedata
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path

import httpx

from kova import providers, sandbox
from kova.checkpoints import BlobStore, ChangeTracker
from kova.compact import compact
from kova.config import Config, load_config
from kova.persona import Persona
from kova.memory import Memory, Note
from kova.models import ModelClient, ModelError
from kova.modes import DEFAULT_MODE, capabilities as mode_capabilities
from kova.store import Store
from kova.tools import Tool, ToolError, openai_schemas
from kova.tools.core import CORE_TOOLS, FILE_TOOLS, FileSession, validate
from kova.tools import core
from kova.tools.sessions import set_current_chat
from kova.permissions import Approvals, needs_approval, action_key
from kova.project_instructions import project_instructions
from kova.review import MemoryReview, ReviewSettings
from kova.skills import Skills


_IDENTITIES = {
    "work": """You are Kova, a private AI that runs on the user's own computer. Everything stays on their machine.
Be warm, direct and genuinely helpful. Use tools when they help: search the web for current facts, work with code and files in the working folder, run commands, and set reminders.
Use ask_user when clarification is needed. Use update_plan for genuine multi-step work; keep the checklist updated as you progress.
Never retry a denied action without new user authorization. Commands can access the system as the current user; file tools stay inside the working folder.
When you learn a lasting fact about the user (name, preferences, projects, people), save it with memory_save.
Keep a short note titled "index" listing who and what you remember (one line per topic); put the detail in separate topic notes, because the index is always loaded and topics are only read when they matter.
Never say you are a different assistant or made by another company. You are Kova.""",
    "chat": """You are Kova, a private AI that runs on the user's own computer. Everything stays on their machine.
Be warm, direct and genuinely helpful. Keep replies short and conversational.
You are in chat mode: a plain conversation. You cannot read or write files, set reminders, or save memories, so never claim to have done anything like that. You can only search and read the web.
If a task needs more than that, say so and suggest switching to work mode with /mode work.
Never say you are a different assistant or made by another company. You are Kova.""",
}

_WEB_TOOLS = ("web_search", "web_fetch")


def select_tools(mode: str, web: bool, tools: dict[str, Tool]) -> dict[str, Tool]:
    """The tools a chat may use: everything when the mode offers tools, web only otherwise.

    The web toggle applies to both modes; a future code mode declares its own
    capabilities in :mod:`kova.modes` instead of adding a branch here.
    """
    allowed = set(tools) if "tools" in mode_capabilities(mode) else set(_WEB_TOOLS)
    if not web:
        allowed -= set(_WEB_TOOLS)
    return {name: tool for name, tool in tools.items() if name in allowed}


def build_system_prompt(now: datetime, tool_names: list[str], memories: list[Note],
                        mode: str = DEFAULT_MODE, *,
                        memory_usage: int | None = None,
                        memory_budget: int | None = None,
                        persona: Persona | None = None,
                        index: str | None = None) -> str:
    """Build the identity, current context, and relevant memory prompt.

    ``memories`` are the topic notes recall picked for this message; ``index``
    is the small index note that is always loaded, so the model knows what
    exists even when nothing matched.
    """
    persona = persona or Persona()
    identity = _IDENTITIES.get(mode, _IDENTITIES[DEFAULT_MODE])
    identity = identity.replace("Kova", persona.name).replace(
        "Be warm, direct and genuinely helpful.", persona.instruction)
    tools = ", ".join(tool_names) if tool_names else "none"
    prompt = (f"{identity}\nCurrent time: {now.isoformat()}\nCurrent mode: {mode}\n"
              f"Available tools: {tools}")
    if memories or memory_budget is not None:
        if memory_budget:
            used = memory_usage or 0
            percent = round(used * 100 / memory_budget)
            header = (f"What you remember about the user (memory {used:,} of "
                      f"{memory_budget:,} chars used, {percent}% full; over-budget "
                      "writes fail, so patch or remove notes to free space):")
        elif memory_budget == 0:
            header = "What you remember about the user (memory size is unlimited):"
        else:
            header = "What you remember about the user:"
        if memories:
            notes = "\n\n".join(f"## {note.title}\n{note.body}" for note in memories)
            prompt += f"\n\n{header}\n{notes}"
        else:
            prompt += f"\n\n{header} nothing yet."
    if index:
        prompt += ("\n\nMemory index (always loaded; open a topic note or run memory_search "
                   f"when you need the detail):\n{index}")
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
        title = _cut_graphemes(words[0], limit) if words else "New chat"
    return title


def _cut_graphemes(text: str, limit: int) -> str:
    """Cut to ``limit`` code points without splitting an emoji sequence or an
    accent from its letter (backs off to the start of that cluster)."""
    if len(text) <= limit:
        return text
    i = limit
    while i > 0 and (text[i - 1] == "\u200d" or text[i] in "\u200d\ufe0f"
                     or unicodedata.combining(text[i])):
        i -= 1
    return text[:i] or text[:limit]


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


def _message_chars(message: dict) -> int:
    """What a message costs in the request: its text plus any tool-call arguments."""
    size = len(message.get('content') or '')
    for call in message.get('tool_calls') or []:
        function = call.get('function') or {}
        size += len(function.get('name') or '') + len(function.get('arguments') or '')
    return size


def trim_history(messages: list[dict], context_tokens: int, reserved_chars: int) -> list[dict]:
    """Keep recent history within the budget, retaining the latest user message."""
    budget = context_tokens * 4 * 0.75 - reserved_chars
    latest_user = next((i for i in range(len(messages) - 1, -1, -1)
                        if messages[i].get('role') == 'user'), None)
    start = 0
    sizes = [_message_chars(m) for m in messages]
    size = sum(sizes)
    while start < len(messages) and size > budget and start != latest_user:
        size -= sizes[start]
        start += 1
    while start < len(messages) and messages[start].get('role') == 'tool':
        start += 1
    return messages[start:]


class Steering:
    """Messages that join a turn that is already running.

    The server owns one per live turn. The agent drains it between model
    rounds, persists each message, and asks the model again instead of ending,
    so a steer changes the work in progress rather than waiting behind it.
    """

    def __init__(self) -> None:
        self._pending: list[str] = []

    def add(self, text: str) -> None:
        self._pending.append(text)

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def take(self) -> list[str]:
        messages, self._pending = self._pending, []
        return messages


def call_fingerprint(name: str, arguments: str) -> str:
    """A stable fingerprint of a tool call: its name and arguments.

    Two calls with the same name and arguments hash to the same value, which the
    agent loop uses to spot a model that keeps reissuing one failing call.
    """
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        parsed = arguments
    payload = f"{name}\x00{json.dumps(parsed, sort_keys=True, default=str)}".encode()
    return hashlib.sha256(payload).hexdigest()


class Agent:
    """Coordinate model streaming, tools, and persistent chat history."""

    def __init__(self, config: Config, store: Store, memory: Memory,
                 tools: dict[str, Tool], transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.config = config
        self.store = store
        self.memory = memory
        self.skills = Skills.for_home(config.home)
        self.tools = tools
        self.transport = transport
        self.now = now
        self.approvals = Approvals(path=config.home / "permission-rules.json")
        self.file_sessions: dict[tuple[str, str], FileSession] = {}
        # Background memory maintenance is opt-in (memory.review / memory.consolidate).
        settings = ReviewSettings.from_config(config)
        self.memory_review = MemoryReview(store, settings) if settings.enabled else None

        # File contents for per-turn checkpoints, outside every working folder.
        self.blobs = BlobStore(config.home / "checkpoints")

    async def _review(self, chat_id: str, cwd: Path, tracker: ChangeTracker | None,
                      label: str) -> dict | None:
        """Record the files this turn changed and describe them for review.

        A checkpoint never fails a turn: any problem recording one is dropped
        and the turn ends normally.
        """
        if tracker is None or not tracker.touched:
            return None
        try:
            files, skipped = await asyncio.to_thread(tracker.finish)
            if not files:
                return {'type': 'review', 'checkpoint_id': None, 'files': [], 'skipped': skipped} \
                    if skipped else None
            checkpoint_id = await asyncio.to_thread(self.store.add_checkpoint,
                                                    chat_id, str(cwd), files, label)
        except Exception:
            return None
        return {'type': 'review', 'checkpoint_id': checkpoint_id, 'root': str(cwd), 'label': label,
                'files': [{'path': path, 'status': entry['status'],
                           'additions': entry['additions'], 'deletions': entry['deletions']}
                          for path, entry in sorted(files.items())],
                'skipped': skipped}

    def _turn_context(self, chat_id: str, user_message_id: int, model_text: str,
                      attached: list[dict]) -> list[dict]:
        """Stored history with this turn's extracted attachment text applied.

        The stored user message stays exactly what the user wrote. For the
        model request only, the message this turn just wrote carries the
        extracted text, so tool rounds, retries and compaction still see it
        without the transcript ever containing it.
        """
        entries = self.store.get_context_entries(chat_id)
        if attached:
            for ident, message in entries:
                if ident == user_message_id:
                    message['content'] = model_text
                    break
        return [message for _, message in entries]

    async def run_turn(self, chat_id: str, text: str, model: str | None = None, *,
                       steering: Steering | None = None, attachment_ids: list[str] | None = None) -> AsyncIterator[dict]:
        """Persist and stream a turn, executing at most ten rounds of tools.

        ``model`` names a configured model for this turn only; the active one otherwise.
        ``steering`` carries messages that arrive while the turn runs.
        """
        from kova import attachments
        from kova.tools import Tool
        import base64
        rows = {r['id']: r for r in self.store.attachments(chat_id)}
        attached = [rows[i] for i in (attachment_ids or []) if i in rows]
        if len(attached) != len(attachment_ids or []):
            yield {'type': 'error', 'message': 'An attachment was removed; attach it again.'}
            return
        # Persist only what the user wrote. Extracted text is request context
        # for this turn: it must not reach the search index, exports, backups
        # or a generated title (see _turn_context below).
        model_text = text
        if attached:
            block = '\n\n'.join(attachments.block(r) for r in attached)
            model_text = f'{text}\n\n{block}' if text else block
        user_message_id = self.store.add_message(chat_id, 'user', text)
        if attached:
            # A saved message takes responsibility for its uploads: retain the
            # files even when the clean transcript no longer spells out an id.
            self.store.mark_attachments_used(chat_id, [row['id'] for row in attached])
            # Keep the file names attached to this message for reloaded chats.
            # The link carries no extracted text and is not part of model history.
            self.store.link_message_attachments(user_message_id, attached)
        set_current_chat(chat_id)
        chat = self.store.get_chat(chat_id)
        mode = (chat or {}).get('mode') or DEFAULT_MODE
        web = (chat or {}).get('web', True)
        cwd = Path((chat or {}).get('cwd') or self.config.files_path).expanduser().resolve()
        permission = (chat or {}).get('permission', 'manual')
        # Full access is the user's explicit choice to run unconfined; every
        # other mode runs commands inside the OS sandbox when the kernel
        # supports one, and asks when it does not.
        confined = permission != 'full' and sandbox.available()
        tools = select_tools(mode, web, self.tools)
        if rows:
            def read_attachment(args):
                row = rows.get(args.get('id'))
                if row is None:
                    return 'Attachment not found in this chat.'
                try:
                    return attachments.read_text(Path(row['path']), int(args.get('start', 1)), args.get('end'), row=row)
                except (attachments.AttachmentError, OSError, ValueError):
                    return 'This attachment could not be read.'
            tools['read_attachment'] = Tool('read_attachment', 'Read local attachment text by ID and optional line range.',
                {'type': 'object', 'properties': {'id': {'type': 'string'}, 'start': {'type': 'integer', 'minimum': 1}, 'end': {'type': 'integer', 'minimum': 1}}, 'required': ['id']}, read_attachment)
        files = None
        if mode != 'chat':
            if cwd == self.config.files_path.resolve():
                cwd.mkdir(parents=True, exist_ok=True)
            elif not cwd.is_dir():
                yield {'type': 'error', 'message': 'Working folder no longer exists. Choose an existing folder.'}
                return
            files = self.file_sessions.setdefault((chat_id, str(cwd)), FileSession(cwd))
            # Bind per-chat state, never mutate the server-wide registry.
            replacements = [*files.tools(), *core.tools(cwd, sandboxed=confined)]
            tools.update({tool.name: tool for tool in replacements if tool.name in tools})
        tracker = ChangeTracker(self.blobs, cwd) if files is not None else None
        needs_title = chat is not None and not chat['title']
        # A title comes from the user's words, or, for an attachment-only
        # turn, from the file names. Never from extracted document text.
        title_source = text.strip() or ', '.join(row['name'] for row in attached)
        if chat is not None:
            for message in chat['messages']:
                if message.get('role') == 'user' and message.get('content', '').strip():
                    title_source = message['content']
                    break
        try:
            memories = await asyncio.to_thread(self.memory.search, model_text, 5, mode="hybrid")
        except Exception:  # memory trouble must never block a chat
            memories = []
        try:
            memory_usage = await asyncio.to_thread(self.memory.usage)
        except Exception:  # a broken note must not block the prompt either
            memory_usage = 0
        # The index is always loaded; topic notes are only the ones recall picked.
        try:
            index = await asyncio.to_thread(self.memory.index_text,
                                            getattr(self.config, 'memory_index_lines', 200))
        except Exception:  # a broken index must not block the prompt either
            index = ""
        memories = [note for note in memories if note.slug != "index"]
        try:
            skills = await asyncio.to_thread(self.skills.prompt_block) if mode != 'chat' else ""
        except Exception:  # a broken skill file must not block the prompt
            skills = ""
        try:  # the identity is a nicety: trouble reading it must never block a chat
            current_config = load_config(self.config.home)
            persona = Persona(current_config.persona_name, current_config.persona_tone)
        except (OSError, ValueError):
            persona = Persona()
        prompt = build_system_prompt(self.now(), list(tools), memories, mode, persona=persona,
                                     memory_usage=memory_usage,
                                     memory_budget=getattr(self.memory, 'budget', 0),
                                     index=index or None)
        if mode != 'chat':
            prompt += f"\nWorking folder: {cwd}\nPermission mode: {permission}"
            instructions = await asyncio.to_thread(project_instructions, cwd,
                                                   user_home=self.config.home)
            if instructions:
                prompt += f"\n\n{instructions}"
            if skills:
                prompt += f"\n\n{skills}"
            if (chat or {}).get('plan'):
                prompt += "\nCurrent plan: " + json.dumps(chat['plan'])
        if rows:
            # Turn-only index: ids make read_attachment work on later turns
            # without placing attachment text or ids in the transcript.
            prompt += ("\n\nAttachments in this chat (read one with read_attachment, "
                       "using its id):\n" + attachments.index(rows.values()))
        self.config.bind_providers()
        if model is not None:
            entry = next((m for m in self.config.models if m.name == model), None)
        else:
            try:
                entry = self.config.active_model()
            except KeyError:
                # Removing the last provider empties models and active; that is a
                # setup state, not a crash (#92).
                entry = None
        if entry is None or not entry.model:
            yield {'type': 'error', 'message': "No model is set up yet. Run: kova setup"}
            return
        client = ModelClient(entry, transport=self.transport)
        # A provider that does not declare tool calling is never sent schemas,
        # so its model can neither call tools nor reach an approval.
        schemas = (openai_schemas(tools) or None) if providers.supports(entry._provider, "tools") else None
        try:
            repeat_count = 0
            repeat_fp = None
            repeat_error = None
            awaiting_replacement = False
            attempted_compaction = False
            for step in range(11):
                if steering is not None:
                    for message in steering.take():
                        # A steer joins the history at a round boundary, never
                        # between an assistant tool call and its tool reply.
                        self.store.add_message(chat_id, 'user', message)
                        yield {'type': 'steer', 'text': message}
                history = self._turn_context(chat_id, user_message_id, model_text, attached)
                threshold = max(0.1, min(float(self.config.compact_threshold), 1.0))
                size = len(prompt) + sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
                if not attempted_compaction and size > entry.context * 4 * threshold:
                    attempted_compaction = True
                    event = await compact(self.store, chat_id, client, context_tokens=entry.context,
                                          reserved_chars=len(prompt), threshold=threshold)
                    if event:
                        yield event
                        history = self._turn_context(chat_id, user_message_id, model_text, attached)
                history = trim_history(repair_tool_pairs(history), entry.context, len(prompt))
                messages = [{'role': 'system', 'content': prompt}, *history]
                if entry.vision and providers.supports(entry._provider, 'vision'):
                    for message in reversed(messages):
                        if message.get('role') == 'user' and message.get('content') == model_text:
                            parts = [{'type': 'text', 'text': model_text}]
                            for row in attached:
                                if row['mime'].startswith('image/'):
                                    data = base64.b64encode(Path(row['path']).read_bytes()).decode('ascii')
                                    parts.append({'type': 'image_url', 'image_url': {'url': f"data:{row['mime']};base64,{data}"}})
                            if len(parts) > 1:
                                message['content'] = parts
                            break

                content: list[str] = []
                calls: list[dict] = []
                async for event in client.stream(messages, schemas if step < 10 else None):
                    if event.type in ('text', 'reasoning'):
                        if event.type == 'text':
                            content.append(event.text)
                        yield {'type': event.type, 'text': event.text}
                    elif event.type == 'tool_call_delta' and event.tool_call is not None and step < 10:
                        yield {'type': 'tool_call_delta', **event.tool_call}
                    elif event.type == 'tool_call' and event.tool_call is not None and step < 10:
                        calls.append(event.tool_call)
                if awaiting_replacement:
                    awaiting_replacement = False
                    for call in calls:
                        if call_fingerprint(call['name'], call['arguments']) == repeat_fp:
                            review = await self._review(chat_id, cwd, tracker, derive_title(text, 60))
                            if review is not None:
                                yield review
                            yield {'type': 'error',
                                   'message': f"Tool '{call['name']}' kept failing after several attempts. "
                                              "Try a different approach or answer the user."}
                            return
                answer = ''.join(content)
                if not calls:
                    if steering is not None and steering.pending:
                        # The user steered while this answer streamed: keep the
                        # answer, then let the model respond to the steer.
                        self.store.add_message(chat_id, 'assistant', answer or None)
                        continue
                    message_id = self.store.add_message(chat_id, 'assistant', answer)
                    if needs_title:
                        current = self.store.get_chat(chat_id)
                        if current is not None and not current['title']:
                            self.store.set_title(chat_id, derive_title(title_source))
                    review = await self._review(chat_id, cwd, tracker, derive_title(text, 60))
                    if review is not None:
                        yield review
                    if self.memory_review is not None:
                        # Opt-in background passes run last so the answer is already
                        # stored; a failure there can never break the reply.
                        for notice in await self.memory_review.after_turn(
                                chat_id=chat_id, mode=mode, memory=self.memory,
                                client=client, now=self.now()):
                            yield notice
                    yield {'type': 'done', 'message_id': message_id, 'model': entry.name}
                    return
                formatted = [{**call.get('extra_fields', {}), 'id': call['id'], 'type': 'function', 'function': {
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
                    yield {'type': 'tool_call', 'id': call['id'], 'name': name, 'args': args}
                    if ok:
                        if name not in tools:
                            result, ok = f'Error: unknown tool {name}', False
                        else:
                            try:
                                if not isinstance(args, dict):
                                    raise ToolError('arguments must be an object')
                                if name in CORE_TOOLS:
                                    validate(tools[name].parameters, args)
                                if name == 'ask_user':
                                    question = self.approvals.create(chat_id, name, args, cwd=cwd)
                                    question['type'] = 'question'
                                    try:
                                        yield question
                                        decision = await self.approvals.wait(question['id'])
                                    finally:
                                        pending = self.approvals.pending.pop(question['id'], None)
                                        if pending and not pending.future.done():
                                            pending.future.cancel()
                                    yield {'type': 'question_resolved', 'id': question['id']}
                                    result = decision.get('message') or 'No answer provided.'
                                    self.store.add_message(chat_id, 'tool', result, tool_call_id=call['id'])
                                    yield {'type': 'tool_result', 'name': name, 'ok': decision['decision'] == 'allow', 'summary': result[:200], 'output': result}
                                    continue
                                prepared = None
                                if name in FILE_TOOLS:
                                    prepared = await asyncio.to_thread(files.prepare, name, args)
                                policy, reason = self.approvals.policy(chat_id, name, args, cwd)
                                if policy == 'deny':
                                    raise ToolError('Action denied by permission rule. ' + (reason or 'Do not retry.'))
                                if policy == 'ask' or (policy != 'allow' and needs_approval(
                                        name, args, permission, cwd, self.config.files_path,
                                        False, sandboxed=confined)):
                                    approval = self.approvals.create(chat_id, name, args, cwd=cwd)
                                    approved_binding = approval["binding"]
                                    try:
                                        yield approval
                                        decision = await self.approvals.wait(approval['id'])
                                    finally:
                                        # Also clean up if the async generator is closed at yield.
                                        pending = self.approvals.pending.pop(approval['id'], None)
                                        if pending and not pending.future.done():
                                            pending.future.cancel()
                                    yield {'type': 'approval_resolved', 'id': approval['id'], **decision}
                                    if action_key(name, args, cwd) != approved_binding:
                                        raise ToolError('Action changed after approval; request fresh authorization.')
                                    if decision['decision'] == 'deny':
                                        raise ToolError('Action denied. Do not retry without new authorization.' +
                                                        (f" User guidance: {decision['message']}" if decision.get('message') else ''))
                                if prepared is not None:
                                    await asyncio.to_thread(files.check, args, prepared)
                                    if tracker is not None:
                                        # Remember the file as it is now, before the write lands.
                                        await asyncio.to_thread(tracker.note, prepared[0])
                                if inspect.iscoroutinefunction(tools[name].run):
                                    result = await tools[name].run(args)
                                else:
                                    result = await asyncio.to_thread(tools[name].run, args)
                                    if inspect.isawaitable(result):
                                        result = await result
                                if name == 'update_plan':
                                    items = args['items']
                                    self.store.update_chat(chat_id, plan=items)
                                    yield {'type': 'plan', 'items': items}
                                if name == 'run_command':
                                    ok = json.loads(result)['ok']
                            except asyncio.CancelledError:
                                self.store.add_message(chat_id, 'tool', 'Error: Action denied or interrupted because the turn was stopped.', tool_call_id=call['id'])
                                raise
                            except Exception as exc:
                                result, ok = f'Error: {exc}', False
                    result = str(result)
                    full_output = result
                    if name != 'run_command' and len(result) > MAX_TOOL_RESULT_CHARS:
                        result = result[:MAX_TOOL_RESULT_CHARS] + '\n[truncated]'
                    triggered = False
                    if not ok:
                        fp = call_fingerprint(name, call['arguments'])
                        if fp == repeat_fp and result == repeat_error and repeat_count >= 1:
                            repeat_count += 1
                        else:
                            repeat_count, repeat_fp, repeat_error = 1, fp, result
                        if repeat_count >= 3:
                            awaiting_replacement = True
                            triggered = True
                    else:
                        repeat_count = 0
                        repeat_fp = None
                        repeat_error = None
                    if triggered:
                        warning = ("Error: tool "
                                   f"'{name}' failed repeatedly and kept returning the same error. "
                                   "This call keeps failing — try a different approach or answer the user.")
                        self.store.add_message(chat_id, 'tool', warning, tool_call_id=call['id'])
                        yield {'type': 'tool_result', 'name': name, 'ok': False,
                               'summary': warning[:200], 'output': warning}
                    else:
                        self.store.add_message(chat_id, 'tool', result, tool_call_id=call['id'])
                        yield {'type': 'tool_result', 'name': name, 'ok': ok, 'summary': result[:200], 'output': full_output}
        except ModelError as exc:
            review = await self._review(chat_id, cwd, tracker, derive_title(text, 60))
            if review is not None:
                yield review
            yield {'type': 'error', 'message': str(exc)}
