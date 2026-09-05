"""Tests for livecore.behavior — Phase 4 simulation primitives."""

from __future__ import annotations

import time

import pytest

from livecore.behavior import BUSY_APM, BUSY_SCALE, QUIET_SCALE, WatchSimulator, activity_scale, cold_start_target, gaussian_jitter_sec, typing_delay_sec
from livecore.types import EngineConfig


def test_gaussian_jitter_is_zero_when_disabled_at_zero():
    assert gaussian_jitter_sec(EngineConfig(jitter_ms=0)) == 0.0


def test_gaussian_jitter_stays_within_two_sigma():
    cfg = EngineConfig(jitter_ms=1200, gaussian_jitter=True, jitter_sigma_ratio=0.35)
    base, sigma = 1.2, 1.2 * 0.35
    for _ in range(200):
        d = gaussian_jitter_sec(cfg)
        assert base - 2 * sigma - 1e-9 <= d <= base + 2 * sigma + 1e-9
        assert d >= 0


def test_gaussian_jitter_samples_cluster_around_mean():
    cfg = EngineConfig(jitter_ms=1200, gaussian_jitter=True)
    samples = [gaussian_jitter_sec(cfg) for _ in range(400)]
    mean = sum(samples) / len(samples)
    assert 1.02 <= mean <= 1.38


def test_legacy_triangular_jitter_still_available():
    cfg = EngineConfig(jitter_ms=1200, gaussian_jitter=False)
    for _ in range(100):
        d = gaussian_jitter_sec(cfg)
        assert 0 <= d <= 1200 * 1.8 / 1000 + 1e-9


def test_typing_delay_respects_min_bound():
    cfg = EngineConfig(typing_ms_per_char=45, typing_min_sec=0.6, typing_max_sec=3.5)
    for _ in range(50):
        assert typing_delay_sec("好", cfg) >= 0.6 - 1e-9


def test_typing_delay_respects_max_bound():
    cfg = EngineConfig(typing_ms_per_char=45, typing_min_sec=0.6, typing_max_sec=3.5)
    long_text = "这是一条特别特别长的弹幕内容用来触发上限保护" * 5
    for _ in range(50):
        assert typing_delay_sec(long_text, cfg) <= 3.5 + 1e-9


def test_typing_delay_grows_with_length():
    cfg = EngineConfig(typing_ms_per_char=100, typing_min_sec=0.0, typing_max_sec=100.0)
    short = sum(typing_delay_sec("好", cfg) for _ in range(40)) / 40
    long = sum(typing_delay_sec("这是一条比较长的回复内容", cfg) for _ in range(40)) / 40
    assert long > short


def test_typing_delay_zero_when_disabled():
    assert typing_delay_sec("你好呀", EngineConfig(typing_ms_per_char=0)) == 0.0
    assert typing_delay_sec("", EngineConfig()) == 0.0


def test_activity_scale_quiet_room_stretches_intervals():
    assert activity_scale(0) == QUIET_SCALE


def test_activity_scale_busy_room_compresses_intervals():
    assert activity_scale(int(BUSY_APM)) == pytest.approx(BUSY_SCALE, abs=1e-6)


def test_activity_scale_is_monotonic():
    values = [activity_scale(n) for n in (0, 1, 5, 15, 30, 60, 200)]
    assert values == sorted(values)
    assert values[0] == QUIET_SCALE
    assert values[-1] == pytest.approx(BUSY_SCALE, abs=1e-6)


def test_activity_scale_disabled_is_neutral():
    assert activity_scale(0, enabled=False) == 1.0
    assert activity_scale(999, enabled=False) == 1.0


def test_cold_start_target_within_jitter_window():
    cfg = EngineConfig(cold_start_sec=12, cold_start_jitter_sec=8)
    for _ in range(100):
        assert 12 <= cold_start_target(cfg) <= 20


def test_cold_start_target_fixed_without_jitter():
    cfg = EngineConfig(cold_start_sec=30, cold_start_jitter_sec=0)
    assert cold_start_target(cfg) == 30


def test_watch_simulator_emits_like_once_interval_elapsed():
    sim = WatchSimulator()
    sim.started_at = time.time() - 10_000
    cfg = EngineConfig(watch_actions=True, like_every_sec=150, share_every_sec=100_000)
    actions = sim.poll(cfg, scale=1.0)
    assert [a.kind for a in actions] == ["like"]
    assert actions[0].reason == "模拟点赞"


def test_watch_simulator_quiet_room_waits_longer():
    loud = WatchSimulator()
    loud.started_at = time.time() - 10_000
    quiet = WatchSimulator()
    quiet.started_at = time.time() - 10_000
    cfg = EngineConfig(watch_actions=True, like_every_sec=150, share_every_sec=100_000)
    assert loud.poll(cfg, scale=1.8) and quiet.poll(cfg, scale=0.5)
    loud_now, quiet_now = time.time(), time.time()
    loud.last["like"] = loud_now
    quiet.last["like"] = quiet_now
    loud.last["like"] -= 120
    quiet.last["like"] -= 120
    assert [a.kind for a in loud.poll(cfg, scale=1.8)] == ["like"]
    assert quiet.poll(cfg, scale=0.5) == []


def test_watch_simulator_respects_disabled_flag():
    sim = WatchSimulator()
    sim.started_at = time.time() - 10_000
    assert sim.poll(EngineConfig(watch_actions=False), scale=1.0) == []


def test_watch_simulator_records_history_and_resets():
    sim = WatchSimulator()
    sim.started_at = time.time() - 10_000
    sim.poll(EngineConfig(watch_actions=True, share_every_sec=100_000), scale=1.0)
    assert len(sim.history) == 1
    sim.reset()
    assert sim.history == []
    assert sim.last == {}
    assert sim.started_at > 0


def test_watch_simulator_disabled_interval_never_fires():
    sim = WatchSimulator()
    sim.started_at = time.time() - 10_000
    cfg = EngineConfig(watch_actions=True, like_every_sec=0, share_every_sec=0)
    assert sim.poll(cfg, scale=1.0) == []
