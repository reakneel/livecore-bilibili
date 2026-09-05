"""Tests for livecore.multi — Phase 5.2 multi-room supervision."""

from __future__ import annotations

import asyncio
import json

import pytest

from livecore.alert import Alerter
from livecore.config import AlertConfig, ConfigStore
from livecore.multi import RoomSupervisor
from livecore.types import EngineConfig


def test_add_room_creates_isolated_engines():
    sup = RoomSupervisor(); a = sup.add_room(101); b = sup.add_room(202)
    assert a is not b and a.ctx is not b.ctx and a.scheduler is not b.scheduler and a.watch is not b.watch
    assert sup.watch_for(101) is not sup.watch_for(202)


def test_add_room_is_idempotent():
    sup = RoomSupervisor(); first = sup.add_room(101)
    assert sup.add_room(101) is first and len(sup.room_ids) == 1


def test_rooms_from_constructor():
    assert sorted(RoomSupervisor(rooms=[1, 2, 3]).room_ids) == [1, 2, 3]


def test_contexts_are_not_shared_between_rooms():
    from livecore.types import LiveEvent, LiveUser
    sup = RoomSupervisor(); a, b = sup.add_room(101), sup.add_room(202)
    a.ctx.push_event(LiveEvent(id="e1", ts=0, kind="danmaku", room_id=101, user=LiveUser(uid=1, name="x"), text="hi"))
    assert len(a.ctx._recent) == 1 and b.ctx._recent == []


def test_engine_for_and_engines_accessors():
    sup = RoomSupervisor(rooms=[5]); assert sup.engine_for(5) is not None and sup.engine_for(999) is None
    assert set(sup.engines) == {5}


@pytest.mark.asyncio
async def test_start_all_isolates_room_failures():
    class Boom(RoomSupervisor):
        async def _start_one(self, room_id):
            if room_id == 2: raise RuntimeError("boom")
            self.started.append(room_id)
        def __init__(self, *a, **kw): self.started = []; super().__init__(*a, **kw)
    sup = Boom(rooms=[1, 2, 3]); await sup.start_all(); assert sup.started == [1, 3]


@pytest.mark.asyncio
async def test_start_all_records_failure_in_log():
    sup = RoomSupervisor(rooms=[101], alerter=Alerter(AlertConfig(enabled=False))); engine = sup.engine_for(101)
    async def boom(_room_id): raise RuntimeError("连接失败")
    engine.start_bilibili = boom  # type: ignore[method-assign]
    await sup.start_all(); assert any("启动失败" in e.message for e in sup.log.snapshot())


@pytest.mark.asyncio
async def test_state_handler_uses_independent_watches():
    sup = RoomSupervisor(rooms=[101, 202]); sup.watch_for(101).consecutive = 5; sup.watch_for(202).consecutive = 2
    await sup._state_handler(101)("live")
    assert sup.watch_for(101).consecutive == 0 and sup.watch_for(202).consecutive == 2


@pytest.mark.asyncio
async def test_state_handler_counts_reconnecting_per_room():
    seen = []
    class Sink:
        async def send(self, alert): seen.append(alert)
    alerter = Alerter(AlertConfig(enabled=True, failure_threshold=2, cooldown_sec=0), sinks=[Sink()])
    sup = RoomSupervisor(rooms=[101, 202], alerter=alerter)
    await sup._state_handler(101)("reconnecting"); await sup._state_handler(202)("reconnecting")
    await sup._state_handler(101)("reconnecting")
    assert len(seen) == 1 and seen[0].key == "reconnect:101"


def test_bind_config_applies_engine_retune(tmp_path):
    path = tmp_path / "c.json"; path.write_text(json.dumps({"rooms": [101], "engine": {"min_gap_sec": 12}}), encoding="utf-8")
    store = ConfigStore(str(path)); sup = RoomSupervisor(rooms=[101], config=EngineConfig(min_gap_sec=12)); sup.bind_config(store)
    path.write_text(json.dumps({"rooms": [101], "engine": {"min_gap_sec": 99}}), encoding="utf-8"); store.reload()
    assert sup.config.min_gap_sec == 99 and sup.engine_for(101).config.min_gap_sec == 99


def test_bind_config_adds_and_removes_rooms(tmp_path):
    path = tmp_path / "c.json"; path.write_text(json.dumps({"rooms": [101]}), encoding="utf-8")
    store = ConfigStore(str(path)); sup = RoomSupervisor(rooms=[101]); sup.bind_config(store)
    path.write_text(json.dumps({"rooms": [202]}), encoding="utf-8"); store.reload()
    assert sup.room_ids == [202] and sup.engine_for(101) is None


def test_bind_config_logs_the_change(tmp_path):
    path = tmp_path / "c.json"; path.write_text(json.dumps({"rooms": []}), encoding="utf-8")
    store = ConfigStore(str(path)); sup = RoomSupervisor(); sup.bind_config(store)
    path.write_text(json.dumps({"rooms": [1]}), encoding="utf-8"); store.reload()
    assert any("热更新" in e.message for e in sup.log.snapshot())
