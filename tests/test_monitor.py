from livecore.monitor import monitor_event
from livecore.types import GiftInfo, LiveEvent, LiveUser


def test_monitor_projection_contains_user_gift_amount_and_meta_summary():
    event = LiveEvent(
        id="evt1",
        ts=1.0,
        kind="gift",
        room_id=123,
        user=LiveUser(uid=7, name="测试用户", guard=3, medal="粉丝牌"),
        gift=GiftInfo(name="小心心", num=3, price=100),
        meta={"gift_id": 1, "total_coin": 300, "coin_type": "gold", "raw_data": {"secret": "hidden"}},
    )
    payload = monitor_event(event)
    assert payload["kind"] == "gift"
    assert payload["kind_label"] == "礼物"
    assert payload["user"]["name"] == "测试用户"
    assert payload["gift"] == {"name": "小心心", "num": 3, "unit_price": 100}
    assert payload["amount"] == {"value": 300, "currency": "gold_coin"}
    assert "gift_id=1" in payload["meta_summary"]
    assert "raw_data" not in payload["meta_summary"]


def test_monitor_projection_falls_back_to_unit_price_times_count():
    event = LiveEvent(
        id="evt2", ts=1.0, kind="gift", room_id=123,
        gift=GiftInfo(name="礼物", num=2, price=50),
    )
    assert monitor_event(event)["amount"] == {"value": 100, "currency": "gold_coin"}
