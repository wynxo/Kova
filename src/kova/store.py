"""SQLite backing store for chats, messages and reminders.

One connection shared across threads (``check_same_thread=False``) with a
single re-entrant lock around every query.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from kova.modes import DEFAULT_MODE, MODES
from kova.permissions import PERMISSIONS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_WORD = re.compile(r"\w+", re.UNICODE)

#: Messages the search index covers. Tool calls and tool output never match.
SEARCH_ROLES = ("user", "assistant")


def _search_terms(query: str) -> list[str]:
    """Query words, de-duplicated and capped, safe to put in an FTS query."""
    terms: list[str] = []
    seen: set[str] = set()
    for word in _WORD.findall(query or ""):
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(word)
        if len(terms) == 12:
            break
    return terms


def _like_pattern(term: str) -> str:
    """A ``%term%`` LIKE pattern with wildcards in the term escaped."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _clean_snippet(text: str) -> str:
    """One readable line: collapse whitespace FTS or a message may contain."""
    return " ".join((text or "").split())


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite build has the FTS5 extension compiled in."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _first_per_chat(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Keep only the first (best-ranked) match for each chat."""
    seen: set[str] = set()
    kept: list[sqlite3.Row] = []
    for row in rows:
        if row["chat_id"] in seen:
            continue
        seen.add(row["chat_id"])
        kept.append(row)
    return kept


def _like_snippet(content: str, terms: list[str], before: int = 60, after: int = 100) -> str:
    """A short window around the first hit in ``content``."""
    lower = content.lower()
    hits = [
        (position, term)
        for term in terms
        if (position := lower.find(term.lower())) != -1
    ]
    if not hits:
        return _clean_snippet(content[: before + after])
    position, term = min(hits, key=lambda hit: hit[0])
    start = max(0, position - before)
    end = min(len(content), position + len(term) + after)
    body = _clean_snippet(content[start:end])
    return ("…" if start else "") + body + ("…" if end < len(content) else "")


