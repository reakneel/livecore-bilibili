"""Tests for the hot-reloadable protobuf field schema.

The point of the schema is that a Bilibili field renumbering is a *config* edit
rather than a code change, so these tests exercise that end to end: renumber a
field on disk and assert the parser follows it on the next packet.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from livecore.parser import parse_notify
from livecore.pb import PbMessage
from livecore.schema import (
    CommandSchema,
    FieldPath,
    FieldReader,
    SchemaError,
    SchemaStore,
    default_schema_path,
    schema_for,
    set_schema_store,
    store_from_settings,
)


# ------------------------------------------------------------------ pb helpers


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


def _pb_payload(cmd: str, blob: bytes, **extra) -> dict:
    data = {"pb": base64.b64encode(blob).decode()}
    data.update(extra)
    return {"cmd": cmd, "data": data}


def _document(commands: dict) -> dict:
    return {"version": 1, "platform": "bilibili", "commands": commands}


def _write(tmp_path, commands: dict, name: str = "schema.pb.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(_document(commands)), encoding="utf-8")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return str(path)


def _touch(path: str, offset: float = 60.0) -> None:
    """Bump mtime deterministically (some filesystems have 1s granularity)."""
    current = os.path.getmtime(path)
    os.utime(path, (current + offset, current + offset))


# ------------------------------------------------------------------- FieldPath


def test_field_path_accepts_int_dotted_and_list():
    assert FieldPath.parse(9).segments == (9,)
    assert FieldPath.parse("9.3").segments == (9, 3)
    assert FieldPath.parse("9.3").key == "9.3"
    assert FieldPath.parse([10, 1, 2]).segments == (10, 1, 2)
    assert FieldPath.parse(" 9 . 3 ").depth == 2


@pytest.mark.parametrize("bad", [0, -1, "", "9.x", True, None, {}, "0.3"])
def test_field_path_rejects_invalid_values(bad):
    with pytest.raises(SchemaError):
        FieldPath.parse(bad)


# ------------------------------------------------------------- shipped schema


def test_shipped_schema_exposes_the_protobuf_commands():
    store = SchemaStore("bilibili")
    assert store.last_error is None
    assert store.protobuf_commands() == ("INTERACT_WORD_V2", "ONLINE_RANK_V3", "SEND_GIFT_V2")
    assert os.path.isfile(default_schema_path("bilibili"))


def test_shipped_interact_schema_matches_verified_field_numbers():
    """Ground truth verified against live packets (2026-09-13)."""
    schema = SchemaStore("bilibili").command("INTERACT_WORD_V2")
    assert schema is not None
    assert schema.path("uid").key == "1"
    assert schema.path("uname").key == "2"
    assert schema.path("identities").key == "4"
    assert schema.path("msg_type").key == "5"
    assert schema.path("timestamp").key == "7"
    # field 8 is a millisecond timestamp, not the "score" the community proto claims
    assert schema.path("timestamp_ms").key == "8"
    # medal lives at 9 (name/level) and is duplicated under uinfo at 22.3
    assert schema.path("medal_name").key == "9.3"
    assert schema.path("medal_level").key == "9.2"
    assert schema.path("uinfo_name").key == "22.2.1"
    assert schema.path("uinfo_medal_name").key == "22.3.1"
    # "recently interacted" tag, rendered as icon + text + type
    assert schema.path("relation_text").key == "23.2"
    # empty placeholder messages are deliberately not treated as drift
    assert schema.ignore == frozenset({12, 19, 24})


def test_shipped_gift_and_rank_schema():
    store = SchemaStore("bilibili")
    gift = store.command("SEND_GIFT_V2")
    assert gift.path("uname").key == "2"
    assert gift.path("gift_id").key == "10.1"
    assert gift.path("num").key == "10.3"
    assert gift.path("price").key == "10.5"
    assert gift.path("coin_type").key == "10.8"

    rank = store.command("ONLINE_RANK_V3")
    assert rank.path("list").key == "3"
    assert rank.item_fields("list")["uid"].key == "1"
    assert rank.item_fields("list")["rank"].key == "5"


def test_schema_for_returns_a_shared_store_per_platform():
    first = schema_for("bilibili")
    assert schema_for("bilibili") is first
    set_schema_store(None)
    try:
        assert schema_for("bilibili") is not first
    finally:
        set_schema_store(None)


# ------------------------------------------------------------------ validation


def test_load_rejects_a_document_without_commands(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(SchemaError, match="commands"):
        SchemaStore("bilibili", path=str(path)).load()
    # construction itself must stay safe for a running connection
    assert isinstance(SchemaStore("bilibili", path=str(path)).last_error, SchemaError)


def test_load_rejects_a_bad_field_number(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": "not-a-number"}}})
    with pytest.raises(SchemaError, match="non-numeric"):
        SchemaStore("bilibili", path=path).load()


def test_load_rejects_a_list_without_a_matching_field(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {}, "lists": {"rows": {"uid": 1}}}})
    with pytest.raises(SchemaError, match="no matching entry"):
        SchemaStore("bilibili", path=path).load()


def test_load_rejects_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SchemaError, match="not valid JSON"):
        SchemaStore("bilibili", path=str(path)).load()


def test_a_broken_document_does_not_raise_at_construction(tmp_path):
    """A bad schema edit must degrade, not take a running connection down."""
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    store = SchemaStore("bilibili", path=str(path))
    assert store.last_error is not None
    assert store.commands == {}
    assert store.reader("INTERACT_WORD_V2", PbMessage(b"")).int("uid", 7) == 7


def test_missing_schema_file_is_tolerated(tmp_path):
    store = SchemaStore("bilibili", path=str(tmp_path / "nope.json"))
    assert store.last_error is not None
    assert store.protobuf_commands() == ()


# --------------------------------------------------------------- hot reloading


def test_hot_reload_picks_up_a_renumbered_field(tmp_path):
    path = _write(tmp_path, {"INTERACT_WORD_V2": {"source": "pb", "fields": {"uname": 2}}})
    store = SchemaStore("bilibili", path=path, poll_sec=0.0)
    assert store.command("INTERACT_WORD_V2").path("uname").key == "2"

    # Bilibili moves uname from field 2 to field 3; edit config, no code change.
    _write(tmp_path, {"INTERACT_WORD_V2": {"source": "pb", "fields": {"uname": 3}}})
    _touch(path)
    assert store.maybe_reload() is True
    assert store.command("INTERACT_WORD_V2").path("uname").key == "3"


def test_maybe_reload_is_mtime_gated(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 1}}})
    store = SchemaStore("bilibili", path=path, poll_sec=5.0)
    seen: list[int] = []
    store.on_reload(lambda _old, _new: seen.append(1))
    assert store.maybe_reload() is False  # poll window has not elapsed yet
    assert seen == []


def test_reload_keeps_the_last_good_snapshot_on_a_bad_edit(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 1}}})
    store = SchemaStore("bilibili", path=path, poll_sec=0.0)
    errors: list[Exception] = []
    store.on_error(errors.append)

    path = tmp_path / "schema.pb.json"
    path.write_text("{ broken", encoding="utf-8")
    _touch(str(path))
    assert store.maybe_reload() is False
    assert store.command("X").path("uid").key == "1"
    assert errors and isinstance(errors[0], SchemaError)


def test_reload_listener_fires_only_on_change(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 1}}})
    store = SchemaStore("bilibili", path=path, poll_sec=0.0)
    changes: list[tuple] = []
    store.on_reload(lambda old, new: changes.append((sorted(old), sorted(new))))
    assert store.maybe_reload() is False
    _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 2}}})
    _touch(path)
    assert store.maybe_reload() is True
    assert changes == [(["X"], ["X"])]


# ------------------------------------------------------------------- overlays


def test_apply_overlay_wins_over_the_shipped_document(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 1, "uname": 2}}})
    store = SchemaStore("bilibili", path=path, poll_sec=0.0)
    assert store.command("X").path("uname").key == "2"

    assert store.apply_overlay({"commands": {"X": {"fields": {"uname": 5}}}}) is True
    assert store.command("X").path("uname").key == "5"
    # untouched fields survive the merge
    assert store.command("X").path("uid").key == "1"


def test_overlay_file_is_merged_on_top(tmp_path):
    base = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 1, "uname": 2}}}, "base.json")
    overlay = _write(tmp_path, {"X": {"fields": {"uname": 9}}}, "overlay.json")
    store = SchemaStore("bilibili", path=base, overlay_path=overlay, poll_sec=0.0)
    assert store.command("X").path("uname").key == "9"
    assert store.command("X").path("uid").key == "1"


def test_store_from_settings_builds_from_a_config_section(tmp_path):
    path = _write(tmp_path, {"X": {"source": "pb", "fields": {"uid": 1}}})
    store = store_from_settings("bilibili", {
        "schema_path": path,
        "poll_sec": 1.5,
        "commands": {"X": {"fields": {"uname": 4}}},
    })
    assert store.poll_sec == 1.5
    assert store.command("X").path("uname").key == "4"
    assert store.command("X").path("uid").key == "1"


# ------------------------------------------------------------------- reader


def test_field_reader_reads_every_shape():
    medal = _int_field(2, 26) + _bytes_field(3, "忆者".encode())
    blob = b"".join([
        _int_field(1, 999),
        _bytes_field(2, "夜飞霜雨".encode()),
        _bytes_field(4, _varint(3) + _varint(1)),
        _int_field(5, 2),
        _bytes_field(9, medal),
    ])
    schema = CommandSchema(cmd="X", source="pb", fields={
        "uid": FieldPath.parse(1),
        "uname": FieldPath.parse(2),
        "identities": FieldPath.parse(4),
        "msg_type": FieldPath.parse(5),
        "medal": FieldPath.parse(9),
        "medal_level": FieldPath.parse("9.2"),
        "medal_name": FieldPath.parse("9.3"),
        "absent": FieldPath.parse("99.1"),
    })
    reader = FieldReader(PbMessage(blob), schema)

    assert reader.known is True
    assert reader.int("uid") == 999
    assert reader.text("uname") == "夜飞霜雨"
    assert reader.ints("identities") == [3, 1]
    assert reader.message("medal").text(3) == "忆者"
    assert reader.text("medal_name") == "忆者"
    assert reader.int("medal_level") == 26
    assert reader.has("uid") is True
    assert reader.has("absent") is False


def test_field_reader_is_total_and_never_raises():
    schema = CommandSchema(cmd="X", source="pb", fields={"uid": FieldPath.parse(1)})
    for blob in (b"", b"\xff\xff\xff", _int_field(1, 5)):
        reader = FieldReader(PbMessage(blob), schema)
        assert reader.int("uid", 7) in (5, 7)
        assert reader.int("unknown", 7) == 7
        assert reader.text("unknown", "d") == "d"
        assert reader.ints("unknown") == []
        assert reader.items("unknown") == []
        assert reader.message("unknown").integer(1) == 0
    # A reader with no schema at all is still safe (unknown platform / command).
    bare = FieldReader(PbMessage(_int_field(1, 5)), None)
    assert bare.known is False
    assert bare.int("uid", 7) == 7
    assert bare.numbers() == [1]


def test_unmapped_reports_new_field_numbers():
    schema = CommandSchema(cmd="X", source="pb", fields={"uid": FieldPath.parse(1)})
    blob = _int_field(1, 1) + _int_field(2, 2) + _int_field(9, 3)
    assert FieldReader(PbMessage(blob), schema).unmapped() == [2, 9]
    assert FieldReader(PbMessage(blob), None).unmapped() == [1, 2, 9]


def test_items_reads_a_repeated_message_list():
    row1 = _int_field(1, 11) + _bytes_field(4, "甲".encode()) + _int_field(5, 1)
    row2 = _int_field(1, 22) + _bytes_field(4, "乙".encode()) + _int_field(5, 2)
    blob = _bytes_field(1, b"online_rank") + _bytes_field(3, row1) + _bytes_field(3, row2)
    schema = CommandSchema(
        cmd="R", source="pb",
        fields={"rank_type": FieldPath.parse(1), "list": FieldPath.parse(3)},
        lists={"list": {"uid": FieldPath.parse(1), "uname": FieldPath.parse(4), "rank": FieldPath.parse(5)}},
    )
    rows = FieldReader(PbMessage(blob), schema).items("list")
    assert [(row.int("uid"), row.text("uname"), row.int("rank")) for row in rows] == [
        (11, "甲", 1), (22, "乙", 2),
    ]


# ------------------------------------------------------- end-to-end with parser


def test_parser_follows_a_schema_renumbering_without_a_code_change(tmp_path):
    path = _write(tmp_path, {
        "INTERACT_WORD_V2": {"source": "pb", "fields": {"uid": 1, "uname": 2, "msg_type": 5}},
    })
    store = SchemaStore("bilibili", path=path, poll_sec=0.0)

    blob_v1 = _int_field(1, 999) + _bytes_field(2, "夜飞霜雨".encode()) + _int_field(5, 1)
    event = parse_notify(7, _pb_payload("INTERACT_WORD_V2", blob_v1), schema=store)
    assert event.user.uid == 999 and event.user.name == "夜飞霜雨"

    # Bilibili moves uname 2 -> 3. Only the config file changes.
    _write(tmp_path, {
        "INTERACT_WORD_V2": {"source": "pb", "fields": {"uid": 1, "uname": 3, "msg_type": 5}},
    })
    _touch(path)
    assert store.maybe_reload() is True

    blob_v2 = _int_field(1, 999) + _bytes_field(3, "夜飞霜雨".encode()) + _int_field(5, 1)
    event = parse_notify(7, _pb_payload("INTERACT_WORD_V2", blob_v2), schema=store)
    assert event.user.uid == 999 and event.user.name == "夜飞霜雨"
    assert event.kind == "enter"


def test_parser_uses_uinfo_when_the_top_level_name_is_masked():
    """Guest connections get a masked/empty uname; the pb uinfo still has it."""
    base = _bytes_field(1, "真名观众".encode()) + _bytes_field(2, b"http://face")
    medal = _bytes_field(1, "大母鹅".encode()) + _int_field(2, 31)
    uinfo = _int_field(1, 1430675) + _bytes_field(2, base) + _bytes_field(3, medal)
    blob = _int_field(1, 1430675) + _int_field(5, 1) + _bytes_field(22, uinfo)

    event = parse_notify(5050, _pb_payload("INTERACT_WORD_V2", blob))
    assert event.user.name == "真名观众"
    assert event.user.medal == "大母鹅"
    assert event.meta["medal_level"] == 31
    assert event.meta["face"] == "http://face"


def test_parser_surfaces_the_relation_tag_from_the_schema():
    relation = _bytes_field(1, b"http://icon.png") + _bytes_field(2, "曾经活跃过，近期与你互动较少".encode())
    blob = _int_field(1, 5) + _bytes_field(2, "观众".encode()) + _int_field(5, 1) + _bytes_field(23, relation)
    event = parse_notify(1, _pb_payload("INTERACT_WORD_V2", blob))
    assert event.meta["relation"] == "曾经活跃过，近期与你互动较少"


def test_parser_prefers_the_medal_blob_over_uinfo_medal():
    medal9 = _int_field(2, 26) + _bytes_field(3, "九号牌".encode())
    medal22 = _bytes_field(1, "二十二号牌".encode()) + _int_field(2, 5)
    uinfo = _bytes_field(3, medal22)
    blob = _int_field(1, 9) + _bytes_field(2, "观众".encode())
    blob += _int_field(5, 1) + _bytes_field(9, medal9) + _bytes_field(22, uinfo)

    event = parse_notify(1, _pb_payload("INTERACT_WORD_V2", blob))
    assert event.user.medal == "九号牌"
    assert event.meta["medal_level"] == 26


def test_send_gift_v2_reads_every_field_through_the_schema():
    gift = b"".join([
        _int_field(1, 31164),
        _bytes_field(2, "小心心".encode()),
        _int_field(3, 3),
        _int_field(5, 100),
        _int_field(6, 80),
        _bytes_field(8, b"gold"),
        _bytes_field(9, b"batch:1:2"),
        _int_field(10, 1789155046),
        _bytes_field(12, b"combo:1:2"),
    ])
    blob = _bytes_field(2, "夜飞霜雨".encode()) + _int_field(3, 123) + _bytes_field(10, gift)
    event = parse_notify(11, _pb_payload("SEND_GIFT_V2", blob))

    assert event.kind == "gift"
    assert event.user.name == "夜飞霜雨"
    assert event.gift.name == "小心心" and event.gift.num == 3 and event.gift.price == 100
    assert event.meta["gift_id"] == 31164
    assert event.meta["coin_type"] == "gold"
    assert event.meta["batch_combo_id"] == "batch:1:2"
    assert event.meta["combo_id"] == "combo:1:2"
    assert event.meta["gift_timestamp"] == 1789155046
    assert event.meta["discount_price"] == 80


def test_online_rank_v3_becomes_an_online_rank_event_with_real_names():
    row = lambda uid, name, rank: (  # noqa: E731
        _int_field(1, uid) + _bytes_field(4, name.encode()) + _int_field(5, rank) + _int_field(6, 3)
    )
    blob = _bytes_field(1, b"online_rank")
    blob += _bytes_field(3, row(11, "甲", 1)) + _bytes_field(3, row(22, "乙", 2))

    event = parse_notify(5050, _pb_payload("ONLINE_RANK_V3", blob))
    assert event.kind == "online_rank"
    assert event.meta["rank_type"] == "online_rank"
    assert event.meta["count"] == 2
    assert [row["name"] for row in event.meta["top"]] == ["甲", "乙"]
    assert "甲" in event.text and "高能榜" in event.text


def test_a_command_missing_from_the_schema_degrades_to_defaults():
    blob = _int_field(1, 999) + _bytes_field(2, "夜飞霜雨".encode()) + _int_field(5, 2)
    event = parse_notify(1, _pb_payload("INTERACT_WORD_V2", blob), schema=SchemaStore("bilibili", path="missing.json"))
    assert event is not None
    assert event.user.name == "观众"
    assert event.meta["event_ts"] == 0
