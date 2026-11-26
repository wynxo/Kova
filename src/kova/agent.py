"""Run one model-assisted chat turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from kova import delegation, egress, parallel, plugins, providers, sandbox
from kova.actors import DEFAULT_ACTOR_ID, Actor
from kova.checkpoints import BlobStore, ChangeTracker
from kova.compact import compact
from kova.config import Config, load_config
from kova.persona import DEFAULT_NAME, Persona
from kova.memory import Memory, Note
from kova.models import ModelClient, ModelError
from kova.modes import DEFAULT_MODE, capabilities as mode_capabilities
from kova.store import Store
from kova.tools import Tool, ToolError, openai_schemas
from kova.tools.core import CORE_TOOLS, FILE_TOOLS, FileSession, validate
from kova.tools.memory import tools_for as memory_tools_for
from kova.tools import core
from kova.tools.sessions import set_current_chat
from kova.pairing import action_capabilities, narrow
from kova.permissions import Approvals, needs_approval, action_key
from kova.project_instructions import project_instructions
from kova.review import MemoryReview, ReviewSettings
from kova.skills import Skills
from kova.usage import TurnMeter, summarize, window_start
from kova.group_context import view_for


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
#: The tools that write notes; a bot without its own scope never gets them.
MEMORY_WRITE_TOOLS = ("memory_add", "memory_patch", "memory_remove", "memory_save")


def select_tools(mode: str, web: bool, tools: dict[str, Tool], *, local_only: bool = False) -> dict[str, Tool]:
    """The tools a chat may use: everything when the mode offers tools, web only otherwise.

    The web toggle applies to both modes; a future code mode declares its own
    capabilities in :mod:`kova.modes` instead of adding a branch here.
    """
    allowed = set(tools) if "tools" in mode_capabilities(mode) else set(_WEB_TOOLS)
    if not web or local_only:
        allowed -= set(_WEB_TOOLS)
    if local_only:
        allowed = {name for name in allowed if '__' not in name and name != 'run_command'}
    return {name: tool for name, tool in tools.items() if name in allowed}


def group_prompt(group: Mapping) -> str:
    """How a member speaks in a group chat: who is here and how to hand off."""
    lines = [f"- {line}" for line in group.get('members') or ()]
    text = ("\n\nYou are one member of a group chat. Other members:\n"
            + ("\n".join(lines) if lines else "- nobody else")
            + "\nMessages from the user start with @you:, other members' replies "
              "start with their @handle. Reply as yourself, without your own "
              "@handle in front. To hand work to another member, @mention them "
              "in your reply. If you have nothing to add, reply exactly (pass).")
    instructions = str((group.get('settings') or {}).get('instructions') or '').strip()
    if instructions:
        text += f"\n\nGroup instructions:\n{instructions}"
    return text


def build_system_prompt(now: datetime, tool_names: list[str], memories: list[Note],
                        mode: str = DEFAULT_MODE, *,
                        memory_usage: int | None = None,
                        memory_budget: int | None = None,
                        persona: Persona | None = None,
                        index: str | None = None,
                        project_name: str | None = None,
                        project_memories: list[Note] | None = None,
                        project_memory_usage: int | None = None,
                        project_memory_budget: int | None = None,
                        project_index: str | None = None,
                        actor: Actor | None = None,
                        user_memories: list[Note] | None = None,
                        user_index: str | None = None,
                        project_memory_read_only: bool = False) -> str:
    """Build the identity, current context, and relevant memory prompt.

    ``memories`` are the topic notes recall picked for this message; ``index``
    is the small index note that is always loaded, so the model knows what
    exists even when nothing matched. A project chat also gets its own scope,
    labelled with the project's name so the two stores never look like one.

    ``actor`` names the speaker. The default assistant (or ``None``) is today's
    prompt; a bot swaps in its own name and adds its role section, in the order
    the design fixes: identity, role, context, memory.
    """
    persona = persona or Persona()
    bot = actor is not None and not actor.is_default
    if bot:
        try:  # a bot name is validated when it is saved; keep a hand-built one safe too
            persona = Persona(actor.name, persona.tone)
        except ValueError:
            persona = Persona(DEFAULT_NAME, persona.tone)
    identity = _IDENTITIES.get(mode, _IDENTITIES[DEFAULT_MODE])
    identity = identity.replace("Kova", persona.name).replace(
        "Be warm, direct and genuinely helpful.", persona.instruction)
    if bot and actor.role.strip():
        identity += f"\n\nYour role:\n{actor.role.strip()}"
    tools = ", ".join(tool_names) if tool_names else "none"
    prompt = (f"{identity}\nCurrent time: {now.isoformat()}\nCurrent mode: {mode}\n"
              f"Available tools: {tools}")
    if memories or memory_budget is not None:
        subject = "in your own notes" if bot else "about the user"
        if memory_budget:
            used = memory_usage or 0
            percent = round(used * 100 / memory_budget)
            header = (f"What you remember {subject} (memory {used:,} of "
                      f"{memory_budget:,} chars used, {percent}% full; over-budget "
                      "writes fail, so patch or remove notes to free space):")
        elif memory_budget == 0:
            header = f"What you remember {subject} (memory size is unlimited):"
        else:
            header = f"What you remember {subject}:"
        if memories:
            notes = "\n\n".join(f"## {note.title}\n{note.body}" for note in memories)
            prompt += f"\n\n{header}\n{notes}"
        else:
            prompt += f"\n\n{header} nothing yet."
    if index:
        prompt += ("\n\nMemory index (always loaded; open a topic note or run memory_search "
                   f"when you need the detail):\n{index}")
    if user_memories or user_index:
        # A bot with its own memory may still read what the user saved, but the
        # notes are context only: no tool of its turn can change them.
        header = "What the user has saved (read-only; you cannot change these notes):"
        if user_memories:
            notes = "\n\n".join(f"## {note.title}\n{note.body}" for note in user_memories)
            prompt += f"\n\n{header}\n{notes}"
        else:
            prompt += f"\n\n{header} nothing relevant this time."
        if user_index:
            prompt += ("\n\nThe user's memory index (read-only; always loaded; use what is "
                       f"here but do not rewrite it):\n{user_index}")
    if project_name is not None or project_memories or project_memory_budget is not None:
        label = f' for project "{project_name}"' if project_name else ""
        read_only = " (read-only)" if project_memory_read_only else ""
        if project_memory_budget:
            used = project_memory_usage or 0
            percent = round(used * 100 / project_memory_budget)
            header = (f"What this project remembers{label}{read_only} (memory {used:,} of "
                      f"{project_memory_budget:,} chars used, {percent}% full):")
        elif project_memory_budget == 0:
            header = f"What this project remembers{label}{read_only} (memory size is unlimited):"
        else:
            header = f"What this project remembers{label}{read_only}:"
        if project_memories:
            notes = "\n\n".join(f"## {note.title}\n{note.body}" for note in project_memories)
            prompt += f"\n\n{header}\n{notes}"
        else:
            prompt += f"\n\n{header} nothing yet."
        if project_index:
            prompt += ("\n\nProject memory index (always loaded; open a topic note or run "
                       f"memory_search for the detail):\n{project_index}")
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

    def __init__(self, capabilities: frozenset[str] | None = None) -> None:
        self._pending: list[str] = []
        #: The device capabilities the turn runs with; None means the computer itself.
        self.capabilities = capabilities

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

    def stop_task(self, chat_id: str, task_id: str) -> bool:
        """Stop one queued or running call; the turn and its other calls go on.

        False when the call is finished, was never part of a group, or belongs
        to a different chat.
        """
        stop = self.task_controls.get(chat_id, {}).get(task_id)
        return bool(stop and stop())

    def __init__(self, config: Config, store: Store, memory: Memory,
                 tools: dict[str, Tool], transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.config = config
        self.store = store
        self.memory = memory
        self.skills = Skills.for_home(config.home, extra_roots=plugins.plugin_skill_roots(config.home))
        self.tools = tools
        self.transport = transport
        self.now = now
        self.approvals = Approvals(path=config.home / "permission-rules.json")
        self.file_sessions: dict[tuple[str, str], FileSession] = {}
        # Per chat, a stop() for each call of a parallel group that is running now (#192).
        self.task_controls: dict[str, dict[str, Callable[[], bool]]] = {}
        # Background memory maintenance is opt-in (memory.review / memory.consolidate).
        settings = ReviewSettings.from_config(config)
        self.memory_review = MemoryReview(store, settings) if settings.enabled else None

        # File contents for per-turn checkpoints, outside every working folder.
        self.blobs = BlobStore(config.home / "checkpoints")
        # The local day the monthly budget warning was last shown (#75).
        self._budget_warned: str | None = None

    def _budget_warning(self) -> dict | None:
        """Once per local day, a notice when this month's spend passes 80% of the budget.

        It only informs: nothing is ever refused because of a budget.
        """
        budget = float(getattr(self.config, 'usage_monthly_budget_usd', 0) or 0)
        now = self.now()
        today = now.date().isoformat()
        if budget <= 0 or self._budget_warned == today:
            return None
        rows = self.store.usage_rows(window_start(1, now))
        status = summarize(rows, days=1, now=now, budget_usd=budget)['budget']
        if not status['warning']:
            return None
        self._budget_warned = today
        return status

    def _skills_block(self) -> str:
        """The prompt's skills block, with plugins enabled since the server started."""
        self.skills.extra_roots = plugins.plugin_skill_roots(self.config.home)
        return self.skills.prompt_block()

    async def _review(self, chat_id: str, cwd: Path, tracker: ChangeTracker | None,
                      label: str) -> dict | None:
        """Record the files this turn changed and describe them for review.

        A checkpoint never fails a turn: any problem recording one is dropped
        and the turn ends normally.
        """
        if self.store.is_ephemeral(chat_id) or tracker is None or not tracker.touched:
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

    def _turn_context(self, chat_id: str, user_message_id: int | None, model_text: str,
                      attached: list[dict], *, actor: Actor | None = None,
                      group: Mapping | None = None) -> list[dict]:
        """Stored history with this turn's extracted attachment text applied.

        The stored user message stays exactly what the user wrote. For the
        model request only, the message this turn just wrote carries the
        extracted text, so tool rounds, retries and compaction still see it
        without the transcript ever containing it. A retry wrote no new
        message, so it lends the text to the last user message already stored.
        """
        if group is not None:
            return self._group_context(chat_id, user_message_id, model_text, attached,
                                       actor=actor, group=group)
        entries = self.store.get_context_entries(chat_id)
        if attached:
            for ident, message in entries:
                if ident == user_message_id:
                    message['content'] = model_text
                    break
            else:
                if user_message_id is None:
                    for _, message in reversed(entries):
                        if message.get('role') == 'user':
                            message['content'] = model_text
                            break
        return [message for _, message in entries]

    def _group_context(self, chat_id: str, user_message_id: int | None, model_text: str,
                       attached: list[dict], *, actor: Actor | None,
                       group: Mapping) -> list[dict]:
        """One group member's own view of the shared transcript (design 4.3)."""
        summary, _, entries = self.store.context_snapshot(chat_id, attributed=True)
        rows = [message for _, message in entries]
        if attached:
            target = next((message for ident, message in entries if ident == user_message_id), None)
            if target is None:
                target = next((message for message in reversed(rows)
                               if message.get('role') == 'user'), None)
            if target is not None:
                target['content'] = model_text
        speaker = actor.id if actor is not None else DEFAULT_ACTOR_ID
        return view_for(speaker, rows, group.get('settings') or {},
                        handles=group.get('handles') or {}, summary=summary)

    async def run_turn(self, chat_id: str, text: str, model: str | None = None, *,
                       steering: Steering | None = None, attachment_ids: list[str] | None = None,
                       capabilities: frozenset[str] | None = None,
                       record_user: bool = True, turn_id: str | None = None,
                       variant: int | None = None, actor: Actor | None = None,
                       hop: int = 0, group: Mapping | None = None) -> AsyncIterator[dict]:
        """Persist and stream a turn under its home and chat privacy policy.

        A chat a paired phone wrote in keeps that phone's limits here, for every
        caller (the server, scheduled prompt jobs), not only for HTTP requests.
        ``turn_id`` and ``variant`` name the attempt a retry or edit is writing;
        every other turn starts a new one.

        ``actor`` is who answers: ``None`` is today's behaviour, byte for byte.
        A bot's turn tags every event with its ``author`` and a fresh
        ``bot_turn_id``, so a client can put a reply and its tool calls in the
        right lane without changing anything for the default assistant.
        ``hop`` is 0 for a reply to the user and 1+ for a hand-off between bots.
        ``group`` is set by the group runner: ``settings`` (resolved group
        settings), ``handles`` (bot id to handle) and ``members`` (one line
        each for the prompt). The history is then this member's own view.
        """
        capabilities = narrow(capabilities, self.store.chat_capabilities(chat_id))
        bot_turn_id = (uuid4().hex if (actor is not None and not actor.is_default)
                       or group is not None else None)
        with egress.scope(config=self.config, store=self.store, chat_id=chat_id):
            async for event in self._run_turn(chat_id, text, model, steering=steering,
                                              attachment_ids=attachment_ids, capabilities=capabilities,
                                              record_user=record_user, turn_id=turn_id,
                                              variant=variant, actor=actor,
                                              bot_turn_id=bot_turn_id, hop=hop, group=group):
                if bot_turn_id is None:
                    yield event
                else:
                    author = actor.id if actor is not None else DEFAULT_ACTOR_ID
                    yield {**event, 'author': author, 'bot_turn_id': bot_turn_id}

    async def _run_turn(self, chat_id: str, text: str, model: str | None = None, *,
                       steering: Steering | None = None, attachment_ids: list[str] | None = None,
                       capabilities: frozenset[str] | None = None,
                       record_user: bool = True, turn_id: str | None = None,
                       variant: int | None = None, actor: Actor | None = None,
                       bot_turn_id: str | None = None, hop: int = 0,
                       group: Mapping | None = None) -> AsyncIterator[dict]:
        """Persist and stream a turn, executing at most ten rounds of tools.

        ``model`` overrides the saved chat model for one turn; unset chats use the default.
        ``steering`` carries messages that arrive while the turn runs.
        ``capabilities`` limits a turn a paired phone started (kova.pairing): tools
        that need more are not offered at all, so no approval or rule can bring
        them back. None means the computer itself, with no limit.
        ``actor`` narrows the same turn to a bot: its model, tool allowlist,
        permission ceiling, folder, memory scopes and round/output limits.
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
        chat = self.store.get_chat(chat_id)
        chat_model = (chat or {}).get('model')
        # A bot's own model wins over the saved chat model, unless this single
        # turn names one. A removed model entry falls back the same way it
        # always has: the chat's model, then the active one.
        selected = model
        if selected is None and actor is not None and actor.model:
            chosen = self.config.model_entry(actor.model)
            if chosen is not None and chosen.model:
                selected = actor.model
        entry = self.config.model_entry(selected if selected is not None else chat_model)
        previous_model = self.store.last_turn_model(chat_id)
        turn_model = entry.name if entry is not None and entry.model else None
        turn_id = turn_id or uuid4().hex
        variant = variant or 1
        # A bot's rows carry who wrote them; the default assistant's stay NULL,
        # which is exactly what every row written before bots existed has.
        author = actor.id if actor is not None and not actor.is_default else None
        author_name = actor.name if author is not None else None

        def stored(*args, **kwargs):
            """Store one row of this turn under its author, unless it is the default's."""
            if author is not None:
                kwargs.setdefault('author', author)
                kwargs.setdefault('author_name', author_name)
                kwargs.setdefault('bot_turn_id', bot_turn_id)
                kwargs.setdefault('hop', hop)
            return self.store.add_message(*args, **kwargs)

        user_message_id = self.store.add_message(
            chat_id, 'user', text, turn_model=turn_model, turn_id=turn_id, variant=variant,
        ) if record_user else None
        if attached:
            # A saved message takes responsibility for its uploads: retain the
            # files even when the clean transcript no longer spells out an id.
            self.store.mark_attachments_used(chat_id, [row['id'] for row in attached])
            # Keep the file names attached to this message for reloaded chats.
            # The link carries no extracted text and is not part of model history.
            # A retry writes a new variant of the turn, with its own message row.
            if user_message_id is not None:
                self.store.link_message_attachments(user_message_id, attached)
        set_current_chat(chat_id, capabilities)
        if previous_model and turn_model and turn_model != previous_model:
            yield {'type': 'model_switch', 'model': turn_model}
        incognito = self.store.is_ephemeral(chat_id)
        mode = 'chat' if incognito else (chat or {}).get('mode') or DEFAULT_MODE
        web = (chat or {}).get('web', True)
        cwd = Path((chat or {}).get('cwd') or self.config.files_path).expanduser()
        if actor is not None and actor.folder is not None:
            # A bot answers in its own folder; like the Kova Files default, it
            # is created on first use instead of failing the turn.
            cwd = Path(actor.folder).expanduser()
            if not cwd.is_dir():
                try:
                    cwd.mkdir(parents=True, exist_ok=True)
                except OSError:
                    yield {'type': 'error',
                           'message': 'The bot working folder could not be created. Choose another folder.'}
                    return
        cwd = cwd.resolve()
        # A bot never gets more than the stricter of its ceiling and the chat's
        # permission; Actor.for_chat already resolved the two. The default
        # assistant's actor keeps the chat's own permission.
        permission = (actor.permission if actor is not None and not actor.is_default
                      else (chat or {}).get('permission', 'manual'))
        # A chat in a project with its own memory mounts exactly that scope.
        # Incognito never reads or writes memory, and a project without the
        # opt-in keeps today's global store. No other project is ever reachable.
        project = None
        project_memory = None
        if chat is not None and chat.get('project_id') and not incognito:
            project = self.store.get_project(chat['project_id'])
            if project is not None and project.get('memory_scope'):
                try:
                    project_memory = self.memory.project_scope(project['id'])
                except ValueError:  # a hand-edited id must not break the turn
                    project_memory = None
        # A bot with `memory.own` mounts its own hidden scope: the same
        # arrangement as a project scope, so no other store lists or searches
        # it. Without one, the bot may still recall the user's notes but its
        # write tools are removed below.
        bot = actor is not None and not actor.is_default
        bot_memory = None
        if bot and actor.memory_own and not incognito:
            try:
                bot_memory = self.memory.bot_scope(actor.id, budget=actor.memory_budget)
            except ValueError:  # a hand-edited id must not break the turn
                bot_memory = None
        # Full access is the user's explicit choice to run unconfined; every
        # other mode runs commands inside the OS sandbox when the kernel
        # supports one, and asks when it does not.
        confined = permission != 'full' and sandbox.available()
        # Incognito cannot execute arbitrary plugins or tools that persist data.
        tools = {} if incognito else select_tools(mode, web, self.tools, local_only=self.config.privacy_local_only)

        def needs(name: str, tool, args: dict | None = None) -> frozenset[str]:
            return action_capabilities(name, external=tool.external, cwd=cwd,
                                       files_path=self.config.files_path, home=self.config.home,
                                       args=args, memory=self.config.memory_path)

        if capabilities is not None:
            tools = {name: tool for name, tool in tools.items() if needs(name, tool) <= capabilities}
        if project_memory is not None:
            # Rebind only the memory tools; the same permission checks decide
            # whether a phone may write, and provenance stays exactly as before.
            scoped = {tool.name: tool for tool in memory_tools_for(project_memory)}
            tools = {name: scoped.get(name, tool) for name, tool in tools.items()}
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
        tools.pop(delegation.NAME, None)  # Reserved for the tool-free child runner.
        if mode == 'work' and capabilities is None and entry is not None and entry.model:
            child_entry = deepcopy(entry)

            async def delegate(args, emit=None):
                child = ModelClient(deepcopy(child_entry), transport=self.transport,
                                    privacy=self.config.privacy_secrets, config=self.config,
                                    fixed_destination=True)
                return await delegation.analyze(child, args['task'], emit)

            tools[delegation.NAME] = Tool(
                delegation.NAME,
                f"Ask the configured model ({entry.name}) to analyze a standalone text task. "
                "Use only for ordinary work explicitly requested by the user. "
                "The exact task text is shown for approval and sent to that model. "
                "No tools or chat history are passed. Consecutive independent calls can run together. "
                f"At most {delegation.ATTEMPT_LIMIT} attempts per turn, including denials.",
                delegation.PARAMETERS, delegate)
        if actor is not None and not actor.is_default:
            # Last, after the mode, the device limits and every bound per-chat
            # tool are in: a tool the bot is not offered cannot be brought back
            # by a rule, an approval or an approval setting.
            tools = {name: tool for name, tool in tools.items() if actor.allows(name)}
            if bot_memory is not None:
                # Only the bot's own notes are writable: the user's global
                # notes and a project's scope stay readable context at most.
                scoped = {tool.name: tool for tool in memory_tools_for(bot_memory)}
                tools = {name: scoped.get(name, tool) for name, tool in tools.items()}
            else:
                tools = {name: tool for name, tool in tools.items()
                         if name not in MEMORY_WRITE_TOOLS}
        delegate_attempts = 0
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
        # For a bot, the primary store is its own scope; the user's global notes
        # are read-only context, and only when the bot opts in to reading them.
        primary = bot_memory or self.memory
        try:
            memories = [] if incognito else await asyncio.to_thread(primary.search, model_text, 5, mode="hybrid")
        except Exception:  # memory trouble must never block a chat
            memories = []
        try:
            memory_usage = 0 if incognito else await asyncio.to_thread(primary.usage)
        except Exception:  # a broken note must not block the prompt either
            memory_usage = 0
        # The index is always loaded; topic notes are only the ones recall picked.
        try:
            index = "" if incognito else await asyncio.to_thread(primary.index_text,
                                            getattr(self.config, 'memory_index_lines', 200))
        except Exception:  # a broken index must not block the prompt either
            index = ""
        memories = [note for note in memories if note.slug != "index"]
        user_memories: list[Note] = []
        user_index = ""
        if bot_memory is not None and not incognito and actor.user_memory == "read":
            try:
                user_memories = await asyncio.to_thread(self.memory.search, model_text, 5,
                                                        mode="hybrid")
                user_index = await asyncio.to_thread(self.memory.index_text,
                                                     getattr(self.config, 'memory_index_lines', 200))
            except Exception:  # the user's notes must never block a bot's turn
                user_memories, user_index = [], ""
            user_memories = [note for note in user_memories if note.slug != "index"]
        project_memories: list[Note] = []
        project_usage = 0
        project_index = ""
        if project_memory is not None:
            try:
                project_memories = await asyncio.to_thread(
                    project_memory.search, model_text, 5, mode="hybrid")
                project_usage = await asyncio.to_thread(project_memory.usage)
                project_index = await asyncio.to_thread(
                    project_memory.index_text,
                    getattr(self.config, 'memory_index_lines', 200))
            except Exception:  # project memory trouble must never block a chat
                project_memories, project_usage, project_index = [], 0, ""
            project_memories = [note for note in project_memories if note.slug != "index"]
        try:
            skills = await asyncio.to_thread(self._skills_block) if mode != 'chat' else ""
        except Exception:  # a broken skill file must not block the prompt
            skills = ""
        try:  # the identity is a nicety: trouble reading it must never block a chat
            current_config = load_config(self.config.home)
            persona = Persona(current_config.persona_name, current_config.persona_tone)
        except (OSError, ValueError):
            persona = Persona()
        prompt = build_system_prompt(self.now(), list(tools), memories, mode, persona=persona,
                                     memory_usage=memory_usage,
                                     memory_budget=getattr(primary, 'budget', 0),
                                     index=index or None,
                                     project_name=project["name"] if project_memory else None,
                                     project_memories=project_memories,
                                     project_memory_usage=project_usage,
                                     project_memory_budget=(
                                         project_memory.budget if project_memory else None),
                                     project_index=project_index or None,
                                     actor=actor,
                                     user_memories=user_memories or None,
                                     user_index=user_index or None,
                                     project_memory_read_only=bot_memory is not None)
        if incognito:
            prompt += ("\nIncognito: this conversation lives only in server memory. "
                       "Do not save memories, use tools, or claim it will be remembered. "
                       "The configured model provider still receives messages.")
        if group is not None:
            prompt += group_prompt(group)
        if mode != 'chat':
            prompt += f"\nWorking folder: {cwd}\nPermission mode: {permission}"
        if mode != 'chat' or project:
            instructions = await asyncio.to_thread(
                project_instructions, cwd if mode != 'chat' else None,
                user_home=self.config.home, saved=project["instructions"] if project else "")
            if instructions:
                prompt += f"\n\n{instructions}"
        if mode != 'chat':
            if skills:
                prompt += f"\n\n{skills}"
            if (chat or {}).get('plan'):
                prompt += "\nCurrent plan: " + json.dumps(chat['plan'])
        if rows:
            # Turn-only index: ids make read_attachment work on later turns
            # without placing attachment text or ids in the transcript.
            prompt += ("\n\nAttachments in this chat (read one with read_attachment, "
                       "using its id):\n" + attachments.index(rows.values()))
        if entry is None or not entry.model:
            yield {'type': 'error', 'message': "No model is set up yet. Run: kova setup"}
            return
        effort = (chat or {}).get('reasoning_effort') if entry.name == chat_model else entry.reasoning_effort
        if actor is not None and not actor.is_default and actor.reasoning_effort:
            effort = actor.reasoning_effort
        client = ModelClient(entry, transport=self.transport,
                             privacy=getattr(self.config, "privacy_secrets", "off"), config=self.config,
                             reasoning_effort=effort,
                             max_output_tokens=(actor.max_output_tokens
                                                if actor is not None and not actor.is_default
                                                else None))
        # A provider that does not declare tool calling is never sent schemas,
        # so its model can neither call tools nor reach an approval.
        schemas = (openai_schemas(tools) or None) if providers.supports(entry._provider, "tools") else None

        def parallel_args(call: dict) -> dict | None:
            """The parsed arguments when ``call`` may join a parallel group, else None.

            Only read-only tools that would run right away qualify: anything a
            rule or the permission mode would stop to ask about, or that this
            chat's authority does not cover, keeps the one-at-a-time path.
            """
            name = call['name']
            tool = tools.get(name)
            if name not in parallel.PARALLEL_TOOLS or tool is None or tool.external:
                return None
            try:
                args = json.loads(call['arguments'])
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(args, dict):
                return None
            try:
                if name in CORE_TOOLS:
                    validate(tool.parameters, args)
                if capabilities is not None and needs(name, tool, args) - capabilities:
                    return None
                policy, _ = self.approvals.policy(chat_id, name, args, cwd)
                if policy in ('deny', 'ask'):
                    return None
                if policy != 'allow' and needs_approval(name, args, permission, cwd, self.config.files_path,
                                                        False, sandboxed=confined, external=tool.external):
                    return None
            except Exception:  # anything doubtful takes the ordinary path, which reports it
                return None
            return args

        meter = TurnMeter()
        pricing = getattr(self.config, 'pricing', None) or {}
        # A bot may use fewer model calls than the agent's own hard outer limit.
        rounds = 11 if actor is None else max(1, min(11, actor.max_rounds))
        last_round = rounds - 1
        try:
            repeat_count = 0
            repeat_fp = None
            repeat_error = None
            awaiting_replacement = False
            attempted_compaction = False
            for step in range(rounds):
                if steering is not None:
                    for message in steering.take():
                        # A steer joins the history at a round boundary, never
                        # between an assistant tool call and its tool reply.
                        self.store.add_message(chat_id, 'user', message,
                                               turn_id=turn_id, variant=variant)
                        yield {'type': 'steer', 'text': message}
                history = self._turn_context(chat_id, user_message_id, model_text, attached,
                                                   actor=actor, group=group)
                threshold = max(0.1, min(float(self.config.compact_threshold), 1.0))
                size = len(prompt) + sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
                if not attempted_compaction and size > entry.context * 4 * threshold:
                    attempted_compaction = True
                    event = await compact(self.store, chat_id, client, context_tokens=entry.context,
                                          reserved_chars=len(prompt), threshold=threshold)
                    if event:
                        yield event
                        history = self._turn_context(chat_id, user_message_id, model_text, attached,
                                                   actor=actor, group=group)
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
                round_schemas = schemas if step < last_round else None
                meter.start_round()
                async for event in client.stream(messages, round_schemas):
                    if event.type in ('text', 'reasoning'):
                        meter.output(event.text)
                        if event.type == 'text':
                            content.append(event.text)
                        yield {'type': event.type, 'text': event.text}
                    elif event.type == 'usage':
                        meter.usage(event.usage)
                    elif event.type == 'privacy' and event.privacy is not None:
                        # A safe summary only: counts and kinds, never a value.
                        yield {'type': 'privacy', **event.privacy}
                    elif event.type == 'tool_call_delta' and event.tool_call is not None and step < last_round:
                        meter.output()
                        yield {'type': 'tool_call_delta', **event.tool_call}
                    elif event.type == 'tool_call' and event.tool_call is not None and step < last_round:
                        meter.count(event.tool_call.get('arguments') or '')
                        calls.append(event.tool_call)
                meter.end_round([messages, round_schemas] if round_schemas else messages)
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
                        stored(chat_id, 'assistant', answer or None)
                        continue
                    created = datetime.now(timezone.utc).isoformat()
                    message_id = stored(chat_id, 'assistant', answer, created=created)
                    usage = meter.summary(entry, pricing)
                    self.store.record_usage(chat_id, message_id, usage)
                    if (actor is not None and not actor.is_default and actor.max_output_tokens
                            and (usage.get('completion_tokens') or 0) >= actor.max_output_tokens):
                        # The bot's per-turn budget is spent: end here, and say so.
                        yield {'type': 'notice', 'kind': 'bot_limit',
                               'notice': f"{actor.name} reached its "
                                         f"{actor.max_output_tokens:,}-token output limit for this turn."}
                    if needs_title:
                        current = self.store.get_chat(chat_id)
                        if current is not None and not current['title']:
                            self.store.set_title(chat_id, derive_title(title_source))
                    review = await self._review(chat_id, cwd, tracker, derive_title(text, 60))
                    if review is not None:
                        yield review
                    # A limited turn without memory:write must not reach memory
                    # through the background pass either: notes go into every chat.
                    may_remember = capabilities is None or 'memory:write' in capabilities
                    if not incognito and self.memory_review is not None and may_remember:
                        # Opt-in background passes run last so the answer is already
                        # stored; a failure there can never break the reply.
                        for notice in await self.memory_review.after_turn(
                                chat_id=chat_id, mode=mode, memory=self.memory,
                                review_memory=bot_memory or project_memory,
                                client=client, now=self.now()):
                            yield notice
                    done = {'type': 'done', 'message_id': message_id, 'created': created, 'model': entry.name, 'usage': usage}
                    if not incognito:
                        try:
                            warning = self._budget_warning()
                        except Exception:  # a usage query must never break a finished reply
                            warning = None
                        if warning is not None:
                            done['budget_warning'] = warning
                    yield done
                    return
                formatted = [{**call.get('extra_fields', {}), 'id': call['id'], 'type': 'function', 'function': {
                    'name': call['name'], 'arguments': call['arguments']}} for call in calls]
                stored(chat_id, 'assistant', answer or None, tool_calls=formatted)
                # Consecutive read-only calls that need no answer run at once (#192).
                ready = [parallel_args(call) for call in calls]
                groups = parallel.runs([args is not None for args in ready])
                # Delegation is a separate run: never move it across other tools.
                delegate_groups = {}
                for index in range(len(calls) - 1, -1, -1):
                    if delegation.NAME in tools and calls[index]['name'] == delegation.NAME:
                        delegate_groups[index] = delegate_groups.get(index + 1, index + 1)
                grouped: dict[int, tuple[str, bool]] = {}
                delegated_started: set[int] = set()
                for index, call in enumerate(calls):
                    name = call['name']
                    is_delegate = index in delegate_groups and index not in grouped
                    batch = []
                    if is_delegate:
                        for i in range(index, delegate_groups[index]):
                            child_call = calls[i]
                            try:
                                args = json.loads(child_call['arguments'])
                                validate(delegation.PARAMETERS, args)
                                if not args['task'].strip():
                                    raise ToolError('Supply a nonempty task.')
                                if delegate_attempts >= delegation.ATTEMPT_LIMIT:
                                    raise ToolError(f'At most {delegation.ATTEMPT_LIMIT} child analyses may be attempted per turn.')
                                delegate_attempts += 1
                                policy, reason = self.approvals.policy(chat_id, name, args, cwd)
                                if policy == 'deny':
                                    raise ToolError('Action denied by permission rule. ' + (reason or 'Do not retry.'))
                            except (ToolError, ValueError, TypeError) as exc:
                                grouped[i] = (f'Error: {exc}', False)
                            else:
                                ready[i] = args
                                batch.append((i, child_call, tools[name], args))
                    elif index in groups:
                        batch = [(i, calls[i], tools[calls[i]['name']], ready[i])
                                 for i in range(index, groups[index])]
                    if batch:
                        try:
                            if is_delegate:
                                destination = delegation.destination(child_entry)
                                tasks = [args['task'] for _, _, _, args in batch]
                                binding_args = {'destination': destination, 'tasks': tasks}
                                listing = '\n'.join(f'{i}. {json.dumps(task, ensure_ascii=False)}'
                                                    for i, task in enumerate(tasks, 1))
                                subject = f'all {len(tasks)} tasks' if len(tasks) > 1 else 'this task'
                                approval = self.approvals.create(
                                    chat_id, name, binding_args, cwd=cwd,
                                    summary=f'Send {subject} to {destination} for child analysis:\n{listing}')
                                binding = approval['binding']
                                try:
                                    yield approval
                                    decision = await self.approvals.wait(approval['id'])
                                finally:
                                    pending = self.approvals.pending.pop(approval['id'], None)
                                    if pending and not pending.future.done():
                                        pending.future.cancel()
                                yield {'type': 'approval_resolved', 'id': approval['id'], **decision}
                                if action_key(name, binding_args, cwd) != binding:
                                    raise ToolError('Action changed after approval; request fresh authorization.')
                                if decision['decision'] == 'deny':
                                    raise ToolError('Action denied. Do not retry without new authorization.' +
                                                    (f" User guidance: {decision['message']}" if decision.get('message') else ''))
                                delegated_started.update(i for i, _, _, _ in batch)
                            controls = self.task_controls.setdefault(chat_id, {})
                            async with aclosing(parallel.run_group(
                                    batch, entry.name, grouped, controls=controls,
                                    run_child=delegate if is_delegate else None)) as events:
                                async for event in events:
                                    yield event
                        except ToolError as exc:
                            for i, _, _, _ in batch:
                                grouped[i] = (f'Error: {exc}', False)
                        except asyncio.CancelledError:
                            end = delegate_groups[index] if is_delegate else groups[index]
                            for batched in calls[index:end]:
                                stored(chat_id, 'tool', 'Error: Action denied or interrupted because the turn was stopped.', tool_call_id=batched['id'])
                            raise
                        finally:
                            if not self.task_controls.get(chat_id):
                                self.task_controls.pop(chat_id, None)
                    ran = index in grouped
                    if ran:
                        args = ready[index]
                        result, ok = grouped.pop(index)
                    else:
                        try:
                            args = json.loads(call['arguments'])
                        except (json.JSONDecodeError, TypeError):
                            args = None
                            result, ok = 'Error: arguments were not valid JSON', False
                        else:
                            result, ok = '', True
                        if name != delegation.NAME:
                            yield {'type': 'tool_call', 'id': call['id'], 'name': name, 'args': args}
                    if ok and not ran:
                        if name not in tools:
                            result, ok = f'Error: unknown tool {name}', False
                        else:
                            try:
                                if not isinstance(args, dict):
                                    raise ToolError('arguments must be an object')
                                if name in CORE_TOOLS:
                                    validate(tools[name].parameters, args)
                                if name == 'ask_user':
                                    question = self.approvals.create(chat_id, name, args, cwd=cwd,
                                                                     authority=capabilities)
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
                                    stored(chat_id, 'tool', result, tool_call_id=call['id'])
                                    yield {'type': 'tool_result', 'id': call['id'], 'name': name, 'ok': decision['decision'] == 'allow', 'summary': result[:200], 'output': result}
                                    continue
                                prepared = None
                                if name in FILE_TOOLS:
                                    prepared = await asyncio.to_thread(files.prepare, name, args)
                                missing = (needs(name, tools[name], args) - capabilities
                                           if capabilities is not None else frozenset())
                                if missing:
                                    # Tools are filtered by name above; this catches what only
                                    # the arguments reveal, such as writing AGENTS.md.
                                    raise ToolError('This needs ' + ', '.join(sorted(missing)) +
                                                    ', which the paired phone behind this chat does not have.')
                                policy, reason = self.approvals.policy(chat_id, name, args, cwd)
                                if policy == 'deny':
                                    raise ToolError('Action denied by permission rule. ' + (reason or 'Do not retry.'))
                                if policy == 'ask' or (policy != 'allow' and needs_approval(
                                        name, args, permission, cwd, self.config.files_path,
                                        False, sandboxed=confined, external=tools[name].external)):
                                    approval = self.approvals.create(chat_id, name, args, cwd=cwd,
                                                                     needs=needs(name, tools[name], args),
                                                                     authority=capabilities)
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
                                result = await parallel.call_tool(tools[name], args)
                                if name == 'update_plan':
                                    items = args['items']
                                    self.store.update_chat(chat_id, plan=items)
                                    yield {'type': 'plan', 'items': items}
                                if name == 'run_command':
                                    ok = json.loads(result)['ok']
                            except asyncio.CancelledError:
                                stored(chat_id, 'tool', 'Error: Action denied or interrupted because the turn was stopped.', tool_call_id=call['id'])
                                raise
                            except Exception as exc:
                                result, ok = f'Error: {exc}', False
                    if name == delegation.NAME and index not in delegated_started:
                        yield {'type': 'tool_call', 'id': call['id'], 'name': name, 'args': args}
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
                        stored(chat_id, 'tool', warning, tool_call_id=call['id'])
                        yield {'type': 'tool_result', 'id': call['id'], 'name': name, 'ok': False,
                               'summary': warning[:200], 'output': warning}
                    else:
                        stored(chat_id, 'tool', result, tool_call_id=call['id'])
                        event = {'type': 'tool_result', 'id': call['id'], 'name': name, 'ok': ok, 'summary': result[:200], 'output': full_output}
                        if ok and tracker is not None and name in {'write_file', 'edit_file'}:
                            # A presentation failure must never fail a successful write.
                            try:
                                change = await asyncio.to_thread(tracker.diff_for, args.get('path', ''))
                                if change is not None:
                                    event['file_change'] = change
                            except Exception:
                                pass
                        yield event
        except ModelError as exc:
            review = await self._review(chat_id, cwd, tracker, derive_title(text, 60))
            if review is not None:
                yield review
            yield {'type': 'error', 'message': str(exc)}
