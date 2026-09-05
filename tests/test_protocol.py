import brotli
import pytest
import zlib

from livecore.protocol import OP_AUTH, decode_packets, encode_auth, encode_heartbeat, encode_packet, expand_packets, read_popularity


def test_roundtrip_auth():
    packets = decode_packets(encode_auth(123, "token-key"))
    assert len(packets) == 1 and packets[0].op == OP_AUTH
    assert b"123" in packets[0].body and b"token-key" in packets[0].body


def test_heartbeat_is_op2():
    pkt = decode_packets(encode_heartbeat())[0]
    assert pkt.op == 2 and pkt.body == b"[object Object]"


def test_zlib_expand():
    inner = encode_packet(5, b'{"cmd":"DANMU_MSG"}', protover=0)
    out = expand_packets(encode_packet(5, zlib.compress(inner), protover=2))
    assert any(b"DANMU_MSG" in p.body for p in out)


def test_brotli_expand_nested_packets():
    inner_a = encode_packet(5, b'{"cmd":"DANMU_MSG"}', protover=0)
    inner_b = encode_packet(5, b'{"cmd":"SEND_GIFT"}', protover=0)
    out = expand_packets(encode_packet(5, brotli.compress(inner_a + inner_b), protover=3))
    assert len(out) == 2 and b"DANMU_MSG" in out[0].body and b"SEND_GIFT" in out[1].body


def test_decode_rejects_invalid_header_length():
    raw = encode_packet(5, b"ok")
    broken = raw[:4] + (8).to_bytes(2, "big") + raw[6:]
    with pytest.raises(ValueError, match="invalid Bilibili packet header"):
        decode_packets(broken)


def test_decode_rejects_truncated_packet():
    with pytest.raises(ValueError, match="truncated Bilibili packet"):
        decode_packets(encode_packet(5, b"ok")[:-1])


def test_popularity():
    assert read_popularity(b"\x00\x00\x04\xd2") == 1234
