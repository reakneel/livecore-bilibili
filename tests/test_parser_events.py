from __future__ import annotations

from livecore.parser import parse_notify


def test_danmu_msg_extracts_extended_metadata():
    event = parse_notify(
        123,
        {
            "cmd": "DANMU_MSG",
            "info": [
                [0, 1, 25, 16777215, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, {"extra": "{\"send_from_me\":false}"}],
                "hello",
                [42, "alice"],
                [1, "badge", 5, 123, 456, 789, 1, 0, 0, 0, 0, 0, 0],
                [], [], [], 3,
            ],
        },
    )
    assert event is not None
    assert event.kind == "danmaku"
    assert event.user is not None
    assert event.user.uid == 42
    assert event.user.medal == "badge"
    assert event.user.guard == 3
    assert event.meta["mode"] == 1
    assert event.meta["color"] == 16777215


def test_send_gift_extracts_money_and_medal():
    event = parse_notify(
        123,
        {
            "cmd": "SEND_GIFT",
            "data": {
                "uid": 7,
                "uname": "bob",
                "giftId": 99,
                "giftName": "小花花",
                "num": 2,
                "price": 100,
                "total_coin": 200,
                "coin_type": "gold",
                "medal_info": {"medal_name": "测试牌", "medal_level": 12},
            },
        },
    )
    assert event is not None
    assert event.kind == "gift"
    assert event.gift is not None
    assert event.gift.name == "小花花"
    assert event.gift.num == 2
    assert event.gift.price == 100
    assert event.meta["gift_id"] == 99
    assert event.meta["total_coin"] == 200
    assert event.user is not None
    assert event.user.medal == "测试牌"


def test_interact_word_v2_is_supported():
    event = parse_notify(
        123,
        {"cmd": "INTERACT_WORD_V2", "data": {"uid": 9, "uname": "carol", "msg_type": 2}},
    )
    assert event is not None
    assert event.kind == "follow"
    assert event.user is not None
    assert event.user.uid == 9


def test_super_chat_extracts_user_and_duration():
    event = parse_notify(
        123,
        {
            "cmd": "SUPER_CHAT_MESSAGE",
            "data": {
                "uid": 8,
                "message": "加油",
                "price": 30,
                "time": 30,
                "user_info": {"uname": "dave", "is_vip": 1},
                "medal_info": {"medal_name": "粉丝牌", "medal_level": 10},
            },
        },
    )
    assert event is not None
    assert event.kind == "superchat"
    assert event.meta["duration"] == 30
    assert event.meta["is_vip"] == 1
    assert event.user is not None
    assert event.user.medal == "粉丝牌"


def test_system_and_room_state_events_are_parsed():
    for cmd in ("LIVE", "PREPARING", "ROOM_CHANGE", "ROOM_REAL_TIME_MESSAGE_UPDATE", "WATCHED_CHANGE"):
        payload = {"cmd": cmd, "data": {"roomid": 123, "fans": 456, "num": 789}}
        if cmd == "WATCHED_CHANGE":
            payload["data"] = {"num": 789}
        event = parse_notify(123, payload)
        assert event is not None
        assert event.kind == ("popularity" if cmd == "WATCHED_CHANGE" else "system")
        assert event.raw_cmd == cmd
