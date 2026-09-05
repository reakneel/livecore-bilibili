from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Literal

Level = Literal["debug", "info", "warn", "error"]
Layer = Literal["infra", "net", "msg", "ai", "behavior", "watch"]


@dataclass(slots=True)
class LogEntry:
    ts: float
    level: Level
    layer: Layer
    message: str


class RingLogger:
    def __init__(self, maxlen: int = 200) -> None:
        self._items: deque[LogEntry] = deque(maxlen=maxlen)
        self._listeners: list[Callable[[LogEntry], None]] = []

    def on(self, fn: Callable[[LogEntry], None]) -> None:
        self._listeners.append(fn)

    def push(self, level: Level, layer: Layer, message: str) -> LogEntry:
        entry = LogEntry(ts=time.time(), level=level, layer=layer, message=message)
        self._items.append(entry)
        for fn in self._listeners:
            fn(entry)
        return entry

    def snapshot(self) -> list[LogEntry]:
        return list(self._items)
