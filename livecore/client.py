"""WebSocket client: auth, heartbeat, bounded reconnect backoff and clean cancellation."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Literal

import websockets

from . import protocol as proto
from .logger import RingLogger
from .parser import parse_notify
from .types import DanmuEndpoint, LiveEvent

State = Literal["connecting", "authenticating", "live", "reconnecting", "offline", "error"]
EventHandler = Callable[[LiveEvent], Awaitable[None] | None]
StateHandler = Callable[[State], Awaitable[None] | None]


class BiliLiveClient:
    def __init__(self, log: RingLogger) -> None:
        self.log = log
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._on_event: EventHandler | None = None
        self._on_state: StateHandler | None = None

    def on_event(self, fn: EventHandler) -> None:
        self._on_event = fn

    def on_state(self, fn: StateHandler) -> None:
        self._on_state = fn

    async def start(self, endpoint: DanmuEndpoint) -> None:
        await self.stop()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(endpoint), name="bili-live-ws")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.log.push("warn", "net", f"停止连接时异常：{exc}")
        await self._emit_state("offline")

    async def _run(self, endpoint: DanmuEndpoint) -> None:
        attempt = 0
        while not self._stop.is_set():
            url = (
                f"wss://{endpoint.host}/sub"
                if endpoint.wss_port in (443, 0)
                else f"wss://{endpoint.host}:{endpoint.wss_port}/sub"
            )
            await self._emit_state("reconnecting" if attempt else "connecting")
            self.log.push("info", "net", f"连接 {url} 房间 {endpoint.room_id}")
            try:
                async with websockets.connect(url, max_size=2_000_000, open_timeout=10) as ws:
                    await self._emit_state("authenticating")
                    await ws.send(proto.encode_auth(endpoint.room_id, endpoint.token))
                    attempt = 0
                    hb = asyncio.create_task(self._heartbeat(ws), name="bili-heartbeat")
                    try:
                        async for raw in ws:
                            if self._stop.is_set():
                                return
                            if isinstance(raw, str):
                                raw = raw.encode("utf-8")
                            await self._handle_frame(endpoint.room_id, raw)
                    finally:
                        hb.cancel()
                        try:
                            await hb
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
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

    async def _heartbeat(self, ws) -> None:
        while True:
            await ws.send(proto.encode_heartbeat())
            self.log.push("debug", "net", "Ping")
            await asyncio.sleep(25)

    async def _handle_frame(self, room_id: int, raw: bytes) -> None:
        for pkt in proto.expand_packets(raw):
            if pkt.op == proto.OP_AUTH_REPLY:
                await self._emit_state("live")
                self.log.push("info", "net", "认证成功，开始心跳")
            elif pkt.op == proto.OP_HEARTBEAT_REPLY:
                pop = proto.read_popularity(pkt.body)
                ev = LiveEvent(id="pop", ts=0, kind="popularity", room_id=room_id,
                               popularity=pop, text=f"人气 {pop}", raw_cmd="HEARTBEAT_REPLY")
                await self._emit_event(ev)
            elif pkt.op == proto.OP_NOTIFY:
                payload = proto.parse_json_body(pkt.body)
                ev = parse_notify(room_id, payload)
                if ev:
                    await self._emit_event(ev)

    async def _emit_event(self, ev: LiveEvent) -> None:
        if self._on_event:
            res = self._on_event(ev)
            if asyncio.isfuture(res) or asyncio.iscoroutine(res):
                await res

    async def _emit_state(self, state: State) -> None:
        if self._on_state:
            res = self._on_state(state)
            if asyncio.isfuture(res) or asyncio.iscoroutine(res):
                await res
