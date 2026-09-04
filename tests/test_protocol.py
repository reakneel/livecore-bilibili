from livecore.protocol import (
    OP_AUTH,
    decode_packets,
    encode_auth,
    encode_heartbeat,
    encode_packet,
    expand_packets,
    read_popularity,
)
import zlib


def test_roundtrip_auth():
    raw = encode_auth(123, "token-key")
    packets = decode_packets(raw)
    assert len(packets) == 1
    assert packets[0].op == OP_AUTH
    assert b"123" in packets[0].body
    assert b"token-key" in packets[0].body


def test_heartbeat_is_op2():
    raw = encode_heartbeat()
    pkt = decode_packets(raw)[0]
    assert pkt.op == 2
    assert pkt.body == b"[object Object]"


def test_zlib_expand():
    inner = encode_packet(5, b'{"cmd":"DANMU_MSG"}', protover=0)
    wrapped = encode_packet(5, zlib.compress(inner), protover=2)
    out = expand_packets(wrapped)
    assert any(b"DANMU_MSG" in p.body for p in out)


def test_popularity():
    assert read_popularity(b"\x00\x00\x04\xd2") == 1234
