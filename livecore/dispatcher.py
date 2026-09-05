from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from .types import EventKind, LiveEvent

Handler = Callable[[LiveEvent], None]


class EventDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[EventKind | str, list[Handler]] = defaultdict(list)

    def on(self, kind: EventKind | str, fn: Handler) -> None:
        self._handlers[kind].append(fn)

    def emit(self, ev: LiveEvent) -> None:
        for fn in self._handlers.get(ev.kind, ()):
            fn(ev)
        for fn in self._handlers.get("*", ()):
            fn(ev)
