from __future__ import annotations

import time

from .types import LiveEvent, Suggestion


class RoomContext:
    def __init__(self, max_events: int = 20) -> None:
        self._recent: list[LiveEvent] = []
        self._last_text: dict[str, float] = {}
        self._max = max_events

    def push_event(self, ev: LiveEvent) -> None:
        self._recent = [*self._recent[-(self._max - 1) :], ev]

    def push_reply(self, s: Suggestion) -> None:
        self._last_text[s.text] = s.ts

    def already_said(self, text: str, within: float = 90.0) -> bool:
        last = self._last_text.get(text)
        return last is not None and time.time() - last < within

    def activity_per_minute(self) -> int:
        since = time.time() - 60
        return sum(1 for e in self._recent if e.ts >= since and e.kind != "popularity")

    def transcript(self, limit: int = 10) -> str:
        rows = [e for e in self._recent if e.kind in {"danmaku", "gift", "superchat"}][-limit:]
        return "\n".join(f"{(e.user.name if e.user else '系统')}: {e.text}" for e in rows)
