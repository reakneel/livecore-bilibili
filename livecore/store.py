"""Phase 5.4 — SQLite state persistence.

Keeps a durable record of what was seen and what was said, so a restart can
rehydrate :class:`~livecore.context.RoomContext` instead of starting cold (which
would otherwise re-answer things it answered five minutes ago).

Disabled unless ``storage.enabled`` is set — nothing is written by default.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .types import LiveEvent, Suggestion

__all__ = ["SqliteStore", "StoredEvent", "StoredReply", "restore_context"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    room_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    user_name TEXT,
    text      TEXT,
    sentiment TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_room_ts ON events (room_id, ts);

CREATE TABLE IF NOT EXISTS replies (
    id      TEXT PRIMARY KEY,
    ts      REAL NOT NULL,
    room_id INTEGER NOT NULL,
    text    TEXT NOT NULL,
    source  TEXT,
    reason  TEXT,
    status  TEXT
);
CREATE INDEX IF NOT EXISTS idx_replies_room_ts ON replies (room_id, ts);
"""


@dataclass(slots=True)
class StoredEvent:
    id: str
    ts: float
    room_id: int
    kind: str
    user_name: str
    text: str
    sentiment: str


@dataclass(slots=True)
class StoredReply:
    id: str
    ts: float
    room_id: int
    text: str
    source: str
    reason: str
    status: str


class SqliteStore:
    """Thin, synchronous wrapper around :mod:`sqlite3`.

    Synchronous on purpose: writes are tiny and infrequent, and this keeps the
    hot async path free of a database driver dependency.
    """

    def __init__(self, path: str = "livecore.db") -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- lifecycle

    def open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SqliteStore:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- writes

    def save_event(self, ev: LiveEvent) -> None:
        conn = self.open()
        conn.execute(
            "INSERT OR REPLACE INTO events (id, ts, room_id, kind, user_name, text, sentiment)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.id,
                ev.ts or time.time(),
                ev.room_id,
                ev.kind,
                ev.user.name if ev.user else "",
                ev.text,
                ev.sentiment or "",
            ),
        )
        conn.commit()

    def save_suggestion(self, s: Suggestion, room_id: int = 0) -> None:
        conn = self.open()
        conn.execute(
            "INSERT OR REPLACE INTO replies (id, ts, room_id, text, source, reason, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (s.id, s.ts, room_id, s.text, s.source, s.reason, s.status),
        )
        conn.commit()

    def prune(self, retention_days: int = 7) -> int:
        """Delete rows older than ``retention_days``. Returns rows removed."""
        cutoff = time.time() - retention_days * 86400
        conn = self.open()
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        removed = cur.rowcount or 0
        cur = conn.execute("DELETE FROM replies WHERE ts < ?", (cutoff,))
        removed += cur.rowcount or 0
        conn.commit()
        return removed

    # ---------------------------------------------------------------- reads

    def recent_events(self, room_id: int, limit: int = 20) -> list[StoredEvent]:
        conn = self.open()
        rows = conn.execute(
            "SELECT id, ts, room_id, kind, user_name, text, sentiment FROM events"
            " WHERE room_id = ? ORDER BY ts DESC LIMIT ?",
            (room_id, limit),
        ).fetchall()
        return [StoredEvent(*row) for row in rows]

    def recent_replies(self, room_id: int, limit: int = 50) -> list[StoredReply]:
        conn = self.open()
        rows = conn.execute(
            "SELECT id, ts, room_id, text, source, reason, status FROM replies"
            " WHERE room_id = ? ORDER BY ts DESC LIMIT ?",
            (room_id, limit),
        ).fetchall()
        return [StoredReply(*row) for row in rows]


def restore_context(store: SqliteStore, room_id: int, ctx, within: float = 3600.0) -> int:
    """Re-seed a :class:`~livecore.context.RoomContext` from persisted replies.

    Only replies newer than ``within`` seconds matter — anything older would
    have aged out of the in-memory de-dupe window anyway. Returns how many
    entries were replayed.
    """
    cutoff = time.time() - within
    count = 0
    for row in store.recent_replies(room_id):
        if row.ts < cutoff:
            continue
        ctx.push_reply(Suggestion(id=row.id, ts=row.ts, text=row.text, reason=row.reason, source="rule"))  # type: ignore[arg-type]
        count += 1
    return count
