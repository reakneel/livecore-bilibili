"""Tests for livecore.multi — Phase 5.2 multi-room supervision."""

from __future__ import annotations

import json

import pytest

from livecore.alert import Alerter
from livecore.config import AlertConfig, ConfigStore
from livecore.multi import RoomSupervisor
from livecore.types import EngineConfig


def test_add_room_creates_isolated_engines():
    sup = RoomSupervisor()
    a = sup.add_room(101)
    b = sup.add_room(202)
    assert a is not b
    assert a.ctx is not b.ctx
    assert a.scheduler is not b.scheduler
    assert a.watch is not b.watch


def test_add_room_is_idempotent():
    sup = RoomSupervisor()
    first = sup.add_room(101)
    assert sup.add_room(101) is first
    assert len(sup.room_ids) == 1


def test_rooms_from_constructor():
    sup = RoomSupervisor(rooms=[1, 2, 3])
    assert sorted(sup.room_ids) == [1, 2, 3]


def test_contexts_are_not_shared_between_rooms():
    sup = RoomSupervisor()
    a, b = sup.add_room(101), sup.add_room(202)
    from livecore.types import LiveEvent, LiveUser

    ev = LiveEvent(id="e1", ts=0, kind="danmaku", room_id=101, user=LiveUser(uid=1, name="x"), text="hi")
    a.ctx.push_event(ev)
    assert len(a.ctx._recent) == 1
    assert b.ctx._recent == []


def test_engine_for_and_engines_accessors():
    sup = RoomSupervisor(rooms=[5])
    assert sup.engine_for(5) is not None
    assert sup.engine_for(999) is None
    assert set(sup.engines) == {5}


# ---------------------------------------------------------------- failure isolation


@pytest.mark.asyncio
async def test_start_all_isolates_room_failures():
    """One room blowing up must not stop the others from starting."""

    class Boom(RoomSupervisor):
        async def _start_one(self, room_id):
            if room_id == 2:
                raise RuntimeError("boom")
            self.started.append(room_id)

        def __init__(self, *a, **kw):
            self.started = []
            super().__init__(*a, **kw)

    sup = Boom(rooms=[1, 2, 3])
    await sup.start_all()
    assert sup.started == [1, 3]


@pytest.mark.asyncio
async def test_start_all_records_failure_in_log():
    """A room that cannot connect is logged, not raised."""
    sup = RoomSupervisor(rooms=[101], alerter=Alerter(AlertConfig(enabled=False)))
    engine = sup.engine_for(101)

    async def boom(_room_id):
        raise RuntimeError("连接失败")

    engine.start_bilibili = boom  # type: ignore[method-assign]
    await sup.start_all()  # 不应抛出
    assert any("启动失败" in e.message for e in sup.log.snapshot())


# ---------------------------------------------------------------- reconnect watch


@pytest.mark.asyncio
async def test_state_handler_resets_watch_on_live():
    sup = RoomSupervisor(rooms=[101])
    sup.watch.consecutive = 5
    await sup._state_handler(101)("live")
    assert sup.watch.consecutive == 0


@pytest.mark.asyncio
async def test_state_handler_counts_reconnecting():
    seen = []

    class Sink:
        async def send(self, alert):
            seen.append(alert)

    alerter = Alerter(AlertConfig(enabled=True, failure_threshold=2, cooldown_sec=0), sinks=[Sink()])
    sup = RoomSupervisor(rooms=[101], alerter=alerter)
    handler = sup._state_handler(101)
    await handler("reconnecting")
    assert len(seen) == 0
    await handler("reconnecting")
    assert len(seen) == 1
    assert "连续失败 2 次" in seen[0].title


# ---------------------------------------------------------------- persistence hook


def test_persist_event_swallows_storage_errors(tmp_path):
    """A full/broken disk must never kill the ingest path."""
    from livecore.store import SqliteStore

    store = SqliteStore(str(tmp_path / "s.db"))
    sup = RoomSupervisor(rooms=[101], store=store)

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    store.save_event = boom  # type: ignore[method-assign]

    from livecore.types import LiveEvent, LiveUser

    ev = LiveEvent(id="e1", ts=0, kind="danmaku", room_id=101, user=LiveUser(uid=1, name="x"), text="hi")
    sup._persist_event(101)(ev)  # 不应抛出
    assert any("持久化失败" in e.message for e in sup.log.snapshot())
    store.close()


def test_add_room_restores_context_from_store(tmp_path):
    import time

    from livecore.store import SqliteStore
    from livecore.types import Suggestion

    store = SqliteStore(str(tmp_path / "s.db"))
    store.save_suggestion(
        Suggestion(id="r1", ts=time.time(), text="谢谢老板", reason="gift", source="rule"), room_id=101
    )
    sup = RoomSupervisor(store=store)
    engine = sup.add_room(101)
    assert engine.ctx.already_said("谢谢老板", within=3600)
    store.close()


# ---------------------------------------------------------------- hot reload


def test_bind_config_applies_engine_retune(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"rooms": [101], "engine": {"min_gap_sec": 12}}), encoding="utf-8")
    store = ConfigStore(str(path))
    sup = RoomSupervisor(rooms=[101], config=EngineConfig(min_gap_sec=12))
    sup.bind_config(store)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [101], "engine": {"min_gap_sec": 99}}, fh)
    store.reload()

    assert sup.config.min_gap_sec == 99
    assert sup.engine_for(101).config.min_gap_sec == 99


def test_bind_config_spins_up_new_rooms(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"rooms": [101]}), encoding="utf-8")
    store = ConfigStore(str(path))
    sup = RoomSupervisor(rooms=[101])
    sup.bind_config(store)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [101, 202]}, fh)
    store.reload()

    assert sorted(sup.room_ids) == [101, 202]


def test_bind_config_logs_the_change(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"rooms": []}), encoding="utf-8")
    store = ConfigStore(str(path))
    sup = RoomSupervisor()
    sup.bind_config(store)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [1]}, fh)
    store.reload()

    assert any("热更新" in e.message for e in sup.log.snapshot())