@dataclass
class Store:
    path: Path
    default_cwd: str | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_tables()
        self._setup_fts()

    # -- schema -----------------------------------------------------------
    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT,
                created TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'work',
                web INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                created TEXT
            );
            CREATE TABLE IF NOT EXISTS chat_compactions (
                chat_id TEXT PRIMARY KEY, cutoff INTEGER NOT NULL, summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_at TEXT NOT NULL,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                kind TEXT NOT NULL,
                before TEXT,
                after TEXT,
                chat_id TEXT,
                detail TEXT,
                undone INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                entry TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_reviews (
                chat_id TEXT PRIMARY KEY,
                turns INTEGER NOT NULL,
                created TEXT NOT NULL
            );
            """
        )
        # Older databases predate message timestamps; add and backfill them so
        # ``list_chats`` can show when a chat was last used.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if "created" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN created TEXT")
        self._conn.execute(
            "UPDATE messages SET created = "
            "(SELECT created FROM chats WHERE chats.id = messages.chat_id) "
            "WHERE created IS NULL"
        )
        # Same for chat modes and the web-search toggle; existing chats keep
        # today's behaviour (work mode, web on).
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(chats)")}
        if "mode" not in columns:
            self._conn.execute(f"ALTER TABLE chats ADD COLUMN mode TEXT NOT NULL DEFAULT '{DEFAULT_MODE}'")
        if "web" not in columns:
            self._conn.execute("ALTER TABLE chats ADD COLUMN web INTEGER NOT NULL DEFAULT 1")
        if "permission" not in columns:
            self._conn.execute("ALTER TABLE chats ADD COLUMN permission TEXT NOT NULL DEFAULT 'manual'")
        if "cwd" not in columns:
            self._conn.execute("ALTER TABLE chats ADD COLUMN cwd TEXT")
        self._conn.execute("UPDATE chats SET cwd = ? WHERE cwd IS NULL", (self.default_cwd or str(Path.home() / "Kova Files"),))
        if "plan" not in columns:
            self._conn.execute("ALTER TABLE chats ADD COLUMN plan TEXT NOT NULL DEFAULT '[]'")
        # Jobs share the reminders table: a job is a row with kind 'job', a repeat
        # schedule and either a shell command or a prompt. One table, one loop.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(reminders)")}
        for name, definition in (
            ("kind", "TEXT NOT NULL DEFAULT 'reminder'"),
            ("schedule", "TEXT"),
            ("command", "TEXT"),
            ("prompt", "TEXT"),
            ("chat_id", "TEXT"),
            ("idle_only", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "INTEGER NOT NULL DEFAULT 0"),
            ("last_run", "TEXT"),
        ):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE reminders ADD COLUMN {name} {definition}")
        self._conn.commit()

    def _setup_fts(self) -> None:
        """Create (or migrate) the full-text index and keep it in sync.

        FTS5 is optional: SQLite builds without it fall back to LIKE search.
        The table stores its own copy of the messages it indexes, so older
        databases get every existing message back-filled once. Triggers keep
        inserts and deletes aligned, and only user/assistant text is indexed.
        """
        self.fts_enabled = False
        if not _fts5_available(self._conn):
            return
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                "USING fts5(content, chat_id UNINDEXED, role UNINDEXED)"
            )
            self._conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages
                WHEN NEW.role IN ('user', 'assistant') AND NEW.content IS NOT NULL
                BEGIN
                    INSERT INTO messages_fts(rowid, content, chat_id, role)
                    VALUES (NEW.id, NEW.content, NEW.chat_id, NEW.role);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages
                BEGIN
                    DELETE FROM messages_fts WHERE rowid = OLD.id;
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages
                BEGIN
                    DELETE FROM messages_fts WHERE rowid = OLD.id;
                    INSERT INTO messages_fts(rowid, content, chat_id, role)
                    SELECT NEW.id, NEW.content, NEW.chat_id, NEW.role
                    WHERE NEW.role IN ('user', 'assistant') AND NEW.content IS NOT NULL;
                END;
                """
            )
            self._conn.execute(
                "INSERT INTO messages_fts(rowid, content, chat_id, role) "
                "SELECT m.id, m.content, m.chat_id, m.role FROM messages m "
                "WHERE m.role IN ('user', 'assistant') AND m.content IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM messages_fts f WHERE f.rowid = m.id)"
            )
            self._conn.commit()
            self.fts_enabled = True
        except sqlite3.OperationalError:
            # No FTS5 in this SQLite: remove any half-built pieces and use LIKE.
            self._conn.rollback()
            for statement in (
                "DROP TRIGGER IF EXISTS messages_fts_ai",
                "DROP TRIGGER IF EXISTS messages_fts_ad",
                "DROP TRIGGER IF EXISTS messages_fts_au",
                "DROP TABLE IF EXISTS messages_fts",
            ):
                try:
                    self._conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()
            self.fts_enabled = False

    # -- chats ------------------------------------------------------------
    def create_chat(self, title: str | None = None, mode: str = DEFAULT_MODE,
                    web: bool = True, permission: str = "manual", cwd: str | None = None) -> dict:
        if mode not in MODES:
            raise ValueError(f"unknown mode '{mode}'")
        if permission not in PERMISSIONS:
            raise ValueError(f"unknown permission '{permission}'")
        cwd = cwd or self.default_cwd or str(Path.home() / "Kova Files")
        with self._lock:
            cid = uuid4().hex[:12]
            created = _utc_now_iso()
            self._conn.execute(
                "INSERT INTO chats (id, title, created, mode, web, permission, cwd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, title, created, mode, web, permission, cwd),
            )
            self._conn.commit()
            return {"id": cid, "title": title, "created": created, "mode": mode, "web": web, "permission": permission, "cwd": cwd, "plan": []}

    def list_chats(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.title, c.created, c.mode, c.web, c.permission, c.cwd, COUNT(m.id) AS message_count, "
                "COALESCE(MAX(m.created), c.created) AS last_message "
                "FROM chats c LEFT JOIN messages m ON m.chat_id = c.id "
                "GROUP BY c.id "
                "ORDER BY last_message DESC, c.created DESC, c.id DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created": r["created"],
                "mode": r["mode"],
                "web": bool(r["web"]),
                "permission": r["permission"],
                "cwd": r["cwd"],
                "last_message": r["last_message"],
                "message_count": r["message_count"],
            }
            for r in rows
        ]

    def get_chat(self, chat_id: str, include_created: bool = False) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, created, mode, web, permission, cwd, plan FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "created": row["created"],
            "mode": row["mode"],
            "web": bool(row["web"]),
            "permission": row["permission"],
            "cwd": row["cwd"],
            "plan": json.loads(row["plan"]),
            "messages": self.get_messages(chat_id, include_created=include_created),
        }

    def update_chat(self, chat_id: str, title: str | None = None, mode: str | None = None,
                    web: bool | None = None, permission: str | None = None,
                    cwd: str | None = None, plan: list[dict] | None = None) -> None:
        """Change chat settings or its latest plan; ``None`` leaves a field alone."""
        if mode is not None and mode not in MODES:
            raise ValueError(f"unknown mode '{mode}'")
        if permission is not None and permission not in PERMISSIONS:
            raise ValueError(f"unknown permission '{permission}'")
        fields = {"title": title, "mode": mode, "web": web, "permission": permission, "cwd": cwd,
                  "plan": json.dumps(plan) if plan is not None else None}
        changes = {name: value for name, value in fields.items() if value is not None}
        if not changes:
            return
        assignments = ", ".join(f"{name} = ?" for name in changes)
        with self._lock:
            self._conn.execute(
                f"UPDATE chats SET {assignments} WHERE id = ?", (*changes.values(), chat_id)
            )
            self._conn.commit()

    def set_title(self, chat_id: str, title: str) -> None:
        self.update_chat(chat_id, title=title)

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM chat_compactions WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            cur = self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def clear_chats(self) -> None:
        """Delete every chat and message (used by a replacing import)."""
        with self._lock:
            self._conn.execute("DELETE FROM chat_compactions")
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM chats")
            self._conn.commit()

    def import_chat(self, chat: dict) -> bool:
        """Insert a chat and its messages, preserving ids and timestamps.

        Returns False when the chat id already exists, so a merging import
        never overwrites local history.
        """
        chat_id = chat.get("id")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("chat has no id")
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM chats WHERE id = ?", (chat_id,)
            ).fetchone():
                return False
            created = chat.get("created") or _utc_now_iso()
            mode = chat.get("mode")
            if mode not in MODES:
                mode = DEFAULT_MODE
            web = chat.get("web")
            web = 1 if web is None else int(bool(web))
            # A restored chat starts at Manual; backup data cannot silently
            # grant unattended command access on this machine.
            try:
                self._conn.execute(
                    "INSERT INTO chats (id, title, created, mode, web, cwd) VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, chat.get("title"), created, mode, web,
                     chat.get("cwd") or self.default_cwd or str(Path.home() / "Kova Files")),
                )
                for message in chat.get("messages", []):
                    if not isinstance(message, dict):
                        raise ValueError("message is not an object")
                    role = message.get("role")
                    if not isinstance(role, str) or not role:
                        raise ValueError("message has no role")
                    tool_calls = message.get("tool_calls")
                    if tool_calls is not None and not isinstance(tool_calls, str):
                        tool_calls = json.dumps(tool_calls)
                    self._conn.execute(
                        "INSERT INTO messages "
                        "(chat_id, role, content, tool_calls, tool_call_id, created) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            chat_id,
                            role,
                            message.get("content"),
                            tool_calls,
                            message.get("tool_call_id"),
                            message.get("created") or created,
                        ),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return True

    # -- search -----------------------------------------------------------
    def search_chats(self, query: str, limit: int = 20,
                     exclude_chat_id: str | None = None) -> list[dict]:
        """Chats with a user/assistant message matching ``query``, newest first.

        Each result carries the surrounding ``snippet`` and the role of the
        message it came from. FTS5 is used when it is compiled in; otherwise
        this falls back to a case-insensitive LIKE scan of the same messages.
        ``exclude_chat_id`` drops one chat (typically the current one) before
        the result cap is applied.
        """
        terms = _search_terms(query)
        if not terms:
            return []
        with self._lock:
            if self.fts_enabled:
                match = " OR ".join(f'"{term}"*' for term in terms)
                rows = self._conn.execute(
                    """
                    SELECT m.chat_id AS chat_id,
                           m.id AS message_id,
                           messages_fts.role AS role,
                           snippet(messages_fts, 0, '', '', '…', 12) AS snippet
                    FROM messages_fts
                    JOIN messages m ON m.id = messages_fts.rowid
                    WHERE messages_fts MATCH ?
                    ORDER BY bm25(messages_fts)
                    """,
                    (match,),
                ).fetchall()
                rows = _first_per_chat(rows)
            else:
                where = " OR ".join("m.content LIKE ? ESCAPE '\\'" for _ in terms)
                rows = self._conn.execute(
                    f"SELECT chat_id, message_id, role, content AS snippet FROM ("
                    "  SELECT m.chat_id AS chat_id, m.id AS message_id, m.role AS role,"
                    "         m.content AS content,"
                    "         ROW_NUMBER() OVER (PARTITION BY m.chat_id ORDER BY m.id DESC) AS rank"
                    "  FROM messages m"
                    "  WHERE m.role IN ('user', 'assistant') AND m.content IS NOT NULL"
                    f"   AND ({where})"
                    ") WHERE rank = 1",
                    tuple(_like_pattern(term) for term in terms),
                ).fetchall()

        chats = {chat["id"]: chat for chat in self.list_chats()}
        results: list[dict] = []
        for row in rows:
            chat = chats.get(row["chat_id"])
            if chat is None:
                continue
            snippet = row["snippet"] or ""
            if not self.fts_enabled:
                snippet = _like_snippet(snippet, terms)
            results.append(
                {
                    "id": chat["id"],
                    "title": chat["title"],
                    "created": chat["created"],
                    "last_message": chat["last_message"],
                    "message_count": chat["message_count"],
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "snippet": _clean_snippet(snippet),
                }
            )
        results.sort(
            key=lambda r: (r["last_message"] or "", r["created"] or "", r["id"]),
            reverse=True,
        )
        if exclude_chat_id is not None:
            results = [r for r in results if r["id"] != exclude_chat_id]
        return results[: max(1, limit)]

    # -- messages ---------------------------------------------------------
    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages "
                "(chat_id, role, content, tool_calls, tool_call_id, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    role,
                    content,
                    json.dumps(tool_calls) if tool_calls is not None else None,
                    tool_call_id,
                    _utc_now_iso(),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_messages(self, chat_id: str, include_created: bool = False) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, created "
                "FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        messages: list[dict] = []
        for role, content, tool_calls, tool_call_id, created in rows:
            message: dict = {"role": role, "content": content}
            if tool_calls is not None:
                message["tool_calls"] = json.loads(tool_calls)
            if tool_call_id is not None:
                message["tool_call_id"] = tool_call_id
            if include_created:
                message["created"] = created
            messages.append(message)
        return messages

    def context_snapshot(self, chat_id: str) -> tuple[str | None, int, list[tuple[int, dict]]]:
        """Return the stored summary and uncompressed messages with stable IDs."""
        with self._lock:
            compact = self._conn.execute("SELECT cutoff, summary FROM chat_compactions WHERE chat_id = ?", (chat_id,)).fetchone()
            cutoff, summary = (compact[0], compact[1]) if compact else (0, None)
            rows = self._conn.execute("SELECT id, role, content, tool_calls, tool_call_id FROM messages WHERE chat_id = ? AND id > ? ORDER BY id", (chat_id, cutoff)).fetchall()
        messages = []
        for row in rows:
            message = {"role": row[1], "content": row[2]}
            if row[3] is not None:
                message["tool_calls"] = json.loads(row[3])
            if row[4] is not None:
                message["tool_call_id"] = row[4]
            messages.append((row[0], message))
        return summary, cutoff, messages

    def get_context(self, chat_id: str) -> list[dict]:
        summary, _, rows = self.context_snapshot(chat_id)
        prefix = [{"role": "user", "content": "Conversation summary (earlier context):\n" + summary}] if summary else []
        return prefix + [message for _, message in rows]

    def save_compaction(self, chat_id: str, expected_cutoff: int, cutoff: int, summary: str) -> bool:
        with self._lock:
            current = self._conn.execute("SELECT cutoff FROM chat_compactions WHERE chat_id = ?", (chat_id,)).fetchone()
            if (current[0] if current else 0) != expected_cutoff:
                return False
            if not self._conn.execute("SELECT 1 FROM messages WHERE chat_id = ? AND id = ?", (chat_id, cutoff)).fetchone():
                return False
            self._conn.execute("INSERT INTO chat_compactions (chat_id, cutoff, summary) VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET cutoff=excluded.cutoff, summary=excluded.summary", (chat_id, cutoff, summary))
            self._conn.commit()
            return True

    # -- memory maintenance ------------------------------------------------
    def record_memory_revision(self, slug: str, kind: str, *, before: str | None = None,
                               after: str | None = None, chat_id: str | None = None,
                               detail: dict | None = None) -> int:
        """Store a memory preimage, so every write behind a background pass is reversible.

        ``before`` is the note's whole previous file text (``None`` when the
        note did not exist) and ``after`` what replaced it; ``detail`` carries
        whatever the caller needs to describe the change to a person.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_revisions (slug, kind, before, after, chat_id, detail, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, kind, before, after, chat_id,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                 _utc_now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def _revision_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "slug": row["slug"],
            "kind": row["kind"],
            "before": row["before"],
            "after": row["after"],
            "chat_id": row["chat_id"],
            "detail": json.loads(row["detail"]) if row["detail"] else None,
            "undone": bool(row["undone"]),
            "created": row["created"],
        }

    def get_memory_revision(self, revision_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_row(row) if row is not None else None

    def memory_revisions(self, *, limit: int = 50, kind: str | None = None,
                         pending_only: bool = False) -> list[dict]:
        """Recent memory revisions, newest first; ``pending_only`` drops undone ones."""
        sql = "SELECT * FROM memory_revisions"
        conditions, params = [], []
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        if pending_only:
            conditions.append("undone = 0")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._revision_row(row) for row in rows]

    def mark_memory_revision_undone(self, revision_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE memory_revisions SET undone = 1 WHERE id = ?", (revision_id,))
            self._conn.commit()

    def add_memory_diary(self, kind: str, entry: str) -> int:
        """One human-readable line of what a background pass did, and why."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_diary (kind, entry, created) VALUES (?, ?, ?)",
                (kind, entry, _utc_now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def memory_diary(self, *, limit: int = 50, kind: str | None = None) -> list[dict]:
        """Recent diary entries, oldest first, so they read as a log."""
        sql = "SELECT id, kind, entry, created FROM memory_diary"
        params: list = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "entry": r["entry"], "created": r["created"]}
                for r in reversed(rows)]

    def last_memory_diary(self, kind: str) -> dict | None:
        entries = self.memory_diary(limit=1, kind=kind)
        return entries[-1] if entries else None

    def memory_review_turns(self, chat_id: str) -> int:
        """How many user turns this chat had when it was last reviewed (0 if never)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT turns FROM memory_reviews WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row["turns"]) if row is not None else 0

    def set_memory_review_turns(self, chat_id: str, turns: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_reviews (chat_id, turns, created) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET turns = excluded.turns, created = excluded.created",
                (chat_id, turns, _utc_now_iso()),
            )
            self._conn.commit()

    # -- reminders --------------------------------------------------------
    def add_reminder(self, when: datetime, text: str) -> int:
        if when.tzinfo is None:
            raise ValueError("reminder time must be timezone-aware")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (reminder_at, text, done) VALUES (?, ?, 0)",
                (when.isoformat(), text),
            )
            self._conn.commit()
            return cur.lastrowid

    def _reminder_rows(self, include_done: bool) -> list[dict]:
        sql = "SELECT id, reminder_at, text, done FROM reminders WHERE kind = 'reminder'"
        if not include_done:
            sql += " AND done = 0"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        out = [
            {
                "id": r["id"],
                "when": r["reminder_at"],
                "text": r["text"],
                "done": bool(r["done"]),
            }
            for r in rows
        ]
        out.sort(key=lambda r: datetime.fromisoformat(r["when"]))
        return out

    def list_reminders(self, include_done: bool = False) -> list[dict]:
        return self._reminder_rows(include_done)

    def due_reminders(self, now: datetime) -> list[dict]:
        return [
            r
            for r in self._reminder_rows(False)
            if datetime.fromisoformat(r["when"]) <= now
        ]

    def mark_reminder_done(self, rid: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
            self._conn.commit()

    def delete_reminder(self, rid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
            self._conn.commit()
            return cur.rowcount > 0

    def clear_reminders(self) -> None:
        """Delete every one-shot reminder (used by a replacing import).

        Scheduled jobs are not part of a backup, so a restore never wipes them.
        """
        with self._lock:
            self._conn.execute("DELETE FROM reminders WHERE kind = 'reminder'")
            self._conn.commit()

    def import_reminder(self, when: str, text: str, done: bool = False) -> int:
        """Insert a reminder from a backup, keeping its done state."""
        stamp = datetime.fromisoformat(when)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (reminder_at, text, done) VALUES (?, ?, ?)",
                (stamp.isoformat(), text, 1 if done else 0),
            )
            self._conn.commit()
            return cur.lastrowid

    # -- jobs (recurring, in the same table as reminders) ------------------
    def add_job(self, text: str, schedule: str, next_at: datetime, *,
                command: str | None = None, prompt: str | None = None,
                idle_only: bool = False) -> int:
        """Schedule one recurring job: a shell command, or a prompt for one turn."""
        if next_at.tzinfo is None:
            raise ValueError("job time must be timezone-aware")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (reminder_at, text, done, kind, schedule, command, prompt, idle_only)"
                " VALUES (?, ?, 0, 'job', ?, ?, ?, ?)",
                (next_at.isoformat(), text, schedule, command, prompt, 1 if idle_only else 0),
            )
            self._conn.commit()
            return cur.lastrowid

    def _job_rows(self, include_done: bool = False) -> list[dict]:
        sql = (
            "SELECT id, reminder_at, text, done, schedule, command, prompt, chat_id, idle_only, runs, last_run"
            " FROM reminders WHERE kind = 'job'"
        )
        if not include_done:
            sql += " AND done = 0"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        out = [
            {
                "id": r["id"],
                "next_run": r["reminder_at"],
                "text": r["text"],
                "done": bool(r["done"]),
                "schedule": r["schedule"] or "",
                "kind": "script" if r["command"] else "prompt",
                "command": r["command"],
                "prompt": r["prompt"],
                "chat_id": r["chat_id"],
                "idle_only": bool(r["idle_only"]),
                "runs": r["runs"],
                "last_run": r["last_run"],
            }
            for r in rows
        ]
        out.sort(key=lambda r: datetime.fromisoformat(r["next_run"]))
        return out

    def list_jobs(self, include_done: bool = False) -> list[dict]:
        return self._job_rows(include_done)

    def due_jobs(self, now: datetime) -> list[dict]:
        return [j for j in self._job_rows(False) if datetime.fromisoformat(j["next_run"]) <= now]

    def mark_job_run(self, jid: int, next_at: datetime | None) -> None:
        """Record one run: schedule the next one, or finish the job with ``None``."""
        if next_at is None:
            sql, params = "UPDATE reminders SET runs = runs + 1, last_run = ?, done = 1 WHERE id = ?", (_utc_now_iso(), jid)
        else:
            if next_at.tzinfo is None:
                raise ValueError("job time must be timezone-aware")
            sql, params = ("UPDATE reminders SET runs = runs + 1, last_run = ?, reminder_at = ? WHERE id = ?",
                           (_utc_now_iso(), next_at.isoformat(), jid))
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def set_job_chat(self, jid: int, chat_id: str) -> None:
        """Remember the chat a prompt job runs in, so it keeps one thread."""
        with self._lock:
            self._conn.execute("UPDATE reminders SET chat_id = ? WHERE id = ? AND kind = 'job'", (chat_id, jid))
            self._conn.commit()

    def delete_job(self, jid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reminders WHERE id = ? AND kind = 'job'", (jid,))
            self._conn.commit()
            return cur.rowcount > 0
