from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from .pb import PbMessage
from .schema import FieldReader, SchemaStore, schema_for
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


def _pb(data: dict[str, Any]) -> PbMessage:
    """Decode the Base64 protobuf blob used by the V2 (*_V2) commands."""
    return PbMessage.from_base64(data.get("pb"))


def _reader(store: SchemaStore, cmd: str, data: dict[str, Any]) -> FieldReader:
    """Bind a command's field schema to the protobuf blob carried in ``data.pb``."""
    return store.reader(cmd, _pb(data))


def _placehold_name(text: Any) -> str:
    """Bilibili renders some usernames as ``<%NAME%>`` inside notice strings."""
    match = re.search(r"<%(.*?)%>", str(text or ""))
    return match.group(1).strip() if match else ""


def _uinfo_name(value: Any) -> str:
    """Read the nested ``uinfo.base.name`` shape used by ENTRY_EFFECT / LIKE_INFO."""
    base = _dict(_dict(value).get("base"))
    if base:
        return str(base.get("name") or base.get("uname") or "")
    return str(_dict(value).get("name") or "")


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


def _parse_gift(room_id: int, cmd: str, data: dict[str, Any], store: SchemaStore) -> LiveEvent:
    """Parse SEND_GIFT / COMBO_SEND / SPECIAL_GIFT, and the protobuf SEND_GIFT_V2.

    ``SEND_GIFT_V2`` no longer carries a JSON ``data`` body: everything moved into
    ``data.pb``, a Base64 protobuf blob. The field numbers are *not* hard-coded
    here — they come from the hot-reloadable schema
    (``livecore/schemas/bilibili.pb.json``), so a Bilibili renumbering is a config
    edit rather than a code change.
    """
    if cmd == "SEND_GIFT_V2" or (not data.get("uname") and data.get("pb")):
        proto = _reader(store, "SEND_GIFT_V2", data)
        name = proto.text("uname") or str(data.get("uname") or "") or "观众"
        gift_name = proto.text("gift_name") or "礼物"
        num = proto.int("num", 1) or 1
        price = proto.int("price")
        gift_id = proto.int("gift_id")
        coin_type = proto.text("coin_type")
        combo_id = proto.text("combo_id")
        batch_combo_id = proto.text("batch_combo_id")
        gift_ts = proto.int("gift_timestamp")
        discount_price = proto.int("discount_price")
        uid = proto.int("uid")
    else:
        name = str(data.get("uname") or data.get("sender_uname") or "观众")
        gift_name = str(data.get("giftName") or data.get("gift_name") or "礼物")
        num = _int(data.get("num") or data.get("gift_num") or data.get("combo_num") or data.get("gift_num_total"), 1)
        price = _int(data.get("price"))
        gift_id = _int(data.get("giftId") or data.get("gift_id"))
        coin_type = str(data.get("coin_type") or "")
        combo_id = str(data.get("combo_id") or "")
        batch_combo_id = str(data.get("batch_combo_id") or "")
        gift_ts = _int(data.get("timestamp"))
        discount_price = _int(data.get("discount_price"))
        uid = 0

    total_coin = _int(data.get("total_coin") or data.get("total_price"))
    medal_name, medal_level = _medal(data.get("medal_info") or data.get("medal"))
    user = _user(uid or data.get("uid") or data.get("sender_uid"), name, medal=data.get("medal_info"))
    user.medal = medal_name
    return _event(
        room_id, "gift", cmd,
        text=f"{name} 投喂 {gift_name} x{num}",
        user=user,
        gift=GiftInfo(name=gift_name, num=num, price=price),
        sentiment="positive",
        meta={
            "gift_id": gift_id,
            "total_coin": total_coin,
            "coin_type": coin_type,
            "action": str(data.get("action") or ""),
            "combo_id": combo_id,
            "batch_combo_id": batch_combo_id,
            "gift_timestamp": gift_ts,
            "discount_price": discount_price,
            "medal_level": medal_level,
            "guard_level": _int(data.get("guard_level")),
            "raw_data": data,
        },
    )


