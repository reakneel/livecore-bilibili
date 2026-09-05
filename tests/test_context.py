"""Tests for livecore.context.RoomContext."""

from __future__ import annotations

import time

import pytest

from livecore.context import RoomContext
from livecore.types import GiftInfo, LiveEvent, LiveUser, Suggestion


def _ev(kind: str = "danmaku", text: str = "hello", ts: float | None = None) -> LiveEvent:
    return LiveEvent(
        id=f"id-{ts or time.time()}",
        ts=ts if ts is not None else time.time(),
        kind=kind,  # type: ignore[arg-type]
        room_id=1,
        user=LiveUser(uid=1, name="tester"),
        text=text,
    )


def test_push_event_trims_to_max():
    ctx = RoomContext(max_events=3)
    for i in range(5):
        ctx.push_event(_ev(text=f"msg-{i}"))
    assert len(ctx._recent) == 3
    assert ctx._recent[-1].text == "msg-4"


def test_already_said_window():
    ctx = RoomContext()
    s = Suggestion(id="x", ts=time.time(), text="hi", reason="r", source="rule")
    ctx.push_reply(s)
    assert ctx.already_said("hi", within=60) is True
    assert ctx.already_said("other", within=60) is False


def test_activity_per_minute_counts_non_popularity():
    ctx = RoomContext()
    base = time.time()
    for ts in (base - 30, base - 10, base - 100):
        ctx.push_event(_ev(ts=ts))
    ctx.push_event(_ev(kind="popularity", ts=base))
    assert ctx.activity_per_minute() == 2


def test_transcript_includes_danmaku_gift_superchat_only():
    ctx = RoomContext()
    ctx.push_event(_ev(kind="danmaku", text="弹幕"))
    ctx.push_event(_ev(kind="popularity", text="2000"))
    ctx.push_event(_ev(kind="enter", text="user"))
    out = ctx.transcript()
    assert "弹幕" in out
    assert "2000" not in out
    assert "user" not in out
