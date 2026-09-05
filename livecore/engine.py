from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from .adapters import AiAdapter, NoopAiAdapter, OutboundAdapter, SimulatorAdapter
from .behavior import WatchSimulator
from .client import BiliLiveClient
from .context import RoomContext
from .dispatcher import EventDispatcher
from .logger import RingLogger
from .postprocess import postprocess_reply
from .rules import match_rule
from .scheduler import BehaviorScheduler
from .types import EngineConfig, LiveEvent, Suggestion, WatchAction


class LiveEngine:
    def __init__(
        self,
        config: EngineConfig | None = None,
        outbound: OutboundAdapter | None = None,
        ai: AiAdapter | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.outbound = outbound or SimulatorAdapter()
        self.ai = ai or NoopAiAdapter()
        self.log = RingLogger()
        self.ctx = RoomContext()
        self.scheduler = BehaviorScheduler()
        self.watch = WatchSimulator()
        self.dispatcher = EventDispatcher()
        self.client = BiliLiveClient(self.log)
        self.suggestions: list[Suggestion] = []
        self._event_handlers: list[Callable[[LiveEvent], None]] = []
        self._watch_handlers: list[Callable[[WatchAction], None]] = []
        self._running = False
        self._sched_task: asyncio.Task[None] | None = None
        self.client.on_event(self._ingest)

    def on_event(self, fn: Callable[[LiveEvent], None]) -> None:
        self._event_handlers.append(fn)

    async def start_bilibili(self, room_id: int) -> None:
        from .bili_http import fetch_danmu_endpoint

        self._running = True
        self.scheduler.reset(self.config)
        self.watch.reset()
        endpoint = await fetch_danmu_endpoint(room_id)
        self.log.push("info", "net", f"弹幕服务器 {endpoint.host} 房间 {endpoint.room_id}")
        await self.client.start(endpoint)
        self._sched_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._running = False
        if self._sched_task:
            self._sched_task.cancel()
            self._sched_task = None
        await self.client.stop()

    async def accept(self, suggestion_id: str) -> None:
        for s in self.suggestions:
            if s.id == suggestion_id and s.status == "queued":
                s.status = "accepted"
                self.ctx.push_reply(s)
                self.scheduler.mark_emit()
                await self.outbound.publish(s)
                self.log.push("info", "behavior", f"采纳建议：{s.text}")
                return

    def _ingest(self, ev: LiveEvent):
        if ev.kind == "popularity":
            return None
        self.ctx.push_event(ev)
        self.dispatcher.emit(ev)
        for fn in self._event_handlers:
            fn(ev)
        if not self.config.auto_suggest:
            return None
        if self.scheduler.cold_remaining(self.config) > 0 or not self.scheduler.gap_ok(self.config):
            return None
        hit = match_rule(ev)
        if not hit:
            return None
        text, reason = hit
        asyncio.create_task(self._delayed_enqueue(text, "rule", reason, ev.id))
        return None

    async def _delayed_enqueue(self, text: str, source: str, reason: str, reply_to: str) -> None:
        # 先等一个高斯分布的操作延迟，再叠加与回复长度相关的「打字」耗时
        await asyncio.sleep(self.scheduler.next_delay(self.config))
        await asyncio.sleep(self.scheduler.typing_delay(text, self.config))
        if not self._running or not self.scheduler.gap_ok(self.config):
            return
        self._enqueue(text, source, reason, reply_to)  # type: ignore[arg-type]

    def _enqueue(self, raw: str, source: str, reason: str, reply_to: str = "") -> None:
        text = postprocess_reply(raw, self.config.reply_max_len)
        if not text or self.ctx.already_said(text, 60):
            return
        item = Suggestion(
            id=uuid.uuid4().hex[:12],
            ts=time.time(),
            text=text,
            reason=reason,
            source=source,  # type: ignore[arg-type]
            in_reply_to=reply_to,
        )
        self.suggestions.append(item)
        self.scheduler.mark_emit()
        self.log.push("info", "behavior", f"建议「{text}」· {reason}")

    def on_watch_action(self, fn: Callable[[WatchAction], None]) -> None:
        """Subscribe to like/share/stay actions. Nothing is sent to Bilibili by default."""
        self._watch_handlers.append(fn)

    async def _scheduler_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1)
            for action in self.watch.poll(self.config, self.scheduler.activity_scale(self.config, self.ctx)):
                self.log.push("info", "watch", f"{action.reason}（{action.kind}）")
                for fn in self._watch_handlers:
                    fn(action)
            action = self.scheduler.tick(self.config, self.ctx)
            if not action or not self.config.auto_suggest:
                continue
            text, source, reason = action
            if self.ctx.already_said(text):
                continue
            self._enqueue(text, source, reason)  # type: ignore[arg-type]
