"""Platform-neutral long connection: auth, heartbeat, backoff reconnect, fan-out.

The transport deliberately contains no platform knowledge. It opens the socket,
sends whatever the adapter tells it to send, and hands raw frames back to the
adapter for decoding. ``BiliLiveClient`` in :mod:`livecore.client` is a thin
Bilibili-bound subclass kept for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import websockets

from .logger import RingLogger
from .platforms import FrameKind, LiveEndpoint, PlatformAdapter, get_adapter
from .types import LiveEvent

State = Literal["connecting", "authenticating", "live", "reconnecting", "offline", "error"]
EventHandler = Callable[[LiveEvent], Awaitable[None] | None]
StateHandler = Callable[[State], Awaitable[None] | None]

#: ``websockets`` renamed this keyword in 14.0 and dropped the old spelling later.
_HEADERS_KWARG: str | None = None


def _headers_kwarg() -> str:
    global _HEADERS_KWARG
    if _HEADERS_KWARG is None:
        try:
            params = inspect.signature(websockets.connect).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic builds
            params = {}
        _HEADERS_KWARG = "additional_headers" if "additional_headers" in params else "extra_headers"
    return _HEADERS_KWARG


class LiveConnection:
    """Owns one websocket connection to one room on one platform."""

    def __init__(self, log: RingLogger, adapter: PlatformAdapter | None = None) -> None:
        self.log = log
        self.adapter = adapter or get_adapter()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._endpoint: LiveEndpoint | None = None
        self._on_event: EventHandler | None = None
        self._on_state: StateHandler | None = None

    # ------------------------------------------------------------- observers

    def on_event(self, fn: EventHandler) -> None:
        self._on_event = fn

    def on_state(self, fn: StateHandler) -> None:
        self._on_state = fn

    @property
    def endpoint(self) -> LiveEndpoint | None:
        return self._endpoint

    @property
    def platform(self) -> str:
        return self.adapter.name

    # -------------------------------------------------------------- lifecycle

    async def start(self, endpoint: LiveEndpoint | object) -> None:
        """Connect using an endpoint produced by any platform adapter."""
        resolved = self.adapter.coerce_endpoint(endpoint)
        await self.stop()
        self._endpoint = resolved
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(resolved), name=f"{self.adapter.name}-live-ws"
        )

    async def start_room(self, room_id: int) -> LiveEndpoint:
        """Resolve ``room_id`` through the adapter and connect to it."""
        endpoint = await self.adapter.fetch_endpoint(self.adapter.normalize_room_id(room_id))
        await self.start(endpoint)
        return endpoint

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown must never raise
                self.log.push("warn", "net", f"停止连接时异常：{exc}")
        await self._emit_state("offline")

    # ------------------------------------------------------------------ loop

    async def _run(self, endpoint: LiveEndpoint) -> None:
        attempt = 0
        options = self.adapter.transport_options()
        headers = self.adapter.connection_headers(endpoint)
        url = self.adapter.websocket_url(endpoint)

        while not self._stop.is_set():
            await self._emit_state("reconnecting" if attempt else "connecting")
            self.log.push("info", "net", f"连接 {url} 房间 {endpoint.room_id}")
            try:
                connect_kwargs: dict[str, Any] = dict(options)
                if headers:
                    connect_kwargs[_headers_kwarg()] = headers
                async with websockets.connect(url, **connect_kwargs) as ws:
                    await self._emit_state("authenticating")
                    await ws.send(self.adapter.build_auth_packet(endpoint))
                    attempt = 0
                    heartbeat = asyncio.create_task(
                        self._heartbeat(ws), name=f"{self.adapter.name}-heartbeat"
                    )
                    try:
                        async for raw in ws:
                            if self._stop.is_set():
                                return
                            if isinstance(raw, str):
                                raw = raw.encode("utf-8")
                            await self._handle_frame(endpoint.room_id, raw)
                    finally:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any socket error means reconnect
                await self._emit_state("error")
                self.log.push("warn", "net", f"套接字异常：{exc}")
            if self._stop.is_set():
                return
            attempt += 1
            delay = min(30.0, 0.8 * (2 ** min(attempt, 6)))
            delay *= 0.7 + random.random() * 0.6
            self.log.push("warn", "net", f"第 {attempt} 次重连，{delay:.1f}s 后重试")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _heartbeat(self, ws: Any) -> None:
        """Application-level heartbeat; the only keepalive these servers honour."""
        interval = self.adapter.heartbeat_interval_sec
        if self.adapter.heartbeat_immediate:
            await ws.send(self.adapter.build_heartbeat_packet())
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            await ws.send(self.adapter.build_heartbeat_packet())

    async def _handle_frame(self, room_id: int, raw: bytes) -> None:
        try:
            frames = self.adapter.decode_frame(raw, room_id)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the socket
            self.log.push("warn", "net", f"解码帧失败，已跳过：{exc}")
            return
        for frame in frames:
            if frame.kind is FrameKind.AUTH_OK:
                await self._emit_state("live")
                self.log.push(
                    "info",
                    "net",
                    f"认证成功，心跳间隔 {self.adapter.heartbeat_interval_sec:.0f}s",
                )
            elif frame.kind is FrameKind.HEARTBEAT:
                event = LiveEvent(
                    id="pop",
                    ts=0.0,
                    kind="popularity",
                    room_id=room_id,
                    popularity=frame.popularity,
                    text=f"人气 {frame.popularity}",
                    raw_cmd="HEARTBEAT_REPLY",
                )
                await self._emit_event(event)
            else:
                for event in frame.events:
                    await self._emit_event(event)

    # --------------------------------------------------------------- emission

    async def _emit_event(self, event: LiveEvent) -> None:
        if self._on_event is None:
            return
        result = self._on_event(event)
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result

    async def _emit_state(self, state: State) -> None:
        if self._on_state is None:
            return
        result = self._on_state(state)
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result
