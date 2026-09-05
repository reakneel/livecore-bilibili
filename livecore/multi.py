"""Phase 5.2 — multi-room supervision."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from .alert import Alerter, AlertConfig, ReconnectWatch
from .config import ConfigStore
from .engine import LiveEngine
from .logger import RingLogger
from .store import SqliteStore, restore_context
from .types import EngineConfig

__all__ = ["RoomSupervisor"]


class RoomSupervisor:
    """Owns one isolated engine and reconnect watcher per room."""

    def __init__(
        self,
        rooms: Iterable[int] = (),
        config: EngineConfig | None = None,
        store: SqliteStore | None = None,
        alerter: Alerter | None = None,
        log: RingLogger | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.log = log or RingLogger()
        self.store = store
        self.alerter = alerter or Alerter(AlertConfig(), log=self.log)
        self._watches: dict[int, ReconnectWatch] = {}
        self._engines: dict[int, LiveEngine] = {}
        self._reload_tasks: set[asyncio.Task[None]] = set()
        for room_id in rooms:
            self.add_room(room_id)

    def _watch_for(self, room_id: int) -> ReconnectWatch:
        watch = self._watches.get(room_id)
        if watch is None:
            watch = ReconnectWatch(self.alerter, self.alerter.config.failure_threshold, key=f"reconnect:{room_id}")
            self._watches[room_id] = watch
        return watch

    def add_room(self, room_id: int, config: EngineConfig | None = None) -> LiveEngine:
        if room_id in self._engines:
            return self._engines[room_id]
        engine = LiveEngine(config=config or self.config)
        engine.client.on_state(self._state_handler(room_id))
        if self.store is not None:
            restore_context(self.store, room_id, engine.ctx)
            engine.attach_store(self.store)
        self._watch_for(room_id)
        self._engines[room_id] = engine
        return engine

    def remove_room(self, room_id: int) -> None:
        """Detach a room after its engine has been stopped."""
        self._engines.pop(room_id, None)
        self._watches.pop(room_id, None)

    def engine_for(self, room_id: int) -> LiveEngine | None:
        return self._engines.get(room_id)

    @property
    def engines(self) -> dict[int, LiveEngine]:
        return dict(self._engines)

    @property
    def room_ids(self) -> list[int]:
        return list(self._engines)

    def _state_handler(self, room_id: int):
        async def on_state(state: str) -> None:
            watch = self._watch_for(room_id)
            if state == "live":
                watch.record_success()
            elif state in {"reconnecting", "error"}:
                await watch.record_failure(f"房间 {room_id} 状态 {state}")
        return on_state

    async def start_all(self) -> None:
        await asyncio.gather(*(self._start_one(room_id) for room_id in self.room_ids), return_exceptions=True)

    async def _start_one(self, room_id: int) -> None:
        engine = self._engines[room_id]
        try:
            await engine.start_bilibili(room_id)
        except Exception as exc:
            self.log.push("error", "net", f"房间 {room_id} 启动失败：{exc}")
            await self._watch_for(room_id).record_failure(f"房间 {room_id} 启动失败：{exc}")

    async def stop_all(self) -> None:
        for task in list(self._reload_tasks):
            task.cancel()
        if self._reload_tasks:
            await asyncio.gather(*self._reload_tasks, return_exceptions=True)
        await asyncio.gather(*(engine.stop() for engine in self._engines.values()), return_exceptions=True)
        self._reload_tasks.clear()

    def bind_config(self, store: ConfigStore) -> None:
        store.on_reload(lambda old, new: self._apply_reload(store, old, new))

    def _apply_reload(self, store: ConfigStore, old: dict, new: dict) -> None:
        """Reconcile rooms/config without blocking the ConfigStore caller."""
        self.log.push("info", "infra", "检测到配置变更，热更新生效")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._reconcile_reload(store))
        self._reload_tasks.add(task)
        task.add_done_callback(self._reload_tasks.discard)

    async def _reconcile_reload(self, store: ConfigStore) -> None:
        desired = set(store.rooms())
        current = set(self._engines)
        for room_id in sorted(desired - current):
            self.add_room(room_id, store.engine_config())
            await self._start_one(room_id)
        for room_id in sorted(current - desired):
            engine = self._engines.get(room_id)
            if engine is not None:
                await engine.stop()
            self.remove_room(room_id)

        fresh = store.engine_config()
        self.config = fresh
        for engine in self._engines.values():
            engine.config = fresh

        self.alerter.config = store.alert_config()
        if self.store is not None and store.storage_config().enabled is False:
            self.store.close()
            self.store = None
        elif self.store is None and store.storage_config().enabled:
            self.store = SqliteStore(store.storage_config().path)
        for engine in self._engines.values():
            engine.attach_store(self.store)
