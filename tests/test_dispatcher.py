"""Tests for livecore.dispatcher.EventDispatcher."""

from __future__ import annotations

from livecore.dispatcher import EventDispatcher
from livecore.types import LiveEvent


def _ev(kind: str = "danmaku") -> LiveEvent:
    return LiveEvent(id="x", ts=0.0, kind=kind, room_id=1, text="x")  # type: ignore[arg-type]


def test_routes_by_kind():
    d = EventDispatcher()
    seen: list[str] = []

    def on_danmu(ev: LiveEvent) -> None:
        seen.append(("danmu", ev.text))

    def on_gift(ev: LiveEvent) -> None:
        seen.append(("gift", ev.text))

    d.on("danmaku", on_danmu)
    d.on("gift", on_gift)

    d.emit(_ev("danmaku"))
    d.emit(_ev("gift"))
    d.emit(_ev("enter"))

    assert seen == [("danmu", "x"), ("gift", "x")]


def test_wildcard_handler_runs_for_every_event():
    d = EventDispatcher()
    seen: list[str] = []

    d.on("*", lambda ev: seen.append(ev.kind))

    d.emit(_ev("danmaku"))
    d.emit(_ev("gift"))

    assert seen == ["danmaku", "gift"]


def test_multiple_handlers_for_same_kind():
    d = EventDispatcher()
    seen: list[int] = []

    d.on("danmaku", lambda _: seen.append(1))
    d.on("danmaku", lambda _: seen.append(2))

    d.emit(_ev("danmaku"))

    assert seen == [1, 2]
