"""Minimal, dependency-free protobuf field reader.

Bilibili has started moving several live commands (notably ``INTERACT_WORD_V2``
and ``SEND_GIFT_V2``) from plain JSON to a Base64-encoded protobuf blob carried in
``data.pb``. The full message definitions live in Bilibili's private schema, so we
only need a small, generic reader that lets the parser pull the handful of fields
it cares about without pulling in ``protobuf`` as a runtime dependency.

The reader is intentionally permissive: unknown fields, unknown wire types and
truncated buffers never raise. A malformed blob simply yields fewer fields.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from dataclasses import dataclass

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH = 2
WIRE_FIXED32 = 5


@dataclass(frozen=True, slots=True)
class PbValue:
    """One decoded protobuf field entry."""

    number: int
    wire_type: int
    value: int | bytes

    @property
    def raw_bytes(self) -> bytes:
        return self.value if isinstance(self.value, bytes) else b""

    def as_int(self) -> int:
        return self.value if isinstance(self.value, int) else 0

    def as_text(self) -> str:
        if not isinstance(self.value, bytes):
            return ""
        try:
            return self.value.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    def as_message(self) -> PbMessage:
        return PbMessage(self.raw_bytes) if isinstance(self.value, bytes) else PbMessage(b"")


def _read_varint(buf: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    index = offset
    while index < len(buf):
        byte = buf[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


class PbMessage:
    """Read-only view over one protobuf message."""

    __slots__ = ("_buf", "_fields")

    def __init__(self, buf: bytes) -> None:
        self._buf = buf
        self._fields: dict[int, list[PbValue]] | None = None

    def __bool__(self) -> bool:
        return bool(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    @classmethod
    def from_base64(cls, value: object) -> PbMessage:
        """Decode a Base64 ``data.pb`` string; returns an empty message on failure."""
        if not isinstance(value, str) or not value:
            return cls(b"")
        try:
            return cls(base64.b64decode(value, validate=False))
        except (binascii.Error, ValueError):
            return cls(b"")

    @property
    def fields(self) -> dict[int, list[PbValue]]:
        if self._fields is None:
            self._fields = self._parse()
        return self._fields

    def _parse(self) -> dict[int, list[PbValue]]:
        out: dict[int, list[PbValue]] = {}
        index = 0
        buf = self._buf
        while index < len(buf):
            try:
                key, index = _read_varint(buf, index)
            except ValueError:
                break
            number, wire_type = key >> 3, key & 0x7
            if number == 0:
                break
            if wire_type == WIRE_VARINT:
                try:
                    value, index = _read_varint(buf, index)
                except ValueError:
                    break
            elif wire_type == WIRE_LENGTH:
                try:
                    size, index = _read_varint(buf, index)
                except ValueError:
                    break
                if size < 0 or index + size > len(buf):
                    break
                value = buf[index:index + size]
                index += size
            elif wire_type == WIRE_FIXED32:
                if index + 4 > len(buf):
                    break
                value = buf[index:index + 4]
                index += 4
            elif wire_type == WIRE_FIXED64:
                if index + 8 > len(buf):
                    break
                value = buf[index:index + 8]
                index += 8
            else:
                break
            out.setdefault(number, []).append(PbValue(number=number, wire_type=wire_type, value=value))
        return out

    def values(self, number: int) -> list[PbValue]:
        return self.fields.get(number, [])

    def first(self, number: int) -> PbValue | None:
        entries = self.fields.get(number)
        return entries[0] if entries else None

    def integer(self, number: int, default: int = 0) -> int:
        entry = self.first(number)
        return entry.as_int() if entry is not None else default

    def text(self, number: int, default: str = "") -> str:
        entry = self.first(number)
        text = entry.as_text() if entry is not None else ""
        return text or default

    def blob(self, number: int) -> bytes:
        entry = self.first(number)
        return entry.raw_bytes if entry is not None else b""

    def message(self, number: int) -> PbMessage:
        return PbMessage(self.blob(number))

    def integers(self, number: int) -> list[int]:
        """Varint values for ``number``, plus unpacked values from a packed field."""
        out: list[int] = []
        for entry in self.values(number):
            if entry.wire_type == WIRE_VARINT:
                out.append(entry.as_int())
            elif entry.wire_type == WIRE_LENGTH:
                packed = PbMessage(entry.raw_bytes)
                index = 0
                buf = entry.raw_bytes
                while index < len(buf):
                    try:
                        value, index = _read_varint(buf, index)
                    except ValueError:
                        break
                    out.append(value)
                if not out and not packed:
                    continue
        return out

    def __iter__(self) -> Iterator[PbValue]:
        for entries in self.fields.values():
            yield from entries

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"PbMessage(fields={sorted(self.fields)}, size={len(self._buf)})"
