"""Bilibili live danmaku binary protocol (big-endian 16-byte header)."""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass

try:
    import brotli
except ImportError:  # pragma: no cover
    brotli = None

HEADER = struct.Struct(">IHHII")
HEADER_SIZE = 16
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_NOTIFY = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8
PROTO_RAW = 0
PROTO_INT = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3


@dataclass(slots=True)
class Packet:
    op: int
    protover: int
    body: bytes


def encode_packet(op: int, body: bytes, protover: int = PROTO_INT) -> bytes:
    return HEADER.pack(HEADER_SIZE + len(body), HEADER_SIZE, protover, op, 1) + body


def encode_auth(room_id: int, token: str, uid: int = 0) -> bytes:
    payload = {"uid": uid, "roomid": room_id, "protover": PROTO_ZLIB, "platform": "web", "type": 2, "key": token}
    return encode_packet(OP_AUTH, json.dumps(payload).encode("utf-8"), PROTO_RAW)


def encode_heartbeat() -> bytes:
    return encode_packet(OP_HEARTBEAT, b"[object Object]")


def decode_packets(buf: bytes) -> list[Packet]:
    packets: list[Packet] = []
    offset = 0
    while offset + HEADER_SIZE <= len(buf):
        packet_len, header_len, protover, op, _seq = HEADER.unpack_from(buf, offset)
        if header_len < HEADER_SIZE or packet_len < header_len:
            raise ValueError("invalid Bilibili packet header")
        if offset + packet_len > len(buf):
            raise ValueError("truncated Bilibili packet")
        packets.append(Packet(op=op, protover=protover, body=buf[offset + header_len:offset + packet_len]))
        offset += packet_len
    if offset != len(buf):
        raise ValueError("trailing bytes do not form a complete Bilibili packet")
    return packets


def expand_packets(buf: bytes) -> list[Packet]:
    out: list[Packet] = []
    for pkt in decode_packets(buf):
        if pkt.protover == PROTO_ZLIB and pkt.body:
            try:
                out.extend(expand_packets(zlib.decompress(pkt.body))); continue
            except zlib.error:
                pass
        elif pkt.protover == PROTO_BROTLI and pkt.body:
            if brotli is None:
                raise RuntimeError("Brotli support requires the 'brotli' package")
            try:
                out.extend(expand_packets(brotli.decompress(pkt.body))); continue
            except brotli.error:
                pass
        out.append(pkt)
    return out


def read_popularity(body: bytes) -> int:
    if len(body) < 4: return 0
    return struct.unpack(">I", body[:4])[0]


def parse_json_body(body: bytes):
    text = body.decode("utf-8", errors="replace").strip()
    if not text: return None
    try: return json.loads(text)
    except json.JSONDecodeError: return text
