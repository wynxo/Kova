"""Background memory maintenance: post-turn review and nightly consolidation.

Both passes are opt-in (``memory.review`` and ``memory.consolidate`` in
``config.toml``; both default to off) and both are auditable and reversible:
every write they make is recorded in ``memory_revisions`` with its preimage and
described by one human-readable line in ``memory_diary``.

**Post-turn review** (#37) forks a just-finished conversation and asks one
narrow question using two read-only tools (``memory_list``, ``memory_search``)
and a single memory-write tool (``memory_add``). A fact is stored only when its
``quote`` appears verbatim in a *user* message of that conversation, so nothing
is ever inferred from tone; negative lessons ("tool X is broken") are refused
outright. Each stored fact surfaces as one line: ``Learned: … (say undo)``, and
:func:`MemoryReview.undo` reverses it from the recorded preimage.

**Nightly consolidation** (#38) asks the model for *operations* over existing
notes (``merge``, ``rewrite``, ``forget``), never for replacement prose. Each
operation must clear a confidence score gate, cite verbatim evidence, and be
composed only of lines that already exist in its sources: consolidation may
drop or regroup what is remembered, it can never invent text. The run is
refused as a whole when the prior content it would lose exceeds the configured
ratio, and every operation stores its preimage so ``undo`` restores it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kova.memory import Memory, Note
from kova.models import ModelClient, ModelError
from kova.store import Store
from kova.tools import Tool, openai_schemas

#: Revision kinds recorded in the store, and the diary categories that mirror them.
REVIEW_KIND = "review"
CONSOLIDATE_KIND = "consolidate"
UNDO_KIND = "undo"

#: Review runs every this many user turns, unless configured otherwise.
DEFAULT_REVIEW_EVERY = 12
#: An operation below this confidence is dropped by the score gate.
DEFAULT_MIN_CONFIDENCE = 0.8
#: At most this share of the store's characters may be dropped by one run.
DEFAULT_MAX_LOSS = 0.25
#: How many read/write rounds the review fork may take.
MAX_REVIEW_ROUNDS = 3
#: A quote this short cannot point at anything, so it is not evidence.
MIN_QUOTE_CHARS = 12
#: How much of the conversation the fork is shown.
MAX_TRANSCRIPT_CHARS = 8000

#: Memory never records a "this is broken" lesson about a tool or a model.
_NEGATIVE = re.compile(
    r"\b(broken|buggy|unreliable|useless|doesn'?t work|does not work|not working|"
    r"fails?|failed|crashes?|hangs?)\b",
    re.IGNORECASE,
)
#: Frontmatter fences and headings are structural, not remembered content.
_OP_KINDS = ("merge", "rewrite", "forget")


@dataclass
class ReviewSettings:
    """The opt-in switches and gates that drive both background passes."""

    review: bool = False
    every: int = DEFAULT_REVIEW_EVERY
    consolidate: bool = False
    consolidate_hour: int = 3
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    max_loss: float = DEFAULT_MAX_LOSS

    @property
    def enabled(self) -> bool:
        """Whether any background pass is switched on."""
        return self.review or self.consolidate

    @classmethod
    def from_config(cls, config) -> "ReviewSettings":
        """Read the settings from a :class:`~kova.config.Config`."""
        return cls(
            review=bool(getattr(config, "memory_review", False)),
            every=max(1, int(getattr(config, "memory_review_every", DEFAULT_REVIEW_EVERY))),
            consolidate=bool(getattr(config, "memory_consolidate", False)),
            consolidate_hour=int(getattr(config, "memory_consolidate_hour", 3)),
            min_confidence=float(getattr(config, "memory_consolidate_min_confidence",
                                         DEFAULT_MIN_CONFIDENCE)),
            max_loss=float(getattr(config, "memory_consolidate_max_loss", DEFAULT_MAX_LOSS)),
        )


@dataclass
class ReviewedFact:
    """One fact the review could point at, with the user's own words for it."""

    title: str
    body: str
    quote: str


@dataclass
class Operation:
    """One consolidation operation, as the model proposed it."""

    kind: str
    slugs: list[str]
    title: str = ""
    tags: list[str] = field(default_factory=list)
    body: str = ""
    confidence: float = 0.0
    evidence: str = ""
    reason: str = ""


@dataclass
class ConsolidationResult:
    """What one consolidation run planned, refused and applied."""

    operations: list[Operation] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


