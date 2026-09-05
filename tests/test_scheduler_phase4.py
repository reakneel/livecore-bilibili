"""Tests for the Phase 4 additions to livecore.scheduler."""

from __future__ import annotations

import time

import pytest

from livecore.behavior import BUSY_SCALE, QUIET_SCALE
from livecore.context import RoomContext
from livecore.scheduler import (
    AMBIENT_BASE_SEC,
    AMBIENT_MAX_SEC,
    AMBIENT_MIN_SEC,
    BehaviorScheduler,
)
from livecore.types import EngineConfig, LiveEvent


def _busy_ctx(n: int = 40) -> RoomContext:
    ctx = RoomContext()
    now = time.time()
    for i in range(n):
        ctx.push_event(
            LiveEvent(id=f"e{i}", ts=now - i, kind="danmaku", room_id=1, text=f"弹幕{i}")
        )
    return ctx


def test_reset_randomises_cold_start():
    cfg = EngineConfig(cold_start_sec=30, cold_start_jitter_sec=30)
    targets = set()
    for _ in range(20):
        s = BehaviorScheduler()
        s.reset(cfg)
        targets.add(round(s.cold_start_target, 3))
    assert len(targets) > 1  # 不是固定值
    assert all(30 <= t <= 60 for t in targets)


def test_reset_without_config_falls_back_to_config():
    s = BehaviorScheduler()
    s.reset()
    assert s.cold_start_target == 0.0
    # target 为 0 时回落到 config.cold_start_sec，刚 reset 所以几乎是满窗
    assert 11.9 < s.cold_remaining(EngineConfig(cold_start_sec=12)) <= 12


def test_cold_remaining_prefers_randomised_target():
    s = BehaviorScheduler()
    s.started_at = time.time() - 5
    s.cold_start_target = 40.0
    # 优先用随机化的 40s，而不是 config 的 12s
    assert 34 < s.cold_remaining(EngineConfig(cold_start_sec=12)) <= 35


def test_activity_scale_quiet_room():
    s = BehaviorScheduler()
    assert s.activity_scale(EngineConfig(activity_boost=True), RoomContext()) == pytest.approx(
        QUIET_SCALE
    )


def test_activity_scale_busy_room():
    s = BehaviorScheduler()
    scale = s.activity_scale(EngineConfig(activity_boost=True), _busy_ctx())
    assert QUIET_SCALE < scale <= BUSY_SCALE


def test_activity_scale_disabled_is_neutral():
    s = BehaviorScheduler()
    assert s.activity_scale(EngineConfig(activity_boost=False), _busy_ctx()) == 1.0


def test_ambient_interval_inverse_to_activity():
    s = BehaviorScheduler()
    cfg = EngineConfig()
    quiet = s.ambient_interval(cfg, RoomContext())
    busy = s.ambient_interval(cfg, _busy_ctx())
    assert busy < quiet
    assert AMBIENT_MIN_SEC <= busy <= quiet <= AMBIENT_MAX_SEC


def test_ambient_interval_neutral_room_is_base():
    s = BehaviorScheduler()
    cfg = EngineConfig(activity_boost=False)
    assert s.ambient_interval(cfg, RoomContext()) == pytest.approx(AMBIENT_BASE_SEC)


def test_tick_reports_activity_aware_reason():
    s = BehaviorScheduler()
    s.started_at = time.time() - 1_000
    s.last_check_in = time.time()  # 抑制打卡分支
    cfg = EngineConfig(cold_start_sec=0, min_gap_sec=0)
    text, source, reason = s.tick(cfg, _busy_ctx())  # type: ignore[misc]
    assert source == "random"
    assert reason == "活跃时增加互动"

    s2 = BehaviorScheduler()
    s2.started_at = time.time() - 1_000
    s2.last_check_in = time.time()
    _, _, quiet_reason = s2.tick(cfg, RoomContext())  # type: ignore[misc]
    assert quiet_reason == "冷清时低频互动"


def test_next_delay_uses_gaussian_path():
    s = BehaviorScheduler()
    cfg = EngineConfig(jitter_ms=1200, gaussian_jitter=True)
    samples = [s.next_delay(cfg) for _ in range(200)]
    assert all(0.3 <= d <= 2.1 for d in samples)
    assert 1.0 < sum(samples) / len(samples) < 1.4


def test_typing_delay_delegates_to_behavior():
    s = BehaviorScheduler()
    cfg = EngineConfig(typing_ms_per_char=100, typing_min_sec=0.5, typing_max_sec=2.0)
    assert s.typing_delay("你好", cfg) >= 0.5
    assert s.typing_delay("一" * 200, cfg) <= 2.0
