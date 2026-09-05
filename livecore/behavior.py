"""Phase 4 — human-like behaviour simulation.

Covers four things that make an automated account look less like a bot:

1. **Gaussian jitter** — sending delays follow a normal distribution around
   ``jitter_ms`` instead of a flat uniform range, so most actions cluster near
   the mean with occasional longer pauses.
2. **Typing cadence** — the delay before a reply grows with its length, as if
   someone were actually typing it out.
3. **Cold start** — the silent "just arrived, watching first" window is
   randomised per session instead of being a fixed constant.
4. **Watch actions** — non-message behaviours (like / share / stay) emitted on
   activity-scaled randomised intervals.

Everything here is pure local computation. No Bilibili endpoint is contacted:
watch actions are returned as plain :class:`~livecore.types.WatchAction`
records so the host application decides what (if anything) to do with them.
"""

from __future__ import annotations

import math
import random
import time

from .types import EngineConfig, WatchAction

__all__ = [
    "QUIET_SCALE",
    "BUSY_SCALE",
    "BUSY_APM",
    "gaussian_jitter_sec",
    "typing_delay_sec",
    "activity_scale",
    "cold_start_target",
    "WatchSimulator",
]

# 冷清（0 弹幕/分钟）时的频率缩放：间隔被拉长
QUIET_SCALE = 0.5
# 热闹时的频率缩放：间隔被压缩
BUSY_SCALE = 1.8
# 视作「满负荷活跃」的每分钟事件数，用于对数归一化
BUSY_APM = 60.0

# 高斯采样截断到 ±2σ，避免偶发的超长卡顿
_SIGMA_CLAMP = 2.0


def gaussian_jitter_sec(config: EngineConfig) -> float:
    """Randomised pre-send pause, in seconds.

    With ``gaussian_jitter`` enabled the delay is ``jitter_ms`` plus a
    normal-distributed offset of ``jitter_ms * jitter_sigma_ratio``, truncated
    at ±2σ and floored at zero. Otherwise it falls back to the legacy
    triangular distribution.
    """
    base_ms = max(0.0, float(config.jitter_ms))
    if base_ms == 0:
        return 0.0
    if not config.gaussian_jitter:
        u = random.random() + random.random() - 1  # triangular in [-1, 1]
        return max(0.0, (base_ms + u * base_ms * 0.8) / 1000)
    sigma_ms = base_ms * max(0.0, config.jitter_sigma_ratio)
    if sigma_ms == 0:
        return base_ms / 1000
    z = random.gauss(0.0, 1.0)
    z = max(-_SIGMA_CLAMP, min(_SIGMA_CLAMP, z))
    return max(0.0, (base_ms + z * sigma_ms) / 1000)


def typing_delay_sec(text: str, config: EngineConfig) -> float:
    """How long a human would plausibly need to type ``text``.

    Scales linearly with character count, adds ±35% noise, then clamps into
    ``[typing_min_sec, typing_max_sec]`` so neither ``"好"`` nor a 30-character
    paragraph produces an absurd wait.
    """
    if config.typing_ms_per_char <= 0 or not text:
        return 0.0
    raw = len(text) * config.typing_ms_per_char / 1000
    noisy = raw * random.uniform(0.65, 1.35)
    return max(config.typing_min_sec, min(config.typing_max_sec, noisy))


def activity_scale(apm: int, enabled: bool = True) -> float:
    """Map events-per-minute to a frequency multiplier in ``[0.5, 1.8]``.

    ``0.5`` means "stretch every interval 2×" (dead room, speak less);
    ``1.8`` means "compress every interval" (busy room, speak more). The mapping
    is logarithmic so the jump from 0→5 danmaku matters more than 55→60.
    """
    if not enabled:
        return 1.0
    if apm <= 0:
        return QUIET_SCALE
    level = min(1.0, math.log1p(apm) / math.log1p(BUSY_APM))
    return QUIET_SCALE + (BUSY_SCALE - QUIET_SCALE) * level


def cold_start_target(config: EngineConfig) -> float:
    """Randomised length of the silent observation window, in seconds.

    A fixed cold start makes every session look identical. We sample uniformly
    in ``[cold_start_sec, cold_start_sec + cold_start_jitter_sec]``.
    """
    base = max(0.0, float(config.cold_start_sec))
    spread = max(0.0, float(config.cold_start_jitter_sec))
    if spread == 0:
        return base
    return base + random.uniform(0.0, spread)


class WatchSimulator:
    """Emits non-message "viewing" behaviours on randomised intervals.

    The intervals are divided by the activity scale, so a lively room gets more
    likes and a dead room gets fewer. Each check also rolls ±25% jitter so the
    cadence never settles into a metronome.
    """

    #: (kind, config attribute, reason) triples driven by :meth:`poll`.
    SCHEDULE: tuple[tuple[str, str, str], ...] = (
        ("like", "like_every_sec", "模拟点赞"),
        ("share", "share_every_sec", "模拟分享"),
    )

    def __init__(self) -> None:
        self.started_at = time.time()
        self.last: dict[str, float] = {}
        self.history: list[WatchAction] = []

    def reset(self) -> None:
        self.started_at = time.time()
        self.last.clear()
        self.history.clear()

    def _due(self, key: str, every: float, now: float, scale: float) -> bool:
        if every <= 0:
            return False
        interval = every / max(0.2, scale)
        jitter = interval * random.uniform(-0.25, 0.25)
        target = max(0.0, interval + jitter)
        prev = self.last.get(key)
        elapsed = now - self.started_at if prev is None else now - prev
        return elapsed >= target

    def poll(self, config: EngineConfig, scale: float = 1.0) -> list[WatchAction]:
        """Return any watch actions that came due. Empty list when nothing is due."""
        if not config.watch_actions:
            return []
        now = time.time()
        due: list[WatchAction] = []
        for kind, attr, reason in self.SCHEDULE:
            every = float(getattr(config, attr, 0) or 0)
            if self._due(kind, every, now, scale):
                self.last[kind] = now
                due.append(WatchAction(kind=kind, ts=now, reason=reason))  # type: ignore[arg-type]
        if due:
            self.history.extend(due)
        return due