_INTERACT_KINDS: dict[int, tuple[str, str]] = {
    1: ("enter", "进入直播间"),
    2: ("follow", "关注了主播"),
    3: ("share", "分享了直播间"),
}


def _parse_interact(room_id: int, cmd: str, data: dict[str, Any], store: SchemaStore) -> LiveEvent:
    """Parse INTERACT_WORD / INTERACT_WORD_V2 (enter / follow / share).

    The V2 variant ships the payload as Base64 protobuf in ``data.pb``; the plain
    JSON fields are kept as a fallback for older streams and for tests. Field
    numbers are resolved through the hot-reloadable schema, and the medal shape
    is read from either the legacy ``fans_medal`` JSON, the pb ``medal`` blob
    (field 9) or the pb ``uinfo.medal`` blob (field 22.3) — whichever is present.
    """
    proto = _reader(store, "INTERACT_WORD_V2", data)
    uid = proto.int("uid") or data.get("uid")
    uname = proto.text("uname") or str(data.get("uname") or "")
    msg_type = proto.int("msg_type") or _int(data.get("msg_type"), 1)
    identities = proto.ints("identities") or data.get("identities") or []
    event_ts = proto.int("timestamp")

    kind, label = _INTERACT_KINDS.get(msg_type, ("enter", "进入直播间"))
    name = uname or proto.text("uinfo_name") or "观众"
    medal_name, medal_level = _medal(data.get("fans_medal") or data.get("fans_medal_info"))
    if not medal_name:
        medal_name = proto.text("medal_name") or proto.text("uinfo_medal_name")
        medal_level = proto.int("medal_level") or proto.int("uinfo_medal_level")
    user = _user(uid, name, guard=data.get("guard_level"), medal=data.get("fans_medal"))
    user.medal = medal_name
    return _event(
        room_id, kind, cmd, text=f"{name} {label}", user=user,
        meta={
            "msg_type": msg_type,
            "identities": identities,
            "event_ts": event_ts,
            "uname_color": proto.text("uname_color"),
            "face": proto.text("uinfo_face"),
            "relation": proto.text("relation_text"),
            "medal_level": medal_level,
            "dmscore": _int(data.get("dmscore")),
            "raw_data": data,
        },
    )


def _parse_online_rank(room_id: int, cmd: str, data: dict[str, Any], store: SchemaStore) -> LiveEvent:
    """Parse the protobuf ONLINE_RANK_V3 high-energy board.

    Before the schema existed this command reached the generic ``system`` branch
    and the whole payload (which lives *only* in ``data.pb``) was thrown away.
    """
    proto = _reader(store, "ONLINE_RANK_V3", data)
    rows: list[dict[str, Any]] = []
    for item in proto.items("list"):
        rows.append({
            "uid": item.int("uid"),
            "name": item.text("uname") or item.text("uinfo_name") or "观众",
            "rank": item.int("rank"),
            "score": item.int("score"),
            "guard_level": item.int("guard_level"),
            "face": item.text("face"),
        })
    leaders = "、".join(f"#{row['rank']} {row['name']}" for row in rows[:3])
    return _event(
        room_id, "online_rank", cmd,
        text=f"高能榜更新（{len(rows)} 人）：{leaders}" if leaders else f"高能榜更新（{len(rows)} 人）",
        popularity=len(rows),
        meta={
            "rank_type": proto.text("rank_type"),
            "count": len(rows),
            "top": rows[:10],
            "raw_data": data,
        },
    )


