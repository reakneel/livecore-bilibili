from __future__ import annotations

from typing import Any

from .types import LiveEvent


def _compact_meta(meta: dict[str, Any], *, limit: int = 180) -> str:
    """Build a deterministic, human-readable summary without dumping raw_data."""
    parts: list[str] = []
    for key, value in meta.items():
        if key in {"raw_data", "extra"}:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (dict, list)):
            continue
        parts.append(f"{key}={value}")
    text = " ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def monitor_event(event: LiveEvent) -> dict[str, Any]:
    """Return the stable monitoring projection consumed by CLI and LiveCore UI."""
    user = None
    if event.user is not None:
        user = {
            "uid": event.user.uid,
            "name": event.user.name,
            "guard": event.user.guard,
            "medal": event.user.medal,
        }

    gift = None
    amount = None
    if event.gift is not None:
        gift = {
            "name": event.gift.name,
            "num": event.gift.num,
            "unit_price": event.gift.price,
        }
        total = event.meta.get("total_coin")
        if not isinstance(total, int) or total <= 0:
            total = event.gift.price * max(event.gift.num, 1)
        amount = {"value": total, "currency": "gold_coin" if event.kind == "gift" else "CNY"}

    return {
        "kind": event.kind,
        "kind_label": {
            "danmaku": "弹幕", "gift": "礼物", "enter": "进场", "follow": "关注",
            "share": "分享", "guard": "大航海", "superchat": "醒目留言",
            "like": "点赞", "system": "系统", "popularity": "人气",
        }.get(event.kind, event.kind),
        "user": user,
        "gift": gift,
        "amount": amount,
        "text": event.text,
        "meta_summary": _compact_meta(event.meta),
        "raw_cmd": event.raw_cmd,
        "popularity": event.popularity,
    }
