from __future__ import annotations

import random
import time

from .context import RoomContext
from .rules import AMBIENT_LINES, CHECK_IN_LINES
from .types import EngineConfig


class BehaviorScheduler:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.last_emit = 0.0
        self.last_check_in = 0.0
        self.last_ambient = 0.0

    def reset(self) -> None:
        now = time.time()
        self.started_at = now
        self.last_emit = 0.0
        self.last_check_in = 0.0
        self.last_ambient = 0.0

    def mark_emit(self) -> None:
        self.last_emit = time.time()

    def cold_remaining(self, config: EngineConfig) -> float:
        return max(0.0, config.cold_start_sec - (time.time() - self.started_at))

    def gap_ok(self, config: EngineConfig) -> bool:
        if self.last_emit == 0:
            return self.cold_remaining(config) == 0
        return time.time() - self.last_emit >= config.min_gap_sec

    def next_delay(self, config: EngineConfig) -> float:
        u = random.random() + random.random() - 1
        return max(0.0, (config.jitter_ms + u * config.jitter_ms * 0.8) / 1000)

    def tick(self, config: EngineConfig, ctx: RoomContext) -> tuple[str, str, str] | None:
        if self.cold_remaining(config) > 0 or not self.gap_ok(config):
            return None
        now = time.time()
        activity = ctx.activity_per_minute()
        boost = config.activity_boost and activity >= 12
        quiet = activity < 4
        if now - self.last_check_in >= config.check_in_min * 60:
            self.last_check_in = now
            return random.choice(CHECK_IN_LINES), "schedule", "定时打卡"
        ambient_every = 180 if quiet else 50 if boost else 90
        if now - self.last_ambient >= ambient_every:
            self.last_ambient = now
            reason = "冷清时低频互动" if quiet else "随机氛围弹幕"
            return random.choice(AMBIENT_LINES), "random", reason
        return None
