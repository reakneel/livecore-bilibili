"""Tests for livecore.scheduler.BehaviorScheduler."""

from __future__ import annotations

import time

from livecore.context import RoomContext
from livecore.scheduler import BehaviorScheduler
from livecore.types import EngineConfig, LiveEvent


def test_cold_remaining_starts_positive_and_decreases():
    s = BehaviorScheduler()
    s.started_at = time.time() - 5
    rem = s.cold_remaining(EngineConfig(cold_start_sec=12))
    assert 6 < rem <= 7


def test_cold_remaining_zero_after_window():
    s = BehaviorScheduler()
    s.started_at = time.time() - 100
    assert s.cold_remaining(EngineConfig(cold_start_sec=12)) == 0


def test_gap_ok_blocks_immediately_after_emit():
    s = BehaviorScheduler()
    s.mark_emit()
    cfg = EngineConfig(min_gap_sec=12)
    assert s.gap_ok(cfg) is False


def test_gap_ok_passes_after_window():
    s = BehaviorScheduler()
    s.last_emit = time.time() - 30
    cfg = EngineConfig(min_gap_sec=12)
    assert s.gap_ok(cfg) is True


def test_next_delay_within_jitter_range():
    s = BehaviorScheduler()
    cfg = EngineConfig(jitter_ms=1200)
    for _ in range(50):
        d = s.next_delay(cfg)
        # triangular distribution; roughly [-1.6, 1.6] * jitter / 1000
        assert -0.5 <= d <= 2.5


def test_tick_returns_checkin_after_window():
    s = BehaviorScheduler()
    s.started_at = time.time() - 30  # past cold-start
    s.last_check_in = time.time() - 6 * 60
    cfg = EngineConfig(check_in_min=5, cold_start_sec=12, min_gap_sec=12)
    action = s.tick(cfg, RoomContext())
    assert action is not None
    text, source, reason = action
    assert source == "schedule"
    assert reason == "定时打卡"


def test_reset_zeroes_all_counters():
    s = BehaviorScheduler()
    s.mark_emit()
    s.last_check_in = time.time()
    s.last_ambient = time.time()
    before = (s.last_emit, s.last_check_in, s.last_ambient)
    s.reset()
    assert (s.last_emit, s.last_check_in, s.last_ambient) != before
    assert s.started_at > 0
