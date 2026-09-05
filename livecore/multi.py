"""Phase 5.2 — multi-room supervision.

Each room runs its own :class:`~livecore.engine.LiveEngine`, and each engine owns
its own :class:`~livecore.context.RoomContext`, scheduler and watch simulator.
That isolation is the whole point: two rooms must never share a de-dupe window
or a cold-start timer.

Rooms are started as concurrent tasks and supervised independently — one room
failing does not tear down the others.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

from .alert import Alerter, AlertConfig, ReconnectWatch
from .config import ConfigStore
from .engine import LiveEngine
from .logger import RingLogger
from .store import SqliteStore, restore_context
from .types import EngineConfig

__all__ = ["RoomSupervisor"]


class RoomSupervisor:
    """Owns one :class:`LiveEngine` per room and supervises them together."""

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
        self.watch = ReconnectWatch(self.alerter, self.alerter.config.failure_threshold)
        self._engines: dict[int, LiveEngine] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._reload_hook: Callable[[dict, dict], None] | None = None
        for room_id in rooms:
            self.add_room(room_id)

    # ---------------------------------------------------------------- registry

    def add_room(self, room_id: int, config: EngineConfig | None = None) -> LiveEngine:
        """Create (or return) the engine for ``room_id``. Contexts stay isolated."""
        if room_id in self._engines:
            return self._engines[room_id]
        engine = LiveEngine(config=config or self.config)
        engine.client.on_state(self._state_handler(room_id))
        if self.store is not None:
            restore_context(self.store, room_id, engine.ctx)
            engine.on_event(self._persist_event(room_id))
        self._engines[room_id] = engine
        return engine

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
            if state == "live":
                self.watch.record_success()
            elif state in {"reconnecting", "error"}:
                await self.watch.record_failure(f"房间 {room_id} 状态 {state}")

        return on_state

    def _persist_event(self, room_id: int):
        def on_event(ev) -> None:
            if self.store is None:
                return
            try:
                self.store.save_event(ev)
            except Exception as exc:  # 持久化失败不能拖垮收流
                self.log.push("warn", "infra", f"事件持久化失败：{exc}")

        return on_event

    # ---------------------------------------------------------------- lifecycle

    async def start_all(self) -> None:
        """Start every room concurrently. A single room's failure is isolated."""
        await asyncio.gather(
            *(self._start_one(room_id) for room_id in self.room_ids),
            return_exceptions=True,
        )

    async def _start_one(self, room_id: int) -> None:
        engine = self._engines[room_id]
        try:
            await engine.start_bilibili(room_id)
        except Exception as exc:
            self.log.push("error", "net", f"房间 {room_id} 启动失败：{exc}")
            await self.watch.record_failure(f"房间 {room_id} 启动失败：{exc}")

    async def stop_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        for engine in self._engines.values():
            try:
                await engine.stop()
            except Exception as exc:
                self.log.push("warn", "net", f"停止时异常：{exc}")

    # ---------------------------------------------------------------- hot reload

    def bind_config(self, store: ConfigStore) -> None:
        """Subscribe to hot reloads: new rooms spin up, retuning applies live."""
        self._reload_hook = lambda old, new: self._apply_reload(store, old, new)
        store.on_reload(self._reload_hook)

    def _apply_reload(self, store: ConfigStore, old: dict, new: dict) -> None:
        self.log.push("info", "infra", "检测到配置变更，热更新生效")
        for room_id in store.rooms():
            if room_id not in self._engines:
                self.add_room(room_id, store.engine_config())
        if old.get("engine") != new.get("engine"):
            fresh = store.engine_config()
            self.config = fresh
            for engine in self._engines.values():
                engine.config = fresh
