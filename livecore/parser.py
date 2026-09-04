from __future__ import annotations

import time
import uuid
from typing import Any

from .sentiment import analyze_sentiment
from .types import GiftInfo, LiveEvent, LiveUser


def _cmd_name(raw: str) -> str:
    return raw.split(":", 1)[0]


def parse_notify(room_id: int, payload: Any) -> LiveEvent | None:
    if not isinstance(payload, dict):
        return None
    cmd = _cmd_name(str(payload.get("cmd", "")))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    ts = time.time()
    eid = uuid.uuid4().hex[:12]

    if cmd == "DANMU_MSG":
        info = payload.get("info") or []
        text = str(info[1]) if len(info) > 1 else ""
        if not text:
            return None
        user_arr = info[2] if len(info) > 2 and isinstance(info[2], list) else []
        medal = info[3] if len(info) > 3 and isinstance(info[3], list) else []
        return LiveEvent(
            id=eid,
            ts=ts,
            kind="danmaku",
            room_id=room_id,
            text=text,
            sentiment=analyze_sentiment(text),
            raw_cmd=cmd,
            user=LiveUser(
                uid=int(user_arr[0]) if user_arr else 0,
                name=str(user_arr[1]) if len(user_arr) > 1 else "匿名",
                guard=int(info[7]) if len(info) > 7 and isinstance(info[7], int) else 0,
                medal=str(medal[1]) if len(medal) > 1 else "",
            ),
        )

    if cmd in {"SEND_GIFT", "POPULARITY_RED_POCKET_NEW"}:
        name = str(data.get("uname") or data.get("sender_uname") or "观众")
        gift_name = str(data.get("giftName") or data.get("gift_name") or "礼物")
        num = int(data.get("num") or 1)
        return LiveEvent(
            id=eid,
            ts=ts,
            kind="gift",
            room_id=room_id,
            raw_cmd=cmd,
            user=LiveUser(uid=int(data.get("uid") or 0), name=name),
            gift=GiftInfo(name=gift_name, num=num, price=int(data.get("price") or 0)),
            text=f"{name} 投喂 {gift_name} x{num}",
            sentiment="positive",
        )

    if cmd == "INTERACT_WORD":
        msg_type = int(data.get("msg_type") or 1)
        name = str(data.get("uname") or "观众")
        kind = {2: "follow", 3: "share"}.get(msg_type, "enter")
        label = {"follow": "关注了主播", "share": "分享了直播间"}.get(kind, "进入直播间")
        return LiveEvent(
            id=eid,
            ts=ts,
            kind=kind,  # type: ignore[arg-type]
            room_id=room_id,
            raw_cmd=cmd,
            user=LiveUser(uid=int(data.get("uid") or 0), name=name),
            text=f"{name} {label}",
            sentiment="neutral",
        )

    if cmd in {"SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"}:
        user = data.get("user_info") if isinstance(data.get("user_info"), dict) else {}
        name = str(user.get("uname") or "观众")
        message = str(data.get("message") or "")
        return LiveEvent(
            id=eid,
            ts=ts,
            kind="superchat",
            room_id=room_id,
            raw_cmd=cmd,
            user=LiveUser(uid=int(data.get("uid") or 0), name=name),
            text=message,
            gift=GiftInfo(name="醒目留言", num=1, price=int(data.get("price") or 0)),
            sentiment=analyze_sentiment(message),
        )

    if cmd == "GUARD_BUY":
        name = str(data.get("username") or "观众")
        gift_name = str(data.get("gift_name") or "舰长")
        return LiveEvent(
            id=eid,
            ts=ts,
            kind="guard",
            room_id=room_id,
            raw_cmd=cmd,
            user=LiveUser(uid=int(data.get("uid") or 0), name=name),
            gift=GiftInfo(name=gift_name, num=int(data.get("num") or 1), price=int(data.get("price") or 0)),
            text=f"{name} 开通了 {gift_name}",
            sentiment="positive",
        )

    if cmd == "LIKE_INFO_V3_CLICK":
        name = str(data.get("uname") or "观众")
        return LiveEvent(
            id=eid,
            ts=ts,
            kind="like",
            room_id=room_id,
            raw_cmd=cmd,
            user=LiveUser(uid=int(data.get("uid") or 0), name=name),
            text=f"{name} 点了赞",
            sentiment="positive",
        )

    if cmd == "LIVE":
        return LiveEvent(id=eid, ts=ts, kind="system", room_id=room_id, raw_cmd=cmd, text="直播已开始")
    if cmd == "PREPARING":
        return LiveEvent(id=eid, ts=ts, kind="system", room_id=room_id, raw_cmd=cmd, text="直播已结束")
    return None