def parse_notify(room_id: int, payload: Any, *, schema: SchemaStore | None = None) -> LiveEvent | None:
    """Normalize Bilibili live commands into stable LiveEvent objects.

    The parser intentionally keeps command-specific protocol fields in ``event.meta``.
    This follows the broad command coverage used by bilibili-api-collect and
    bilibili-api-python while keeping the core LiveEvent API stable.

    ``schema`` is the hot-reloadable protobuf field map. When omitted, the
    process-wide default store for ``bilibili`` is used. It is polled (mtime
    gated) on every call, so editing the schema file takes effect on a live
    connection without a restart.
    """
    if not isinstance(payload, dict):
        return None

    store = schema or schema_for()
    store.maybe_reload()

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
    if cmd in {"SEND_GIFT", "SEND_GIFT_V2", "COMBO_SEND", "SPECIAL_GIFT", "POPULARITY_RED_POCKET_NEW"}:
        return _parse_gift(room_id, cmd, data, store)

    # INTERACT_WORD was replaced by INTERACT_WORD_V2 in newer streams.
    if cmd in {"INTERACT_WORD", "INTERACT_WORD_V2"}:
        return _parse_interact(room_id, cmd, data, store)

    # High-energy audience board; its entire payload lives in data.pb.
    if cmd == "ONLINE_RANK_V3":
        return _parse_online_rank(room_id, cmd, data, store)

    # ENTRY_EFFECT is the "xxx 来了" entrance animation; it is the only entrance
    # notification that still carries the real nickname on busy rooms.
    if cmd == "ENTRY_EFFECT":
        base = _dict(data.get("uinfo"))
        name = _uinfo_name(base) or str(data.get("uname") or "") or _placehold_name(data.get("copy_writing"))
        name = name or "观众"
        medal_name, medal_level = _medal(_dict(base).get("medal"))
        user = _user(data.get("uid"), name, guard=_int(data.get("privilege_type")), medal=_dict(base).get("medal"))
        user.medal = medal_name
        return _event(
            room_id, "enter", cmd, text=f"{name} 进入直播间", user=user,
            meta={
                "effect_id": _int(data.get("id")),
                "privilege_type": _int(data.get("privilege_type")),
                "medal_level": medal_level,
                "raw_data": data,
            },
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
        name = str(data.get("uname") or "") or _uinfo_name(data.get("uinfo")) or "观众"
        count = _int(data.get("click_count") or data.get("like_count") or data.get("count"), 1)
        return _event(
            room_id, "like", cmd, text=f"{name} 点赞 x{count}",
            user=_user(data.get("uid"), name), sentiment="positive",
            meta={"count": count, "raw_data": data},
        )

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

    if cmd == "POPULARITY_CHANGE":
        popularity = _int(data.get("popularity"))
        text = str(data.get("popularity_text") or f"当前人气 {popularity}")
        return _event(room_id, "popularity", cmd, text=text, popularity=popularity, meta={"raw_data": data})

    if cmd in {"WATCHED_CHANGE", "ONLINE_RANK_COUNT"}:
        popularity = _int(data.get("num") or data.get("online_count") or data.get("count"))
        return _event(room_id, "popularity", cmd, text=f"当前观看/热度 {popularity}", popularity=popularity, meta={"raw_data": data})

    # Preserve important but less stable live commands as system events instead
    # of silently dropping them. This makes future protocol additions observable.
    observable = {
        "ROOM_BLOCK_MSG", "WELCOME", "NEW_GUARD_COUNT", "MEDAL_UPGRADE", "RECALL_DANMU_MSG",
        "ANCHOR_LOT_START", "ANCHOR_LOT_END", "ANCHOR_LOT_AWARD", "COMMON_NOTICE_DANMAKU",
        "PK_BATTLE_PRE", "PK_BATTLE_START", "PK_BATTLE_PROCESS", "PK_BATTLE_SETTLE",
        "PK_BATTLE_END", "PK_END", "PK_ENDING", "PK_BEST_UNAME", "PK_MATCH_INFO",
        "PK_WINNING_STREAK", "PK_LOTTERY_START", "VOICE_JOIN_LIST", "VOICE_JOIN_STATUS",
        "VOICE_JOIN_ROOM_COUNT_INFO", "WIDGET_BANNER", "SYS_MSG", "TV_END", "WISH_BOTTLE",
        "RANK_CHANGED", "RANK_CHANGED_V2", "HOT_ROOM_NOTIFY",
        "WARNING", "CUT_OFF", "ROOM_SILENT_OFF", "DM_INTERACTION", "INTERACTIVE_USER",
    }
    if cmd in observable:
        name = str(data.get("uname") or data.get("username") or "")
        return _event(room_id, "system", cmd, text=f"{name}: {cmd}" if name else cmd, meta={"raw_data": data})

    return None
