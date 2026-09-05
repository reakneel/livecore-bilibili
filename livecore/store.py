"""Phase 5.4 — durable SQLite state persistence.

The public sync methods remain available for simple callers, while async wrappers
run database work in a worker thread so SQLite never blocks the asyncio event
loop. A single connection is protected by a lock because the async engine may
schedule several persistence operations concurrently.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass

from .types import LiveEvent, Suggestion

__all__ = ["SqliteStore", "StoredEvent", "StoredReply", "restore_context"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, room_id INTEGER NOT NULL,
    kind TEXT NOT NULL, user_name TEXT, text TEXT, sentiment TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_room_ts ON events (room_id, ts);
CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, room_id INTEGER NOT NULL,
    text TEXT NOT NULL, source TEXT, reason TEXT, status TEXT, in_reply_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_replies_room_ts ON replies (room_id, ts);
"""


@dataclass(slots=True)
class StoredEvent:
    id: str; ts: float; room_id: int; kind: str; user_name: str; text: str; sentiment: str


@dataclass(slots=True)
class StoredReply:
    id: str; ts: float; room_id: int; text: str; source: str; reason: str; status: str; in_reply_to: str = ""


class SqliteStore:
    """Thread-safe SQLite store with non-blocking async adapters."""

    def __init__(self, path: str = "livecore.db") -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.path, check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.executescript(_SCHEMA)
                # Migrate databases created by the earlier Phase 5 schema.
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(replies)")}
                if "in_reply_to" not in columns:
                    self._conn.execute("ALTER TABLE replies ADD COLUMN in_reply_to TEXT DEFAULT ''")
                self._conn.commit()
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "SqliteStore":
        self.open(); return self

    def __exit__(self, *exc) -> None:
        self.close()

    def save_event(self, ev: LiveEvent) -> None:
        with self._lock:
            conn = self.open()
            conn.execute(
                "INSERT OR REPLACE INTO events (id, ts, room_id, kind, user_name, text, sentiment) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ev.id, ev.ts or time.time(), ev.room_id, ev.kind, ev.user.name if ev.user else "", ev.text, ev.sentiment or ""),
            )
            conn.commit()

    async def save_event_async(self, ev: LiveEvent) -> None:
        await asyncio.to_thread(self.save_event, ev)

    def save_suggestion(self, s: Suggestion, room_id: int = 0) -> None:
        with self._lock:
            conn = self.open()
            conn.execute(
                "INSERT OR REPLACE INTO replies (id, ts, room_id, text, source, reason, status, in_reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (s.id, s.ts, room_id, s.text, s.source, s.reason, s.status, s.in_reply_to),
            )
            conn.commit()

    async def save_suggestion_async(self, s: Suggestion, room_id: int = 0) -> None:
        await asyncio.to_thread(self.save_suggestion, s, room_id)

    def prune(self, retention_days: int = 7) -> int:
        with self._lock:
            conn = self.open()
            cutoff = time.time() - retention_days * 86400
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)); removed = cur.rowcount or 0
            cur = conn.execute("DELETE FROM replies WHERE ts < ?", (cutoff,)); removed += cur.rowcount or 0
            conn.commit(); return removed

    def recent_events(self, room_id: int, limit: int = 20) -> list[StoredEvent]:
        with self._lock:
            rows = self.open().execute(
                "SELECT id, ts, room_id, kind, user_name, text, sentiment FROM events WHERE room_id = ? ORDER BY ts DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        return [StoredEvent(*row) for row in rows]

    def recent_replies(self, room_id: int, limit: int = 50) -> list[StoredReply]:
        with self._lock:
            rows = self.open().execute(
                "SELECT id, ts, room_id, text, source, reason, status, in_reply_to FROM replies WHERE room_id = ? ORDER BY ts DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        return [StoredReply(*row) for row in rows]


def restore_context(store: SqliteStore, room_id: int, ctx, within: float = 3600.0) -> int:
    cutoff = time.time() - within
    count = 0
    for row in store.recent_replies(room_id):
        if row.ts < cutoff:
            continue
        ctx.push_reply(Suggestion(
            id=row.id, ts=row.ts, text=row.text, reason=row.reason,
            source=row.source, in_reply_to=row.in_reply_to, status=row.status,
        ))
        count += 1
    return count