# -- shared helpers -------------------------------------------------------

def _text(message: dict) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def user_statements(messages: Sequence[dict]) -> list[str]:
    """Only what the user wrote: assistant text is never evidence."""
    return [text for message in messages
            if message.get("role") == "user" and (text := _text(message))]


def turn_count(messages: Sequence[dict]) -> int:
    """How many user turns a conversation has."""
    return sum(1 for message in messages if message.get("role") == "user")


def _first_json(raw: str) -> object:
    """The first JSON value in ``raw``, tolerating prose and ```json fences."""
    starts = [index for index in (raw.find("{"), raw.find("[")) if index != -1]
    if not starts:
        raise ValueError("the model returned no JSON")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[min(starts):])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the model returned invalid JSON: {exc}") from None
    return value


def _transcript(messages: Sequence[dict], limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The tail of a conversation as plain ``role: text`` lines, bounded in size."""
    lines = [f"{message.get('role')}: {_text(message)}" for message in messages
             if message.get("role") in ("user", "assistant") and _text(message)]
    body = "\n".join(lines)
    if len(body) <= limit:
        return body
    return "…\n" + body[-limit:]


async def _ask(client: ModelClient, messages: list[dict],
               schemas: list[dict] | None) -> tuple[list[dict], str]:
    """One model pass: the tool calls it asked for, and any text it produced."""
    calls: list[dict] = []
    parts: list[str] = []
    async for event in client.stream(messages, schemas):
        if event.type == "text":
            parts.append(event.text)
        elif event.type == "tool_call" and event.tool_call is not None:
            calls.append(event.tool_call)
    return calls, "".join(parts)


def _tool_reply(call: dict, result: str) -> dict:
    return {"role": "tool", "tool_call_id": call["id"], "content": result}


def _assistant_message(text: str, calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {"id": call["id"], "type": "function",
             "function": {"name": call["name"], "arguments": call["arguments"]}}
            for call in calls
        ],
    }


# -- #37: post-turn review ------------------------------------------------

#: The single narrow question the fork is asked, with its exact answer contract.
REVIEW_INSTRUCTION = f"""You review one finished conversation to find facts worth remembering about the user.
Rules, in order:
1. Look at memory first (memory_search, memory_list) so you never duplicate a note.
2. Save a fact only when you can quote the user's own words for it: call memory_add with
   title, body and quote, where quote is at least {MIN_QUOTE_CHARS} characters copied exactly
   from a user message above.
3. Never infer mood, personality or traits from tone, and never record a lesson about a tool
   being broken, slow or unreliable.
4. Facts the user did not state themselves are not your job here.
5. If nothing is worth saving, call no tools at all and reply with one short sentence."""


def review_fact(args: dict, quotes: Sequence[str]) -> tuple[ReviewedFact | None, str]:
    """Validate one ``memory_add`` call from the review fork.

    Returns the fact to store, or a refusal reason the model can act on. The
    quote must appear verbatim in a user statement, so every reviewed memory
    can be pointed at afterwards; negative lessons about tools are refused.
    """
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    quote = str(args.get("quote") or "").strip()
    if not title or not body:
        return None, "title and body are both required"
    if len(quote) < MIN_QUOTE_CHARS:
        return None, f"quote must be at least {MIN_QUOTE_CHARS} characters of the user's own words"
    if not any(quote in statement for statement in quotes):
        return None, "quote was not found in a user message; copy the user's exact words"
    if _NEGATIVE.search(title) or _NEGATIVE.search(body):
        return None, "Kova never records a lesson about something being broken or unreliable"
    return ReviewedFact(title=title, body=body, quote=quote), ""


def learned_notice(fact: ReviewedFact) -> str:
    """The one-line notice a stored review fact surfaces."""
    return f"Learned: {fact.title} — {fact.body} (say undo)"


class MemoryReview:
    """Run the opt-in background memory passes for a finished turn."""

    def __init__(self, store: Store, settings: ReviewSettings | None = None) -> None:
        self.store = store
        self.settings = settings or ReviewSettings()

    @property
    def enabled(self) -> bool:
        """Whether any pass is switched on; the agent hooks only when it is."""
        return self.settings.review or self.settings.consolidate

    def review_due(self, chat_id: str, messages: Sequence[dict]) -> bool:
        """Whether the review should run now: every N turns, and not twice for one."""
        turns = turn_count(messages)
        if turns == 0 or turns % self.settings.every != 0:
            return False
        return self.store.memory_review_turns(chat_id) != turns

    def nightly_due(self, now: datetime) -> bool:
        """Whether nightly consolidation has not run yet today and it is past its hour."""
        if now.hour < self.settings.consolidate_hour:
            return False
        last = self.store.last_memory_diary(CONSOLIDATE_KIND)
        if last is None:
            return True
        try:
            ran_at = datetime.fromisoformat(last["created"]).astimezone()
        except ValueError:
            return True
        return ran_at.date() != now.date()

    def _review_tools(self, memory: Memory, chat_id: str,
                      quotes: Sequence[str], stored: list[ReviewedFact]) -> dict[str, Tool]:
        """The fork's whole toolbox: two read-only tools and one write tool."""
        def list_notes(_args: dict) -> str:
            return json.dumps([
                {"slug": note.slug, "title": note.title, "body": note.body,
                 "origin": note.origin, "tags": note.tags}
                for note in memory.list()[:50]
            ])

        def search(args: dict) -> str:
            notes = memory.search(str(args.get("query") or ""), 10, mode="hybrid")
            return json.dumps([
                {"slug": note.slug, "title": note.title, "body": note.body} for note in notes
            ])

        def add(args: dict) -> str:
            fact, refusal = review_fact(args, quotes)
            if fact is None:
                return f"Error: {refusal}"
            try:
                note = memory.add(fact.title, fact.body, ["review"], origin="user", source=chat_id)
            except ValueError as exc:
                return f"Error: {exc}"
            record_revision(self.store, memory, note.slug, REVIEW_KIND, before=None,
                            chat_id=chat_id, detail={"title": fact.title, "quote": fact.quote})
            self.store.add_memory_diary(
                REVIEW_KIND, f"Learned '{fact.title}' from chat {chat_id} (quoted: {fact.quote[:120]})")
            stored.append(fact)
            return f"Saved memory: {note.title}"

        properties = {
            "title": {"type": "string"}, "body": {"type": "string"},
            "quote": {"type": "string"},
        }
        return {
            "memory_list": Tool("memory_list", "List memory notes with their slugs. Read-only.",
                                {"type": "object", "properties": {}}, list_notes),
            "memory_search": Tool("memory_search", "Search memory. Read-only.",
                                  {"type": "object", "properties": {"query": {"type": "string"}},
                                   "required": ["query"]}, search),
            "memory_add": Tool(
                "memory_add",
                "Save one fact the user stated, with the user's exact words in quote.",
                {"type": "object", "properties": properties,
                 "required": ["title", "body", "quote"]}, add),
        }

    async def review(self, chat_id: str, memory: Memory,
                     client: ModelClient) -> list[ReviewedFact]:
        """Fork the conversation and store the facts the fork can point at."""
        messages = self.store.get_messages(chat_id)
        quotes = user_statements(messages)
        if not quotes:
            return []
        stored: list[ReviewedFact] = []
        tools = self._review_tools(memory, chat_id, quotes, stored)
        schemas = openai_schemas(tools) or None
        request = [{"role": "system", "content": REVIEW_INSTRUCTION},
                   {"role": "user", "content": _transcript(messages)}]
        for _ in range(MAX_REVIEW_ROUNDS):
            calls, text = await _ask(client, request, schemas)
            if not calls:
                break
            request.append(_assistant_message(text, calls))
            for call in calls:
                tool = tools.get(call.get("name") or "")
                if tool is None:
                    request.append(_tool_reply(call, f"Error: unknown tool {call.get('name')}"))
                    continue
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    request.append(_tool_reply(call, "Error: arguments were not valid JSON"))
                    continue
                request.append(_tool_reply(call, str(tool.run(args if isinstance(args, dict) else {}))))
        self.store.set_memory_review_turns(chat_id, turn_count(messages))
        return stored

    async def after_turn(self, *, chat_id: str, mode: str, memory: Memory,
                         client: ModelClient, now: datetime) -> list[dict]:
        """Run whichever opt-in passes are due, as notice events to show the user."""
        if mode == "chat" or not self.enabled:
            return []
        notices: list[dict] = []
        if self.settings.review and self.review_due(chat_id, self.store.get_messages(chat_id)):
            try:
                facts = await self.review(chat_id, memory, client)
            except (ModelError, OSError, ValueError) as exc:
                self.store.add_memory_diary(REVIEW_KIND, f"Review skipped: {exc}")
            else:
                notices += [{"type": "notice", "kind": REVIEW_KIND,
                             "notice": learned_notice(fact)} for fact in facts]
        if self.settings.consolidate and self.nightly_due(now):
            try:
                result = await run_consolidation(self.store, memory, client,
                                                 min_confidence=self.settings.min_confidence,
                                                 max_loss=self.settings.max_loss)
            except (ModelError, OSError, ValueError) as exc:
                self.store.add_memory_diary(CONSOLIDATE_KIND, f"Consolidation skipped: {exc}")
            else:
                if result.applied:
                    notices.append({
                        "type": "notice", "kind": CONSOLIDATE_KIND,
                        "notice": f"Consolidated memory: {'; '.join(result.applied)} "
                                  "(kova memory diary, kova memory undo)",
                    })
        return notices


def record_revision(store: Store, memory: Memory, slug: str, kind: str, *, before: str | None,
                    chat_id: str | None = None, detail: dict | None = None) -> int:
    """Record a note revision around a write a background pass just made.

    ``before`` is the note's previous file text, or ``None`` when the pass
    created it; the after text is read from disk so the record is always what
    actually landed.
    """
    path = memory.write_dir / f"{slug}.md"
    after = path.read_text() if path.is_file() else None
    return store.record_memory_revision(slug, kind, before=before, after=after,
                                        chat_id=chat_id, detail=detail)


def undo(memory: Memory, store: Store, *, revision_id: int | None = None,
         kind: str | None = None, limit: int = 20) -> list[str]:
    """Reverse recorded memory changes, newest first; returns the slugs touched.

    One revision puts one note back exactly as it was (``before``), or removes
    it again when the pass created it. Nothing is ever re-invented: the preimage
    is the file text that was replaced.
    """
    if revision_id is not None:
        found = store.get_memory_revision(revision_id)
        revisions = [found] if found else []
    else:
        revisions = store.memory_revisions(limit=limit, kind=kind, pending_only=True)
    touched: list[str] = []
    for revision in revisions:
        if revision["undone"]:
            continue
        if revision["before"] is None:
            if not memory.delete(revision["slug"]) and memory.get(revision["slug"]) is not None:
                continue
        elif not memory.restore(revision["slug"], revision["before"]):
            continue
        store.mark_memory_revision_undone(revision["id"])
        touched.append(revision["slug"])
    if touched:
        store.add_memory_diary(UNDO_KIND, f"Undid {len(touched)} memory change(s): {', '.join(touched)}")
    return touched


# -- #38: consolidation ---------------------------------------------------

CONSOLIDATE_INSTRUCTION = """You consolidate the user's memory notes. Reply with JSON only, no prose:
{"operations": [...]}, where each operation is one of
{"op": "merge", "slugs": ["a", "b"], "title": "...", "tags": [...], "body": "...",
 "evidence": "...", "confidence": 0.0, "reason": "..."}
{"op": "rewrite", "slug": "a", "title": "...", "tags": [...], "body": "...",
 "evidence": "...", "confidence": 0.0, "reason": "..."}
{"op": "forget", "slug": "a", "evidence": "...", "confidence": 0.0, "reason": "..."}
Rules:
1. Operations describe changes to notes that already exist; never invent notes or facts.
2. Every line of a new body must already appear verbatim in that operation's source notes.
   You may drop or regroup lines, never rewrite or paraphrase them.
3. evidence is a verbatim fragment of the source notes that justifies the operation.
4. confidence is your honest 0-1 score that the operation is safe and worth making.
5. Only propose an operation that clearly reduces duplication or keeps the store honest.
   Reply with {"operations": []} when there is nothing worth doing."""


def parse_operations(raw: str) -> list[Operation]:
    """Read the model's operation list; malformed entries are dropped, never guessed."""
    try:
        data = _first_json(raw)
    except ValueError:
        return []
    items: Iterable = []
    if isinstance(data, dict):
        items = data.get("operations") or []
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return []
    operations: list[Operation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("op") or item.get("kind") or "").strip().lower()
        if kind not in _OP_KINDS:
            continue
        raw_slugs = item.get("slugs")
        if raw_slugs is None:
            raw_slugs = [item.get("slug")] if item.get("slug") else []
        if not isinstance(raw_slugs, list):
            continue
        slugs = [str(slug) for slug in raw_slugs if isinstance(slug, str) and slug.strip()]
        tags = item.get("tags")
        tags = [str(tag) for tag in tags] if isinstance(tags, list) else []
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        operations.append(Operation(
            kind=kind, slugs=slugs, title=str(item.get("title") or "").strip(),
            tags=tags, body=str(item.get("body") or ""), confidence=confidence,
            evidence=str(item.get("evidence") or ""), reason=str(item.get("reason") or ""),
        ))
    return operations


def verbatim_only(body: str, sources: Sequence[str]) -> bool:
    """True when every non-blank line of ``body`` already exists in a source note.

    This is the anti-hallucination gate: consolidation may drop or regroup
    remembered text, but it can never write a sentence into memory that no note
    ever contained.
    """
    haystack = "\n".join(sources)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    return all(line in haystack for line in lines)


def _note_chars(title: str, body: str, tags: Sequence[str]) -> int:
    return len(title) + sum(len(tag) for tag in tags) + len(body)


def gate_operations(operations: Sequence[Operation], notes: Sequence[Note], *,
                    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                    max_loss: float = DEFAULT_MAX_LOSS) -> tuple[list[Operation], list[str]]:
    """Keep the operations that clear every gate, and explain the rest.

    Gates, in order: the operation names notes that exist and does not reuse a
    note; its confidence clears the score gate; its evidence appears verbatim
    in its sources; a new body is verbatim-only over those sources; and the
    whole run loses no more than ``max_loss`` of the store's characters.
    """
    by_slug = {note.slug: note for note in notes}
    accepted: list[Operation] = []
    refused: list[str] = []
    used: set[str] = set()
    for op in operations:
        sources = [by_slug.get(slug) for slug in op.slugs]
        if any(source is None for source in sources):
            refused.append(f"{op.kind}: mentions a note that does not exist ({', '.join(op.slugs)})")
            continue
        if len(set(op.slugs)) != len(op.slugs):
            refused.append(f"{op.kind}: names the same note twice")
            continue
        if used & set(op.slugs):
            refused.append(f"{op.kind}: reuses a note another operation already changed")
            continue
        if op.kind == "merge" and len(op.slugs) < 2:
            refused.append("merge: needs at least two existing notes")
            continue
        if op.kind == "forget" and len(op.slugs) != 1:
            refused.append("forget: forgets exactly one note")
            continue
        if op.confidence < min_confidence:
            refused.append(f"{op.kind} of {', '.join(op.slugs)}: confidence {op.confidence:.2f} "
                           f"is below the {min_confidence:.2f} gate")
            continue
        bodies = [source.body for source in sources if source is not None]
        if not op.evidence.strip():
            refused.append(f"{op.kind} of {', '.join(op.slugs)}: no evidence given")
            continue
        if not any(op.evidence in body for body in bodies) and not any(
                op.evidence in source.title for source in sources if source is not None):
            refused.append(f"{op.kind} of {', '.join(op.slugs)}: evidence is not in those notes")
            continue
        if op.kind in ("merge", "rewrite"):
            if not op.title:
                refused.append(f"{op.kind} of {', '.join(op.slugs)}: a title is required")
                continue
            if not verbatim_only(op.body, bodies):
                refused.append(f"{op.kind} of {', '.join(op.slugs)}: the new body contains text "
                               "that is not in those notes")
                continue
        used |= set(op.slugs)
        accepted.append(op)
    if not accepted:
        return [], refused
    before_chars = sum(_note_chars(note.title, note.body, note.tags) for note in notes)
    removed = 0
    for op in accepted:
        sources = [by_slug[slug] for slug in op.slugs]
        source_chars = sum(_note_chars(n.title, n.body, n.tags) for n in sources)
        if op.kind == "forget":
            removed += source_chars
        else:
            new_chars = _note_chars(op.title, op.body, op.tags)
            removed += max(0, source_chars - new_chars)
    cap = max_loss * before_chars
    if removed > cap:
        return [], [*refused, f"refused the whole run: it would drop {removed} of {before_chars} "
                              f"characters, over the {cap:.0f}-character cap "
                              f"(memory_consolidate_max_loss = {max_loss:.2f})"]
    return accepted, refused


def apply_operations(store: Store, memory: Memory,
                     operations: Sequence[Operation]) -> list[str]:
    """Write the accepted operations, recording a preimage and diary line each.

    A note that would exceed the budget is skipped with its reason recorded,
    rather than partially applied.
    """
    applied: list[str] = []
    for op in operations:
        sources = [note for slug in op.slugs if (note := memory.get(slug)) is not None]
        if len(sources) != len(op.slugs):
            continue
        preimages = {slug: (memory.write_dir / f"{slug}.md").read_text() for slug in op.slugs}
        detail = {"reason": op.reason, "evidence": op.evidence, "confidence": op.confidence}
        if op.kind == "forget":
            note = sources[0]
            store.record_memory_revision(note.slug, CONSOLIDATE_KIND, before=preimages[note.slug],
                                         after=None, detail=detail)
            if not memory.delete(note.slug):
                continue
            line = f"Forgot '{note.title}' ({op.reason or 'consolidation'})"
            store.add_memory_diary(CONSOLIDATE_KIND, line)
            applied.append(line)
            continue
        before_chars = sum(_note_chars(n.title, n.body, n.tags) for n in sources)
        try:
            if op.kind == "merge":
                note = memory.save(op.title, op.body, op.tags or None)
                for slug in op.slugs:
                    if slug != note.slug:
                        store.record_memory_revision(slug, CONSOLIDATE_KIND,
                                                     before=preimages[slug], after=None, detail=detail)
                        memory.delete(slug)
                new_slug = note.slug
            else:
                note = memory.update(op.slugs[0], title=op.title or None, body=op.body,
                                     tags=op.tags or None)
                new_slug = note.slug
        except ValueError as exc:  # over budget: leave every note untouched
            store.add_memory_diary(CONSOLIDATE_KIND,
                                   f"Skipped {op.kind} of {', '.join(op.slugs)}: {exc}")
            continue
        after_text = (memory.write_dir / f"{new_slug}.md").read_text()
        store.record_memory_revision(new_slug, CONSOLIDATE_KIND,
                                     before=preimages.get(new_slug), after=after_text, detail=detail)
        saved = before_chars - _note_chars(note.title, note.body, note.tags)
        verb = "Merged" if op.kind == "merge" else "Rewrote"
        line = (f"{verb} {', '.join(repr(slug) for slug in op.slugs)} into '{note.title}' "
                f"({saved:+d} chars)")
        store.add_memory_diary(CONSOLIDATE_KIND, line)
        applied.append(line)
    return applied


async def plan_consolidation(memory: Memory, client: ModelClient) -> list[Operation]:
    """Ask the model for operations over the whole store; it never writes anything."""
    notes = memory.list()
    if not notes:
        return []
    catalogue = [
        {"slug": note.slug, "title": note.title, "tags": note.tags, "body": note.body,
         "chars": _note_chars(note.title, note.body, note.tags),
         "origin": note.origin, "source": note.source, "created": note.created}
        for note in notes
    ]
    request = [{"role": "system", "content": CONSOLIDATE_INSTRUCTION},
               {"role": "user", "content": json.dumps(catalogue, ensure_ascii=False)}]
    _calls, text = await _ask(client, request, None)
    if not text.strip():
        return []
    return parse_operations(text)


async def run_consolidation(store: Store, memory: Memory, client: ModelClient, *,
                            min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                            max_loss: float = DEFAULT_MAX_LOSS) -> ConsolidationResult:
    """Plan, gate and apply one consolidation run; nothing happens when it is refused."""
    operations = await plan_consolidation(memory, client)
    if not operations:
        return ConsolidationResult()
    accepted, refused = gate_operations(operations, memory.list(),
                                        min_confidence=min_confidence, max_loss=max_loss)
    applied = apply_operations(store, memory, accepted) if accepted else []
    if not applied and refused:
        store.add_memory_diary(CONSOLIDATE_KIND, "Nothing applied: " + "; ".join(refused[:3]))
    return ConsolidationResult(operations=operations, refused=refused, applied=applied)


def diary_path(memory: Memory) -> Path:
    """Where a human can read the diary in the filesystem, next to the notes."""
    return memory.root / "DIARY.md"


def write_diary(memory: Memory, entries: Sequence[dict]) -> Path | None:
    """Mirror the diary into ``DIARY.md`` so it is readable without the CLI."""
    if not entries:
        return None
    lines = [f"- {entry['created']} · {entry['kind']}: {entry['entry']}" for entry in entries]
    path = diary_path(memory)
    try:
        path.write_text("# Memory diary\n\n" + "\n".join(lines) + "\n")
    except OSError:
        return None
    return path
