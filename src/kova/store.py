"""SQLite backing store for chats, messages and reminders.

One connection shared across threads (``check_same_thread=False``) with a
single re-entrant lock around every query.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
# Attachment ids are uuid4().hex. Matching the whole token avoids keeping a
# file because its id happens to appear inside an unrelated hex string.
_ATTACHMENT_ID = re.compile(r"\b[0-9a-f]{32}\b")

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
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, name TEXT NOT NULL,
                mime TEXT NOT NULL, size INTEGER NOT NULL, pages INTEGER,
                sha256 TEXT NOT NULL, path TEXT NOT NULL, created TEXT,
                used INTEGER NOT NULL DEFAULT 0
            );
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
            CREATE TABLE IF NOT EXISTS message_attachments (
                message_id INTEGER NOT NULL,
                attachment_id TEXT NOT NULL,
                name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (message_id, attachment_id)
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
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                created TEXT NOT NULL,
                label TEXT,
                root TEXT NOT NULL,
                files TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queued_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                model TEXT,
                created TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS share_drafts (
                chat_id TEXT PRIMARY KEY
            );
            """
        )
        # Older databases predate message timestamps; add and backfill them so
        # ``list_chats`` can show when a chat was last used.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(attachments)")}
        if "created" not in columns:
            self._conn.execute("ALTER TABLE attachments ADD COLUMN created TEXT")
        if "used" not in columns:
            self._conn.execute("ALTER TABLE attachments ADD COLUMN used INTEGER NOT NULL DEFAULT 0")
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

    def fork_chat(self, chat_id: str) -> dict | None:
        """Atomically clone a conversation, including its compacted context."""
        with self._lock:
            source = self._conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
            if source is None:
                return None
            cid, created = uuid4().hex[:12], _utc_now_iso()
            copied_dir = self.path.parent / "attachments" / cid
            try:
                attachment_ids: dict[str, str] = {}
                for attachment in self.attachments(chat_id):
                    ident = uuid4().hex
                    copied_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path = copied_dir / (ident + Path(attachment["path"]).suffix)
                    with open(attachment["path"], "rb") as src:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    attachment_ids[attachment["id"]] = ident
                    self._conn.execute(
                        "INSERT INTO attachments (id, chat_id, name, mime, size, pages, sha256, path, created, used) "
                        "VALUES (:id,:chat_id,:name,:mime,:size,:pages,:sha256,:path,:created,:used)",
                        {**attachment, "id": ident, "chat_id": cid, "path": str(path)})

                def remap(value: str | None) -> str | None:
                    if value is None:
                        return None
                    for old, new in attachment_ids.items():
                        value = value.replace(old, new)
                    return value

                self._conn.execute(
                    "INSERT INTO chats (id, title, created, mode, web, permission, cwd, plan) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, (source['title'] or 'Untitled') + ' (fork)', created, source['mode'],
                     source['web'], source['permission'], source['cwd'], remap(source['plan'])))
                rows = self._conn.execute("SELECT id, role, content, tool_calls, tool_call_id, created FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
                ids = {}
                for row in rows:
                    cur = self._conn.execute("INSERT INTO messages (chat_id, role, content, tool_calls, tool_call_id, created) VALUES (?, ?, ?, ?, ?, ?)",
                                             (cid, row['role'], remap(row['content']), remap(row['tool_calls']), row['tool_call_id'], row['created']))
                    ids[row['id']] = cur.lastrowid
                links = self._conn.execute(
                    "SELECT ma.message_id, ma.attachment_id, ma.name, ma.position "
                    "FROM message_attachments ma JOIN messages m ON m.id = ma.message_id "
                    "WHERE m.chat_id = ? ORDER BY ma.message_id, ma.position", (chat_id,)).fetchall()
                for link in links:
                    if link['message_id'] in ids:
                        self._conn.execute(
                            "INSERT INTO message_attachments (message_id, attachment_id, name, position) VALUES (?, ?, ?, ?)",
                            (ids[link['message_id']],
                             attachment_ids.get(link['attachment_id'], link['attachment_id']),
                             link['name'], link['position']))
                compact = self._conn.execute("SELECT cutoff, summary FROM chat_compactions WHERE chat_id = ?", (chat_id,)).fetchone()
                if compact and compact['cutoff'] in ids:
                    self._conn.execute("INSERT INTO chat_compactions (chat_id, cutoff, summary) VALUES (?, ?, ?)",
                                       (cid, ids[compact['cutoff']], remap(compact['summary'])))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                shutil.rmtree(copied_dir, ignore_errors=True)
                raise
        return self.get_chat(cid)

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

    def add_attachment(self, row: dict) -> None:
        row = {"created": None, "used": 0, **row}
        with self._lock:
            self._conn.execute(
                "INSERT INTO attachments (id, chat_id, name, mime, size, pages, sha256, path, created, used) "
                "VALUES (:id,:chat_id,:name,:mime,:size,:pages,:sha256,:path,:created,:used)", row)
            self._conn.commit()

    def attachment_rows(self) -> list[dict]:
        """Every stored attachment, across all chats (for quota and cleanup)."""
        with self._lock:
            return [dict(row) for row in self._conn.execute("SELECT * FROM attachments")]

    def attachments(self, chat_id: str) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute("SELECT * FROM attachments WHERE chat_id = ?", (chat_id,))]

    def mark_attachments_used(self, chat_id: str, ids: list[str]) -> None:
        """Retain these uploads: a persisted message has taken responsibility for them."""
        unique = list(dict.fromkeys(ids))
        if not unique:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE attachments SET used = 1 WHERE chat_id = ? AND id = ?",
                [(chat_id, ident) for ident in unique])
            self._conn.commit()

    def add_share_draft(self, chat_id: str) -> None:
        """Remember a share's draft chat until the share is redeemed or expires.

        Share tokens deliberately live only in memory, so a restart makes every
        unredeemed draft unreachable. The journal lets the next startup find
        and remove those chats instead of leaving empty conversations behind.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO share_drafts (chat_id) VALUES (?)", (chat_id,))
            self._conn.commit()

    def drop_share_draft(self, chat_id: str) -> None:
        """Forget a draft journal entry once the share is handed off."""
        with self._lock:
            self._conn.execute("DELETE FROM share_drafts WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    def reap_share_drafts(self) -> list[str]:
        """Clear the journal and return draft chats with no saved messages.

        Called once at startup: every journal entry belongs to a share token
        that did not survive the restart. A draft that already has a message
        is a real conversation (a crash can land between redemption and the
        journal update), so it is kept.
        """
        with self._lock:
            ids = [row["chat_id"] for row in self._conn.execute(
                "SELECT chat_id FROM share_drafts")]
            self._conn.execute("DELETE FROM share_drafts")
            self._conn.commit()
            return [chat_id for chat_id in ids if not self._conn.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone()]

    def referenced_attachment_ids(self, chat_id: str) -> set[str]:
        """Every attachment id a saved chat still uses.

        The explicit ``message_attachments`` link is the authoritative record.
        Older transcripts spell the id out in a message, tool call or summary;
        that scan stays as a fallback for rows created before ``used`` existed
        and for chats restored from a backup. Cleanup therefore never expires
        an upload that a saved message still references, even when a merge or an
        older database leaves ``used`` at zero.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT content, tool_calls FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchall()
            summary = self._conn.execute(
                "SELECT summary FROM chat_compactions WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            # This table arrives with the name-only message link. Keep working
            # when this branch runs on a database that predates it.
            links = []
            if self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_attachments'"
            ).fetchone():
                links = self._conn.execute(
                    "SELECT ma.attachment_id FROM message_attachments ma "
                    "JOIN messages m ON m.id = ma.message_id WHERE m.chat_id = ?",
                    (chat_id,),
                ).fetchall()
        parts = [row[name] for row in rows for name in ("content", "tool_calls")]
        if summary is not None:
            parts.append(summary["summary"])
        found = set(_ATTACHMENT_ID.findall("\n".join(part for part in parts if part)))
        found.update(link["attachment_id"] for link in links)
        return found

    def delete_attachment(self, chat_id: str, ident: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT path FROM attachments WHERE chat_id = ? AND id = ?", (chat_id, ident)).fetchone()
            if row is None:
                return False
            path = Path(row['path'])
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
            self._conn.execute("DELETE FROM attachments WHERE id = ?", (ident,))
            self._conn.commit()
            return True

    def delete_chat(self, chat_id: str) -> bool:
        for row in self.attachments(chat_id):
            self.delete_attachment(chat_id, row["id"])
        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', chat_id):
            try:
                (self.path.parent / 'attachments' / chat_id).rmdir()
            except OSError:
                pass
        with self._lock:
            self._conn.execute("DELETE FROM chat_compactions WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM share_drafts WHERE chat_id = ?", (chat_id,))
            self._conn.execute(
                "DELETE FROM message_attachments WHERE message_id IN "
                "(SELECT id FROM messages WHERE chat_id = ?)", (chat_id,))
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM checkpoints WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM queued_messages WHERE chat_id = ?", (chat_id,))
            cur = self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def clear_chats(self) -> None:
        """Delete every chat and message (used by a replacing import)."""
        for chat in self.list_chats():
            for row in self.attachments(chat["id"]):
                self.delete_attachment(chat["id"], row["id"])
        with self._lock:
            self._conn.execute("DELETE FROM chat_compactions")
            self._conn.execute("DELETE FROM share_drafts")
            self._conn.execute("DELETE FROM message_attachments")
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM checkpoints")
            self._conn.execute("DELETE FROM queued_messages")
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
                    cur = self._conn.execute(
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
                    rows = message.get("attachments")
                    if isinstance(rows, list):
                        self._add_message_attachments(cur.lastrowid, rows)
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

    def link_message_attachments(self, message_id: int, rows: list[dict]) -> None:
        """Record which uploaded files a message used, without storing their text."""
        with self._lock:
            self._add_message_attachments(message_id, rows)
            self._conn.commit()

    def _add_message_attachments(self, message_id: int, rows: list[dict]) -> None:
        for position, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name, ident = row.get('name'), row.get('id')
            if not isinstance(name, str) or not name:
                continue
            ident = ident if isinstance(ident, str) and ident else f'import-{message_id}-{position}'
            self._conn.execute(
                "INSERT OR REPLACE INTO message_attachments "
                "(message_id, attachment_id, name, position) VALUES (?, ?, ?, ?)",
                (message_id, ident, name, position),
            )

    def get_messages(self, chat_id: str, include_created: bool = False) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, tool_calls, tool_call_id, created "
                "FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
            links = self._conn.execute(
                "SELECT ma.message_id, ma.attachment_id, ma.name "
                "FROM message_attachments ma JOIN messages m ON m.id = ma.message_id "
                "WHERE m.chat_id = ? ORDER BY ma.message_id, ma.position",
                (chat_id,),
            ).fetchall()
        attached: dict[int, list[dict]] = {}
        for link in links:
            attached.setdefault(link["message_id"], []).append(
                {"id": link["attachment_id"], "name": link["name"]}
            )
        messages: list[dict] = []
        for message_id, role, content, tool_calls, tool_call_id, created in rows:
            message: dict = {"role": role, "content": content}
            if tool_calls is not None:
                message["tool_calls"] = json.loads(tool_calls)
            if tool_call_id is not None:
                message["tool_call_id"] = tool_call_id
            if include_created:
                message["created"] = created
            if message_id in attached:
                message["attachments"] = attached[message_id]
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

    def get_context_entries(self, chat_id: str) -> list[tuple[int | None, dict]]:
        """Like :meth:`get_context`, but keep each stored message's row id.

        Callers use the id to attach turn-only content to the message they just
        wrote without changing what is stored. The compacted summary has no
        row, so its ``None`` id marks it as not a stored message.
        """
        summary, _, rows = self.context_snapshot(chat_id)
        prefix = [(None, {"role": "user", "content": "Conversation summary (earlier context):\n" + summary})] if summary else []
        return prefix + rows

    def get_context(self, chat_id: str) -> list[dict]:
        return [message for _, message in self.get_context_entries(chat_id)]

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

    # -- queued messages --------------------------------------------------
    def queue_message(self, chat_id: str, text: str, model: str | None = None) -> int:
        """Persist a message to run after the chat's current turn ends."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO queued_messages (chat_id, text, model, created) VALUES (?, ?, ?, ?)",
                (chat_id, text, model, _utc_now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

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

    def queued_messages(self, chat_id: str) -> list[dict]:
        """The chat's queued messages, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, model, created FROM queued_messages "
                "WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
        return [dict(row) for row in rows]

    def pop_queued_message(self, chat_id: str) -> dict | None:
        """Remove and return the oldest queued message for a chat."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, text, model, created FROM queued_messages "
                "WHERE chat_id = ? ORDER BY id LIMIT 1", (chat_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM queued_messages WHERE id = ?", (row["id"],))
            self._conn.commit()
            return dict(row)

    def delete_queued_message(self, chat_id: str, message_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM queued_messages WHERE id = ? AND chat_id = ?",
                (message_id, chat_id))
            self._conn.commit()
            return cur.rowcount > 0

    def clear_queued(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM queued_messages WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    # -- checkpoints ------------------------------------------------------
    def add_checkpoint(self, chat_id: str, root: str, files: dict, label: str | None = None) -> int:
        """Record one turn's changed files: path -> before/after digests and stats."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO checkpoints (chat_id, created, label, root, files) VALUES (?, ?, ?, ?, ?)",
                (chat_id, _utc_now_iso(), label, root, json.dumps(files)),
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

    def _checkpoint(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "chat_id": row["chat_id"], "created": row["created"],
                "label": row["label"], "root": row["root"], "files": json.loads(row["files"])}

    def list_checkpoints(self, chat_id: str) -> list[dict]:
        """One chat's checkpoints, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, created, label, root, files FROM checkpoints "
                "WHERE chat_id = ? ORDER BY id DESC", (chat_id,)).fetchall()
        return [self._checkpoint(row) for row in rows]

    def get_checkpoint(self, chat_id: str, checkpoint_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, chat_id, created, label, root, files FROM checkpoints "
                "WHERE chat_id = ? AND id = ?", (chat_id, checkpoint_id)).fetchone()
        return self._checkpoint(row) if row is not None else None

    def checkpoints_from(self, chat_id: str, checkpoint_id: int) -> list[dict]:
        """Checkpoints at or after one id, oldest first, for a restore plan."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, created, label, root, files FROM checkpoints "
                "WHERE chat_id = ? AND id >= ? ORDER BY id", (chat_id, checkpoint_id)).fetchall()
        return [self._checkpoint(row) for row in rows]

    def delete_checkpoints(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM checkpoints WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

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
            self._conn.execute("UPDATE reminders SET done = 1 WHERE id = ? AND kind = 'reminder'", (rid,))
            self._conn.commit()

    def delete_reminder(self, rid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reminders WHERE id = ? AND kind = 'reminder'", (rid,))
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
