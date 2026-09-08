from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .sentiment import analyze_sentiment
from .types import GiftInfo, LiveEvent, LiveUser


def _cmd_name(raw: str) -> str:
    return raw.split(":", 1)[0]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _medal(value: Any) -> tuple[str, int]:
    obj = _dict(value)
    if obj:
        return str(obj.get("medal_name") or obj.get("name") or ""), _int(obj.get("medal_level"))
    if isinstance(value, list):
        return (str(value[1]) if len(value) > 1 else ""), (_int(value[2]) if len(value) > 2 else 0)
    return "", 0


def _user(uid: Any, name: Any, *, guard: Any = 0, medal: Any = None) -> LiveUser:
    medal_name, _ = _medal(medal)
    return LiveUser(uid=_int(uid), name=str(name or "匿名"), guard=_int(guard), medal=medal_name)


def _event(room_id: int, kind: Any, cmd: str, *, text: str = "", user: LiveUser | None = None,
           gift: GiftInfo | None = None, sentiment: Any = None, popularity: int = 0,
           meta: dict[str, Any] | None = None) -> LiveEvent:
    return LiveEvent(
        id=uuid.uuid4().hex[:12],
        ts=time.time(),
        kind=kind,
        room_id=room_id,
        user=user,
        text=text,
        gift=gift,
        sentiment=sentiment,
        raw_cmd=cmd,
        popularity=popularity,
        meta=meta or {},
    )


