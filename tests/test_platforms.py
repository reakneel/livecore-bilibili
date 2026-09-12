"""Tests for the platform abstraction layer and the transport-level fixes."""

from __future__ import annotations

import base64
import json

import pytest

from livecore import protocol as proto
from livecore.client import BiliLiveClient
from livecore.connection import LiveConnection
from livecore.logger import RingLogger
from livecore.parser import parse_notify
from livecore.pb import PbMessage
from livecore.platforms import (
    BilibiliAdapter,
    FrameKind,
    LiveEndpoint,
    PlatformAdapter,
    available_platforms,
    default_platform,
    get_adapter,
    register_adapter,
    unregister_adapter,
)
from livecore.types import DanmuEndpoint


# --------------------------------------------------------------------- registry


def test_bilibili_is_the_registered_default():
    assert default_platform() == "bilibili"
    assert available_platforms()[0] == "bilibili"
    adapter = get_adapter()
    assert isinstance(adapter, BilibiliAdapter)
    assert get_adapter("bilibili") is adapter


def test_unknown_platform_raises_with_helpful_message():
    with pytest.raises(KeyError, match="unknown platform"):
        get_adapter("does-not-exist")


def test_registering_a_second_platform_does_not_touch_bilibili():
    class FakeAdapter(PlatformAdapter):
        name = "fake"

        def convert_endpoint(self, endpoint):  # pragma: no cover - unused
            raise TypeError

        async def fetch_endpoint(self, room_id):  # pragma: no cover - unused
            raise NotImplementedError

        def build_auth_packet(self, endpoint):  # pragma: no cover - unused
            return b""

        def build_heartbeat_packet(self):  # pragma: no cover - unused
            return b""

        def decode_frame(self, raw, room_id):  # pragma: no cover - unused
            return []

    registered = register_adapter(FakeAdapter(), aliases=("fake-alias",))
    try:
        assert get_adapter("fake") is registered
        assert get_adapter("fake-alias") is registered
        assert available_platforms()[0] == "bilibili"
        # The Bilibili adapter is untouched by the new registration.
        assert get_adapter("bilibili").name == "bilibili"
    finally:
        assert unregister_adapter("fake") is registered
        assert get_adapter("bilibili").name == "bilibili"


# --------------------------------------------------------------------- endpoint


def test_live_endpoint_url_omits_default_port():
    assert LiveEndpoint(host="a.example").url == "wss://a.example/sub"
    assert LiveEndpoint(host="a.example", port=443).url == "wss://a.example/sub"
    assert LiveEndpoint(host="a.example", port=2245).url == "wss://a.example:2245/sub"
    assert LiveEndpoint(host="a.example", port=80, secure=False).url == "ws://a.example/sub"


# ------------------------------------------------------------------ transports


def test_bilibili_disables_library_keepalive_ping():
    """Regression guard.

    The server never answers RFC 6455 ping frames, so leaving ``websockets``'
    defaults (20s ping / 20s timeout / 10s close) in place drops a healthy
    connection every 50 seconds. The application-level ``op=2`` heartbeat is the
    only keepalive it honours.
    """
    adapter = BilibiliAdapter()
    options = adapter.transport_options()
    assert options["ping_interval"] is None
    assert options["ping_timeout"] is None
    assert adapter.transport_keepalive_ping is False
    assert adapter.heartbeat_interval_sec == 30.0
    assert adapter.heartbeat_immediate is True


def test_bilibili_heartbeat_packet_is_op2():
    packet = proto.decode_packets(BilibiliAdapter().build_heartbeat_packet())[0]
    assert packet.op == proto.OP_HEARTBEAT
    assert packet.body == b"[object Object]"


def test_bilibili_auth_packet_carries_token_room_and_buvid():
    adapter = BilibiliAdapter(uid=7, buvid="BV-123")
    endpoint = LiveEndpoint(host="h", port=2245, token="tok", room_id=999)
    payload = json.loads(proto.decode_packets(adapter.build_auth_packet(endpoint))[0].body)
    assert payload["roomid"] == 999
    assert payload["key"] == "tok"
    assert payload["uid"] == 7
    assert payload["buvid"] == "BV-123"
    assert payload["protover"] == proto.PROTO_ZLIB


def test_bilibili_auth_packet_omits_buvid_when_unset():
    endpoint = LiveEndpoint(host="h", token="tok", room_id=1)
    payload = json.loads(proto.decode_packets(BilibiliAdapter().build_auth_packet(endpoint))[0].body)
    assert "buvid" not in payload


