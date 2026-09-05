from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from .adapters import AiAdapter, NoopAiAdapter, OutboundAdapter, SimulatorAdapter
from .behavior import WatchSimulator
from .bili_http import fetch_danmu_endpoint
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
        self.room_id = 0
        self._event_handlers: list[Callable[[LiveEvent], None]] = []
        self._watch_handlers: list[Callable[[WatchAction], None]] = []
        self._running = False
        self._sched_task: asyncio.Task[None] | None = None
        self._persist_tasks: set[asyncio.Task[None]] = set()
        self._store = None
        self.client.on_event(self._ingest)

    def attach_store(self, store) -> None:
        """Attach an optional SqliteStore; persistence is always scheduled off-loop."""
        self._store = store

    def on_event(self, fn: Callable[[LiveEvent], None]) -> None:
        self._event_handlers.append(fn)

    async def start_bilibili(self, room_id: int) -> None:
        self._running = True
        self.room_id = room_id
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
            try:
                await self._sched_task
            except asyncio.CancelledError:
                pass
            self._sched_task = None
        await self.client.stop()
        if self._persist_tasks:
            await asyncio.gather(*self._persist_tasks, return_exceptions=True)

    def _track(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)
        task.add_done_callback(self._persist_done)

    def _persist_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.log.push("warn", "infra", f"持久化任务失败：{task.exception()}")

    async def accept(self, suggestion_id: str) -> None:
        for s in self.suggestions:
            if s.id == suggestion_id and s.status == "queued":
                s.status = "accepted"
                self.ctx.push_reply(s)
                self.scheduler.mark_emit()
                if self._store is not None:
                    self._track(self._store.save_suggestion_async(s, self.room_id))
                await self.outbound.publish(s)
                self.log.push("info", "behavior", f"采纳建议：{s.text}")
                return

    def _ingest(self, ev: LiveEvent):
        if ev.kind == "popularity":
            return None
        self.ctx.push_event(ev)
        if self._store is not None:
            self._track(self._store.save_event_async(ev))
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
        await asyncio.sleep(self.scheduler.next_delay(self.config))
        await asyncio.sleep(self.scheduler.typing_delay(text, self.config))
        if not self._running or not self.scheduler.gap_ok(self.config):
            return
        self._enqueue(text, source, reason, reply_to)

    def _enqueue(self, raw: str, source: str, reason: str, reply_to: str = "") -> None:
        text = postprocess_reply(raw, self.config.reply_max_len)
        if not text or self.ctx.already_said(text, 60):
            return
        item = Suggestion(
            id=uuid.uuid4().hex[:12],
            ts=time.time(),
            text=text,
            reason=reason,
            source=source,
            in_reply_to=reply_to,
        )
        self.suggestions.append(item)
        if self._store is not None:
            self._track(self._store.save_suggestion_async(item, self.room_id))
        self.scheduler.mark_emit()
        self.log.push("info", "behavior", f"建议「{text}」· {reason}")

    def on_watch_action(self, fn: Callable[[WatchAction], None]) -> None:
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
            self._enqueue(text, source, reason)
