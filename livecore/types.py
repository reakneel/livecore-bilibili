from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EventKind = Literal[
    "danmaku", "gift", "enter", "follow", "share", "guard", "superchat",
    "like", "system", "popularity",
]
Sentiment = Literal["positive", "neutral", "negative"]
SuggestionSource = Literal["rule", "ai", "schedule", "random"]
WatchKind = Literal["like", "share", "stay"]


@dataclass(slots=True)
class LiveUser:
    uid: int
    name: str
    guard: int = 0
    medal: str = ""


@dataclass(slots=True)
class GiftInfo:
    name: str
    num: int
    price: int


@dataclass(slots=True)
class LiveEvent:
    id: str
    ts: float
    kind: EventKind
    room_id: int
    user: LiveUser | None = None
    text: str = ""
    gift: GiftInfo | None = None
    sentiment: Sentiment | None = None
    raw_cmd: str = ""
    popularity: int = 0


@dataclass(slots=True)
class Suggestion:
    id: str
    ts: float
    text: str
    reason: str
    source: SuggestionSource
    in_reply_to: str = ""
    status: Literal["queued", "accepted", "dismissed", "expired"] = "queued"


@dataclass(slots=True)
class WatchAction:
    kind: WatchKind
    ts: float
    reason: str = ""


@dataclass(slots=True)
class EngineConfig:
    persona: str = "你是直播间里的热心观众，说话短、口语化、真诚，不超过 24 字。"
    reply_max_len: int = 24
    cold_start_sec: float = 12
    check_in_min: float = 5
    min_gap_sec: float = 12
    activity_boost: bool = True
    auto_suggest: bool = True
    jitter_ms: int = 1200
    cold_start_jitter_sec: float = 8
    gaussian_jitter: bool = True
    jitter_sigma_ratio: float = 0.35
    typing_ms_per_char: int = 45
    typing_min_sec: float = 0.6
    typing_max_sec: float = 3.5
    # Outbound-like viewer actions are opt-in for production safety.
    watch_actions: bool = False
    like_every_sec: float = 150
    share_every_sec: float = 900


@dataclass(slots=True)
class DanmuEndpoint:
    host: str
    wss_port: int
    token: str
    room_id: int
