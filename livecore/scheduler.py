from __future__ import annotations

import random
import time

from .behavior import (
    activity_scale as activity_scale_factor,
)
from .behavior import cold_start_target, gaussian_jitter_sec
from .context import RoomContext
from .rules import AMBIENT_LINES, CHECK_IN_LINES
from .types import EngineConfig

#: 氛围弹幕的基础间隔（秒），再按活跃度缩放后夹紧到该区间。
AMBIENT_BASE_SEC = 90.0
AMBIENT_MIN_SEC = 45.0
AMBIENT_MAX_SEC = 300.0


class BehaviorScheduler:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.last_emit = 0.0
        self.last_check_in = 0.0
        self.last_ambient = 0.0
        # 0 表示「尚未随机化」，此时回落到 config.cold_start_sec
        self.cold_start_target = 0.0

    def reset(self, config: EngineConfig | None = None) -> None:
        now = time.time()
        self.started_at = now
        self.last_emit = 0.0
        self.last_check_in = 0.0
        self.last_ambient = 0.0
        self.cold_start_target = cold_start_target(config) if config else 0.0

    def mark_emit(self) -> None:
        self.last_emit = time.time()

    def cold_remaining(self, config: EngineConfig) -> float:
        base = self.cold_start_target or config.cold_start_sec
        return max(0.0, base - (time.time() - self.started_at))

    def gap_ok(self, config: EngineConfig) -> bool:
        if self.last_emit == 0:
            return self.cold_remaining(config) == 0
        return time.time() - self.last_emit >= config.min_gap_sec

    def next_delay(self, config: EngineConfig) -> float:
        """Pre-send pause in seconds (Phase 4: gaussian by default)."""
        return gaussian_jitter_sec(config)

    def typing_delay(self, text: str, config: EngineConfig) -> float:
        """Extra pause proportional to reply length — see :mod:`livecore.behavior`."""
        from .behavior import typing_delay_sec

        return typing_delay_sec(text, config)

    def activity_scale(self, config: EngineConfig, ctx: RoomContext) -> float:
        """>1 means the room is busy (compress intervals), <1 means quiet (stretch)."""
        return activity_scale_factor(ctx.activity_per_minute(), config.activity_boost)

    def ambient_interval(self, config: EngineConfig, ctx: RoomContext) -> float:
        """Seconds between spontaneous ambient lines, scaled by room activity."""
        scale = self.activity_scale(config, ctx)
        return max(AMBIENT_MIN_SEC, min(AMBIENT_MAX_SEC, AMBIENT_BASE_SEC / max(0.2, scale)))

    def tick(self, config: EngineConfig, ctx: RoomContext) -> tuple[str, str, str] | None:
        if self.cold_remaining(config) > 0 or not self.gap_ok(config):
            return None
        now = time.time()
        scale = self.activity_scale(config, ctx)
        if now - self.last_check_in >= config.check_in_min * 60:
            self.last_check_in = now
            return random.choice(CHECK_IN_LINES), "schedule", "定时打卡"
        ambient_every = self.ambient_interval(config, ctx)
        if now - self.last_ambient >= ambient_every:
            self.last_ambient = now
            if scale < 0.8:
                reason = "冷清时低频互动"
            elif scale > 1.4:
                reason = "活跃时增加互动"
            else:
                reason = "随机氛围弹幕"
            return random.choice(AMBIENT_LINES), "random", reason
        return None
