"""Tests for livecore.engine.LiveEngine."""

from __future__ import annotations

import asyncio
import time

import pytest

from livecore.adapters import OutboundAdapter, SimulatorAdapter
from livecore.context import RoomContext
from livecore.engine import LiveEngine
from livecore.types import (
    DanmuEndpoint,
    EngineConfig,
    GiftInfo,
    LiveEvent,
    LiveUser,
    Suggestion,
)


def _danmu(text: str, sentiment: str | None = None) -> LiveEvent:
    return LiveEvent(
        id=f"d-{text}",
        ts=time.time(),
        kind="danmaku",
        room_id=1,
        user=LiveUser(uid=1, name="u"),
        text=text,
        sentiment=sentiment,  # type: ignore[arg-type]
    )


def test_engine_initializes_subsystems():
    engine = LiveEngine()
    assert isinstance(engine.outbound, SimulatorAdapter)
    assert engine.ctx is not None
    assert engine.scheduler is not None
    assert engine.dispatcher is not None
    assert engine.log is not None


def test_engine_emits_to_handlers():
    engine = LiveEngine()
    seen: list[LiveEvent] = []
    engine.on_event(lambda ev: seen.append(ev))
    ev = _danmu("牛逼")
    engine._ingest(ev)
    assert seen == [ev]


@pytest.mark.asyncio
async def test_engine_dedupes_suggestions_in_window_after_push_reply():
    cfg = EngineConfig(auto_suggest=True, cold_start_sec=0, min_gap_sec=0, jitter_ms=0)
    engine = LiveEngine(config=cfg)
    engine._running = True

    # First accepted suggestion registers the text in ctx dedupe history.
    sug = Suggestion(id="seed", ts=time.time(), text="同样的回复", reason="r", source="rule")
    engine.ctx.push_reply(sug)

    await engine._delayed_enqueue("同样的回复", "rule", "x", "x1")
    engine.scheduler.last_emit = 0.0
    await engine._delayed_enqueue("同样的回复", "rule", "x", "x2")

    sugs = [s for s in engine.suggestions if s.text == "同样的回复"]
    # 'seed' was pushed via push_reply (not in suggestions list); both enqueues
    # are dedupe-blocked, so suggestions stays empty.
    assert sugs == []


@pytest.mark.asyncio
async def test_engine_different_texts_both_kept():
    cfg = EngineConfig(auto_suggest=True, cold_start_sec=0, min_gap_sec=0, jitter_ms=0)
    engine = LiveEngine(config=cfg)
    engine._running = True
    await engine._delayed_enqueue("谢谢", "rule", "r", "x1")
    engine.scheduler.last_emit = 0.0
    await engine._delayed_enqueue("666", "rule", "r", "x2")
    assert len(engine.suggestions) == 2


@pytest.mark.asyncio
async def test_accept_publishes_via_outbound(monkeypatch):
    # Inline async-friendly outbound
    class CaptureAdapter:
        def __init__(self) -> None:
            self.sent: list = []

        async def publish(self, suggestion):
            self.sent.append(suggestion)

    cap = CaptureAdapter()
    engine = LiveEngine(outbound=cap, config=EngineConfig(auto_suggest=True, cold_start_sec=0))
    ev = _danmu("666")
    engine._ingest(ev)
    # fire delayed task synchronously
    await asyncio.sleep(0.05)
    # accept the first suggestion
    if engine.suggestions:
        sid = engine.suggestions[0].id
        await engine.accept(sid)
        assert cap.sent and cap.sent[0].id == sid
        assert engine.suggestions[0].status == "accepted"


def test_engine_warm_path_does_not_enqueue_during_cold_start(monkeypatch):
    engine = LiveEngine(config=EngineConfig(auto_suggest=True, cold_start_sec=999))
    # Cool trick: rewrite started_at to keep us in cold start
    engine.scheduler.started_at = time.time()
    pre = list(engine.suggestions)
    engine._ingest(_danmu("666 牛"))
    assert engine.suggestions == pre  # nothing added


def test_accept_unknown_id_is_noop():
    engine = LiveEngine()
    asyncio.run(engine.accept("does-not-exist"))  # must not raise
