"""SQLite backing store for chats, messages and reminders.

One connection shared across threads (``check_same_thread=False``) with a
single re-entrant lock around every query.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Store:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_tables()

    # -- schema -----------------------------------------------------------
    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT,
                created TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_at TEXT NOT NULL,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
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
        self._conn.commit()

    # -- chats ------------------------------------------------------------
    def create_chat(self, title: str | None = None) -> dict:
        with self._lock:
            cid = uuid4().hex[:12]
            created = _utc_now_iso()
            self._conn.execute(
                "INSERT INTO chats (id, title, created) VALUES (?, ?, ?)",
                (cid, title, created),
            )
            self._conn.commit()
            return {"id": cid, "title": title, "created": created}

    def list_chats(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.title, c.created, COUNT(m.id) AS message_count, "
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
                "last_message": r["last_message"],
                "message_count": r["message_count"],
            }
            for r in rows
        ]

    def get_chat(self, chat_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, created FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "created": row["created"],
            "messages": self.get_messages(chat_id),
        }

    def set_title(self, chat_id: str, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chats SET title = ? WHERE id = ?", (title, chat_id)
            )
            self._conn.commit()

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            cur = self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self._conn.commit()
            return cur.rowcount > 0

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

    def get_messages(self, chat_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_calls, tool_call_id "
                "FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        messages: list[dict] = []
        for role, content, tool_calls, tool_call_id in rows:
            message: dict = {"role": role, "content": content}
            if tool_calls is not None:
                message["tool_calls"] = json.loads(tool_calls)
            if tool_call_id is not None:
                message["tool_call_id"] = tool_call_id
            messages.append(message)
        return messages

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
        sql = "SELECT id, reminder_at, text, done FROM reminders"
        if not include_done:
            sql += " WHERE done = 0"
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
