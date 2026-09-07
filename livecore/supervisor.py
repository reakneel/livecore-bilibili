"""Production connection supervision and health snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from .bili_http import HttpConfig, fetch_danmu_endpoint
from .client import BiliLiveClient, State
from .logger import RingLogger
from .types import LiveEvent


EventHandler = Callable[[LiveEvent], Awaitable[None] | None]
StateHandler = Callable[[State], Awaitable[None] | None]


@dataclass(slots=True)
class ConnectionHealth:
    room_id: int
    state: State = "offline"
    reconnects: int = 0
    last_live_at: float = 0.0
    last_error: str = ""

    @property
    def live_for_sec(self) -> float:
        return max(0.0, monotonic() - self.last_live_at) if self.last_live_at else 0.0


class ConnectionSupervisor:
    """Own one client lifecycle and expose a small UI/API-friendly health model."""

    def __init__(self, room_id: int, log: RingLogger, *, http_config: HttpConfig | None = None) -> None:
        if room_id <= 0:
            raise ValueError("room_id must be positive")
        self.room_id = room_id
        self.log = log
        self.http_config = http_config or HttpConfig()
        self.client = BiliLiveClient(log)
        self.health = ConnectionHealth(room_id=room_id)
        self._event_handler: EventHandler | None = None
        self._state_handler: StateHandler | None = None
        self.client.on_event(self._emit_event)
        self.client.on_state(self._emit_state)

    def on_event(self, fn: EventHandler) -> None:
        """Register an observer for parsed LiveEvent values."""
        self._event_handler = fn

    def on_state(self, fn: StateHandler) -> None:
        """Register an observer for connection state changes."""
        self._state_handler = fn

    async def start(self) -> None:
        endpoint = await fetch_danmu_endpoint(self.room_id, config=self.http_config)
        await self.client.start(endpoint)

    async def stop(self) -> None:
        await self.client.stop()

    async def _emit_event(self, event: LiveEvent) -> None:
        if self._event_handler:
            result = self._event_handler(event)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                await result

    async def _emit_state(self, state: State) -> None:
        previous = self.health.state
        self.health.state = state
        if state == "live":
            self.health.last_live_at = monotonic()
        elif state == "reconnecting" and previous != "reconnecting":
            self.health.reconnects += 1
        if self._state_handler:
            result = self._state_handler(state)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                await result

    def snapshot(self) -> ConnectionHealth:
        return ConnectionHealth(
            room_id=self.health.room_id,
            state=self.health.state,
            reconnects=self.health.reconnects,
            last_live_at=self.health.last_live_at,
            last_error=self.health.last_error,
        )


class MultiRoomSupervisor:
    """Lightweight lifecycle manager suitable for a Vite-facing API service."""

    def __init__(self, log: RingLogger) -> None:
        self.log = log
        self.rooms: dict[int, ConnectionSupervisor] = {}

    async def add(self, room_id: int, *, http_config: HttpConfig | None = None) -> ConnectionSupervisor:
        if room_id in self.rooms:
            return self.rooms[room_id]
        supervisor = ConnectionSupervisor(room_id, self.log, http_config=http_config)
        self.rooms[room_id] = supervisor
        try:
            await supervisor.start()
        except Exception:
            self.rooms.pop(room_id, None)
            raise
        return supervisor

    async def remove(self, room_id: int) -> None:
        supervisor = self.rooms.pop(room_id, None)
        if supervisor:
            await supervisor.stop()

    async def stop(self) -> None:
        rooms = list(self.rooms.values())
        self.rooms.clear()
        if rooms:
            await asyncio.gather(*(room.stop() for room in rooms), return_exceptions=True)

    def health(self) -> list[ConnectionHealth]:
        return [room.snapshot() for room in self.rooms.values()]