def parse_notify(room_id: int, payload: Any) -> LiveEvent | None:
    """Normalize Bilibili live commands into stable LiveEvent objects.

    The parser intentionally keeps command-specific protocol fields in ``event.meta``.
    This follows the broad command coverage used by bilibili-api-collect and
    bilibili-api-python while keeping the core LiveEvent API stable.
    """
    if not isinstance(payload, dict):
        return None

    cmd = _cmd_name(str(payload.get("cmd", "")))
    data = _dict(payload.get("data"))

    # DANMU_MSG: the legacy ``info`` array is still the richest public source.
    if cmd == "DANMU_MSG":
        info = payload.get("info") or []
        text = str(info[1]) if len(info) > 1 else ""
        if not text:
            return None
        user_arr = info[2] if len(info) > 2 and isinstance(info[2], list) else []
        medal = info[3] if len(info) > 3 else None
        danmu = info[0] if len(info) > 0 and isinstance(info[0], list) else []
        extra = _dict(danmu[15]) if len(danmu) > 15 and isinstance(danmu[15], dict) else {}
        extra_raw = extra.get("extra")
        if isinstance(extra_raw, str):
            try:
                extra.update(_dict(json.loads(extra_raw)))
            except (TypeError, ValueError):
                pass
        medal_name, medal_level = _medal(medal)
        user = _user(
            user_arr[0] if user_arr else 0,
            user_arr[1] if len(user_arr) > 1 else "匿名",
            guard=info[7] if len(info) > 7 else 0,
            medal=medal,
        )
        user.medal = medal_name
        return _event(
            room_id, "danmaku", cmd, text=text, user=user,
            sentiment=analyze_sentiment(text),
            meta={
                "mode": _int(danmu[1]) if len(danmu) > 1 else 0,
                "font_size": _int(danmu[2]) if len(danmu) > 2 else 0,
                "color": _int(danmu[3]) if len(danmu) > 3 else 0,
                "timestamp": _int(danmu[4]) if len(danmu) > 4 else 0,
                "medal_level": medal_level,
                "extra": extra,
            },
        )

    # Gifts and combo gifts.
    if cmd in {"SEND_GIFT", "COMBO_SEND", "SPECIAL_GIFT", "POPULARITY_RED_POCKET_NEW"}:
        name = str(data.get("uname") or data.get("sender_uname") or "观众")
        gift_name = str(data.get("giftName") or data.get("gift_name") or "礼物")
        num = _int(data.get("num") or data.get("gift_num") or data.get("gift_num_total"), 1)
        price = _int(data.get("price"))
        total_coin = _int(data.get("total_coin") or data.get("total_price"))
        medal_name, medal_level = _medal(data.get("medal_info") or data.get("medal"))
        user = _user(data.get("uid") or data.get("sender_uid"), name, medal=data.get("medal_info"))
        user.medal = medal_name
        return _event(
            room_id, "gift", cmd,
            text=f"{name} 投喂 {gift_name} x{num}",
            user=user,
            gift=GiftInfo(name=gift_name, num=num, price=price),
            sentiment="positive",
            meta={
                "gift_id": _int(data.get("giftId") or data.get("gift_id")),
                "total_coin": total_coin,
                "coin_type": data.get("coin_type", ""),
                "action": data.get("action", ""),
                "batch_combo_id": data.get("batch_combo_id", ""),
                "medal_level": medal_level,
                "guard_level": _int(data.get("guard_level")),
                "raw_data": data,
            },
        )

    # INTERACT_WORD was replaced by INTERACT_WORD_V2 in newer streams.
    if cmd in {"INTERACT_WORD", "INTERACT_WORD_V2"}:
        msg_type = _int(data.get("msg_type"), 1)
        kind = {2: "follow", 3: "share"}.get(msg_type, "enter")
        name = str(data.get("uname") or "观众")
        label = {"follow": "关注了主播", "share": "分享了直播间"}.get(kind, "进入直播间")
        medal_name, medal_level = _medal(data.get("fans_medal") or data.get("fans_medal_info"))
        user = _user(data.get("uid"), name, guard=data.get("guard_level"), medal=data.get("fans_medal"))
        user.medal = medal_name
        return _event(
            room_id, kind, cmd, text=f"{name} {label}", user=user,
            meta={"msg_type": msg_type, "medal_level": medal_level, "dmscore": _int(data.get("dmscore")), "raw_data": data},
        )

    if cmd in {"SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"}:
        user_info = _dict(data.get("user_info"))
        medal_name, medal_level = _medal(data.get("medal_info"))
        message = str(data.get("message") or "")
        name = str(user_info.get("uname") or data.get("uname") or "观众")
        user = _user(data.get("uid") or user_info.get("uid"), name, medal=data.get("medal_info"))
        user.medal = medal_name
        return _event(
            room_id, "superchat", cmd, text=message, user=user,
            gift=GiftInfo(name="醒目留言", num=1, price=_int(data.get("price"))),
            sentiment=analyze_sentiment(message),
            meta={
                "duration": _int(data.get("time")),
                "message_id": _int(data.get("id")),
                "is_vip": _int(user_info.get("is_vip")),
                "is_svip": _int(user_info.get("is_svip")),
                "medal_level": medal_level,
                "background_color": data.get("background_color", ""),
                "raw_data": data,
            },
        )

    if cmd == "SUPER_CHAT_MESSAGE_DELETE":
        return _event(room_id, "system", cmd, text="醒目留言已删除", meta={"ids": data.get("ids", []), "raw_data": data})

    if cmd in {"GUARD_BUY", "USER_TOAST_MSG", "WELCOME_GUARD", "FIRST_GUARD"}:
        name = str(data.get("username") or data.get("uname") or "观众")
        level = _int(data.get("guard_level") or data.get("guard_type") or data.get("privilege_type"))
        level_name = {1: "总督", 2: "提督", 3: "舰长"}.get(level, str(data.get("gift_name") or "大航海"))
        return _event(
            room_id, "guard", cmd,
            text=f"{name} 开通/进入 {level_name}",
            user=_user(data.get("uid"), name, guard=level),
            gift=GiftInfo(name=level_name, num=_int(data.get("num"), 1), price=_int(data.get("price"))),
            sentiment="positive",
            meta={"guard_level": level, "raw_data": data},
        )

    if cmd in {"LIKE_INFO_V3_CLICK", "LIKE_INFO_V3_UPDATE", "LIKE_INFO_V3"}:
        name = str(data.get("uname") or "观众")
        count = _int(data.get("click_count") or data.get("like_count") or data.get("count"), 1)
        return _event(room_id, "like", cmd, text=f"{name} 点赞 x{count}", user=_user(data.get("uid"), name), sentiment="positive", meta={"count": count, "raw_data": data})

    if cmd in {"LIVE", "PREPARING", "ROOM_CHANGE", "ROOM_REAL_TIME_MESSAGE_UPDATE", "ROOM_RANK", "ATTENTION", "SHARE", "NOTICE_MSG", "SPECIAL_ATTENTION", "HOT_RANK_CHANGED", "HOT_RANK_SETTLEMENT", "HOT_RANK_SETTLEMENT_V2", "ACTIVITY_BANNER_UPDATE_V2"}:
        labels = {
            "LIVE": "直播已开始", "PREPARING": "直播已结束", "ROOM_CHANGE": "房间信息发生变化",
            "ROOM_REAL_TIME_MESSAGE_UPDATE": "直播间实时数据更新", "ROOM_RANK": "直播间排名更新",
            "ATTENTION": "收到关注", "SHARE": "收到分享", "NOTICE_MSG": "直播间通知",
            "SPECIAL_ATTENTION": "特别关注事件", "HOT_RANK_CHANGED": "热门榜排名变化",
            "HOT_RANK_SETTLEMENT": "热门榜结算", "HOT_RANK_SETTLEMENT_V2": "热门榜结算",
            "ACTIVITY_BANNER_UPDATE_V2": "活动榜单更新",
        }
        return _event(room_id, "system", cmd, text=labels.get(cmd, cmd), meta={"raw_data": data})

    if cmd in {"WATCHED_CHANGE", "ONLINE_RANK_COUNT"}:
        popularity = _int(data.get("num") or data.get("count"))
        return _event(room_id, "popularity", cmd, text=f"当前观看/热度 {popularity}", popularity=popularity, meta={"raw_data": data})

    # Preserve important but less stable live commands as system events instead
    # of silently dropping them. This makes future protocol additions observable.
    observable = {
        "ROOM_BLOCK_MSG", "ENTRY_EFFECT", "WELCOME", "SPECIAL_GIFT", "NEW_GUARD_COUNT",
        "ANCHOR_LOT_START", "ANCHOR_LOT_END", "ANCHOR_LOT_AWARD", "MEDAL_UPGRADE",
        "PK_BATTLE_PRE", "PK_BATTLE_START", "PK_BATTLE_PROCESS", "PK_BATTLE_SETTLE",
        "PK_BATTLE_END", "PK_END", "PK_ENDING", "PK_BEST_UNAME", "PK_MATCH_INFO",
        "PK_WINNING_STREAK", "PK_LOTTERY_START", "VOICE_JOIN_LIST", "VOICE_JOIN_STATUS",
        "VOICE_JOIN_ROOM_COUNT_INFO", "WIDGET_BANNER", "SYS_MSG", "TV_END", "WISH_BOTTLE",
    }
    if cmd in observable:
        name = str(data.get("uname") or data.get("username") or "")
        return _event(room_id, "system", cmd, text=f"{name}: {cmd}" if name else cmd, meta={"raw_data": data})

    return None
