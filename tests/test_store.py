"""Tests for livecore.store — Phase 5.4 SQLite persistence."""

from __future__ import annotations

import asyncio
import time

import pytest

from livecore.context import RoomContext
from livecore.store import SqliteStore, restore_context
from livecore.types import GiftInfo, LiveEvent, LiveUser, Suggestion


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(str(tmp_path / "livecore.db"))
    yield s
    s.close()


def _event(eid: str, ts: float | None = None, kind: str = "danmaku", text: str = "你好") -> LiveEvent:
    return LiveEvent(id=eid, ts=ts if ts is not None else time.time(), kind=kind, room_id=101,
                     user=LiveUser(uid=7, name="观众A"), text=text, sentiment="neutral")  # type: ignore[arg-type]


def _reply(rid: str, ts: float | None = None, text: str = "谢谢", source: str = "rule") -> Suggestion:
    return Suggestion(id=rid, ts=ts if ts is not None else time.time(), text=text,
                      reason="礼物", source=source, in_reply_to="e1", status="accepted")  # type: ignore[arg-type]


def test_save_and_read_event(store):
    store.save_event(_event("e1"))
    rows = store.recent_events(101)
    assert len(rows) == 1 and rows[0].id == "e1"
    assert rows[0].user_name == "观众A" and rows[0].text == "你好" and rows[0].kind == "danmaku"


def test_save_and_read_reply(store):
    store.save_suggestion(_reply("r1"), room_id=101)
    row = store.recent_replies(101)[0]
    assert row.text == "谢谢" and row.source == "rule" and row.status == "accepted"
    assert row.in_reply_to == "e1"


@pytest.mark.asyncio
async def test_async_writes_round_trip(store):
    await asyncio.gather(*(store.save_event_async(_event(f"e{i}")) for i in range(20)))
    assert len(store.recent_events(101, limit=50)) == 20


def test_events_are_scoped_by_room(store):
    store.save_event(_event("e1"))
    store.save_event(LiveEvent(id="e2", ts=time.time(), kind="danmaku", room_id=999, text="别的房间"))
    assert len(store.recent_events(101)) == 1 and len(store.recent_events(999)) == 1


def test_recent_orders_newest_first_and_limits(store):
    now = time.time()
    for i in range(10): store.save_event(_event(f"e{i}", ts=now - i))
    assert [r.id for r in store.recent_events(101, limit=3)] == ["e0", "e1", "e2"]


def test_duplicate_ids_upsert(store):
    store.save_event(_event("e1", text="第一次")); store.save_event(_event("e1", text="第二次"))
    assert len(store.recent_events(101)) == 1 and store.recent_events(101)[0].text == "第二次"


def test_event_without_user_persists_empty_name(store):
    store.save_event(LiveEvent(id="e9", ts=time.time(), kind="system", room_id=101, text="系统通知"))
    assert store.recent_events(101)[0].user_name == ""


def test_gift_event_round_trip(store):
    ev = _event("g1", kind="gift", text="投喂"); ev.gift = GiftInfo(name="辣条", num=1, price=100)
    store.save_event(ev); assert store.recent_events(101)[0].kind == "gift"


def test_prune_removes_old_rows_only(store):
    now = time.time()
    store.save_event(_event("old", ts=now - 30 * 86400)); store.save_event(_event("new", ts=now))
    store.save_suggestion(_reply("rold", ts=now - 30 * 86400), room_id=101)
    store.save_suggestion(_reply("rnew", ts=now), room_id=101)
    assert store.prune(retention_days=7) == 2
    assert [r.id for r in store.recent_events(101)] == ["new"]
    assert [r.id for r in store.recent_replies(101)] == ["rnew"]


def test_prune_keeps_everything_when_fresh(store):
    store.save_event(_event("e1")); assert store.prune(retention_days=7) == 0


def test_restore_context_replays_all_reply_metadata(store):
    now = time.time()
    store.save_suggestion(_reply("r1", now - 10, "谢谢老板", "ai"), room_id=101)
    ctx = RoomContext(); assert restore_context(store, 101, ctx) == 1
    restored = ctx._replies[-1]
    assert restored.source == "ai" and restored.status == "accepted" and restored.in_reply_to == "e1"


def test_restore_context_skips_expired_replies(store):
    now = time.time(); store.save_suggestion(_reply("old", now - 7200, "很久以前"), room_id=101)
    store.save_suggestion(_reply("new", now, "刚刚"), room_id=101)
    ctx = RoomContext(); assert restore_context(store, 101, ctx, within=3600) == 1
    assert ctx.already_said("刚刚", within=3600) and not ctx.already_said("很久以前", within=3600)


def test_restore_context_empty_database(tmp_path):
    with SqliteStore(str(tmp_path / "empty.db")) as store: assert restore_context(store, 101, RoomContext()) == 0


def test_context_manager_closes_connection(tmp_path):
    path = str(tmp_path / "ctx.db")
    with SqliteStore(path) as store:
        store.save_event(_event("e1")); assert store._conn is not None
    assert store._conn is None


def test_reopen_reads_existing_file(tmp_path):
    path = str(tmp_path / "persist.db"); s1 = SqliteStore(path); s1.save_event(_event("e1")); s1.close()
    s2 = SqliteStore(path); assert len(s2.recent_events(101)) == 1; s2.close()
