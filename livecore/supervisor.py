"""Production connection supervision and health snapshots."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic

from .bili_http import HttpConfig, fetch_danmu_endpoint
from .client import BiliLiveClient, State
from .logger import RingLogger


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
        self.client.on_state(self._on_state)

    async def start(self) -> None:
        endpoint = await fetch_danmu_endpoint(self.room_id, config=self.http_config)
        await self.client.start(endpoint)

    async def stop(self) -> None:
        await self.client.stop()

    async def _on_state(self, state: State) -> None:
        previous = self.health.state
        self.health.state = state
        if state == "live":
            self.health.last_live_at = monotonic()
        elif state == "reconnecting" and previous != "reconnecting":
            self.health.reconnects += 1

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
        await supervisor.start()
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