def test_bilibili_accepts_legacy_danmu_endpoint():
    legacy = DanmuEndpoint(host="danmu.example", wss_port=2245, token="t", room_id=42)
    endpoint = BilibiliAdapter().coerce_endpoint(legacy)
    assert endpoint.host == "danmu.example"
    assert endpoint.port == 2245
    assert endpoint.url == "wss://danmu.example:2245/sub"


def test_bilibili_rejects_foreign_endpoint():
    with pytest.raises(TypeError):
        BilibiliAdapter().coerce_endpoint(object())


def test_normalize_room_id_validates():
    adapter = BilibiliAdapter()
    assert adapter.normalize_room_id("123") == 123
    with pytest.raises(ValueError, match="positive"):
        adapter.normalize_room_id(0)
    with pytest.raises(ValueError, match="integer"):
        adapter.normalize_room_id("abc")


# ------------------------------------------------------------------ decode frame


def _notify_frame(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return proto.encode_packet(proto.OP_NOTIFY, body, protover=proto.PROTO_RAW)


def test_decode_frame_reads_auth_heartbeat_and_events():
    adapter = BilibiliAdapter()

    auth = adapter.decode_frame(proto.encode_packet(proto.OP_AUTH_REPLY, b""), 1)
    assert [frame.kind for frame in auth] == [FrameKind.AUTH_OK]

    heartbeat = adapter.decode_frame(proto.encode_packet(proto.OP_HEARTBEAT_REPLY, b"\x00\x00\x04\xd2"), 1)
    assert heartbeat[0].kind is FrameKind.HEARTBEAT
    assert heartbeat[0].popularity == 1234

    danmaku = {"cmd": "DANMU_MSG", "info": [[], "hi", [1, "u"], [], [], [], [], 0]}
    frames = adapter.decode_frame(_notify_frame(danmaku), 7)
    assert frames[0].kind is FrameKind.EVENT
    assert frames[0].events[0].kind == "danmaku"
    assert frames[0].events[0].room_id == 7


def test_decode_frame_ignores_malformed_payload_without_raising():
    adapter = BilibiliAdapter()
    assert adapter.decode_frame(b"", 1) == []
    assert adapter.decode_frame(b"\x00\x01\x02", 1) == []
    # An unknown command is dropped rather than crashing the socket read loop.
    assert adapter.decode_frame(_notify_frame({"cmd": "NOT_A_REAL_COMMAND"}), 1) == []


def test_expand_packets_tolerates_garbage_only_in_non_strict_mode():
    assert proto.expand_packets(b"\x00\x01\x02", strict=False) == []
    with pytest.raises(ValueError):
        proto.expand_packets(b"\x00\x01\x02")


# ---------------------------------------------------------------- protobuf reader


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def test_pb_message_reads_nested_fields():
    gift = _int_field(1, 31164) + _bytes_field(2, "粉丝团灯牌".encode())
    buff = _int_field(1, 1653287944) + _bytes_field(2, "夜飞霜雨".encode())
    buff += _int_field(5, 2) + _bytes_field(10, gift)

    message = PbMessage(buff)
    assert message.integer(1) == 1653287944
    assert message.text(2) == "夜飞霜雨"
    assert message.integer(5) == 2
    assert message.message(10).integer(1) == 31164
    assert message.message(10).text(2) == "粉丝团灯牌"


def test_pb_message_handles_garbage():
    assert not PbMessage(b"")
    assert PbMessage(b"\xff\xff\xff").integer(1) == 0
    assert PbMessage.from_base64("not-base64!!").integer(1) == 0
    assert PbMessage.from_base64(None).text(1) == ""
    assert PbMessage.from_base64(base64.b64encode(_int_field(1, 1)).decode()).integer(1) == 1
    # A packed repeated varint field is unpacked on demand.
    assert PbMessage(_bytes_field(4, _varint(3) + _varint(1))).integers(4) == [3, 1]


# ----------------------------------------------------------------- v2 commands


def test_interact_word_v2_decodes_protobuf_payload():
    """The V2 payload dropped every plain JSON field in favour of ``data.pb``."""
    blob = _int_field(1, 1653287944) + _bytes_field(2, "夜飞霜雨".encode())
    blob += _int_field(5, 2) + _int_field(7, 1789155046)
    payload = {"cmd": "INTERACT_WORD_V2", "data": {"dmscore": 9, "pb": base64.b64encode(blob).decode()}}

    event = parse_notify(123, payload)
    assert event is not None
    assert event.kind == "follow"
    assert event.user is not None
    assert event.user.uid == 1653287944
    assert event.user.name == "夜飞霜雨"
    assert event.meta["msg_type"] == 2
    assert "夜飞霜雨" in event.text


def test_interact_word_v2_reads_medal_from_protobuf():
    medal = _int_field(2, 26) + _bytes_field(3, "忆者".encode())
    blob = _int_field(1, 999) + _bytes_field(2, "观众".encode()) + _int_field(5, 1) + _bytes_field(9, medal)
    payload = {"cmd": "INTERACT_WORD_V2", "data": {"pb": base64.b64encode(blob).decode()}}

    event = parse_notify(123, payload)
    assert event is not None
    assert event.kind == "enter"
    assert event.user is not None and event.user.medal == "忆者"
    assert event.meta["medal_level"] == 26


def test_interact_word_v2_still_accepts_plain_json():
    event = parse_notify(123, {"cmd": "INTERACT_WORD_V2", "data": {"uid": 9, "uname": "carol", "msg_type": 2}})
    assert event is not None
    assert event.kind == "follow"
    assert event.user is not None and event.user.uid == 9


def test_send_gift_v2_decodes_protobuf_payload():
    gift = (
        _int_field(1, 31164)
        + _bytes_field(2, "小心心".encode())
        + _int_field(3, 3)
        + _int_field(5, 100)
        + _bytes_field(8, b"gold")
        + _bytes_field(12, b"batch:gift:combo_id:1:2:31164:1")
    )
    blob = _bytes_field(2, "观众".encode()) + _bytes_field(10, gift)
    payload = {"cmd": "SEND_GIFT_V2", "data": {"dmscore": 56, "pb": base64.b64encode(blob).decode()}}

    event = parse_notify(123, payload)
    assert event is not None
    assert event.kind == "gift"
    assert event.gift is not None
    assert event.gift.name == "小心心"
    assert event.gift.num == 3
    assert event.gift.price == 100
    assert event.user is not None and event.user.name == "观众"
    assert event.meta["gift_id"] == 31164
    assert event.meta["coin_type"] == "gold"
    assert event.meta["combo_id"].startswith("batch:gift:combo_id")


def test_combo_send_uses_combo_num_fallback():
    event = parse_notify(123, {"cmd": "COMBO_SEND", "data": {"uid": 1, "uname": "bob", "gift_id": 4,
                                                             "gift_name": "辣条", "combo_num": 5, "price": 100}})
    assert event is not None
    assert event.gift is not None
    assert event.gift.name == "辣条"
    assert event.gift.num == 5


def test_entry_effect_becomes_enter_with_real_name():
    event = parse_notify(123, {
        "cmd": "ENTRY_EFFECT",
        "data": {
            "id": 380,
            "uid": 6370391,
            "privilege_type": 3,
            "copy_writing": "<%DMon%> 来了",
            "uinfo": {"base": {"name": "DMon"}, "medal": {"name": "诗雨雨", "level": 1}},
        },
    })
    assert event is not None
    assert event.kind == "enter"
    assert event.user is not None
    assert event.user.uid == 6370391
    assert event.user.name == "DMon"
    assert event.user.guard == 3
    assert event.user.medal == "诗雨雨"


def test_entry_effect_falls_back_to_placeholder_name():
    event = parse_notify(123, {"cmd": "ENTRY_EFFECT", "data": {"uid": 5, "copy_writing": "<%神秘观众%> 来了"}})
    assert event is not None
    assert event.user is not None
    assert event.user.name == "神秘观众"


def test_popularity_change_is_parsed():
    event = parse_notify(123, {"cmd": "POPULARITY_CHANGE",
                               "data": {"popularity": 10400080, "popularity_text": "1040.0万人气"}})
    assert event is not None
    assert event.kind == "popularity"
    assert event.popularity == 10400080


def test_online_rank_count_prefers_online_count():
    event = parse_notify(123, {"cmd": "ONLINE_RANK_COUNT", "data": {"count": 4903, "online_count": 4903}})
    assert event is not None
    assert event.kind == "popularity"
    assert event.popularity == 4903


# ----------------------------------------------------------------- client shim


def test_bili_live_client_is_a_platform_bound_live_connection():
    client = BiliLiveClient(RingLogger())
    assert isinstance(client, LiveConnection)
    assert client.platform == "bilibili"


@pytest.mark.asyncio
async def test_start_room_resolves_through_the_adapter(monkeypatch):
    adapter = BilibiliAdapter()
    endpoint = LiveEndpoint(host="h", port=443, token="t", room_id=5)
    seen: list[int] = []

    async def fake_fetch(room_id: int) -> LiveEndpoint:
        seen.append(room_id)
        return endpoint

    async def fake_start(resolved) -> None:
        seen.append(resolved.port)

    monkeypatch.setattr(adapter, "fetch_endpoint", fake_fetch)
    connection = LiveConnection(RingLogger(), adapter)
    monkeypatch.setattr(connection, "start", fake_start)

    assert await connection.start_room(5) == endpoint
    assert seen == [5, 443]
