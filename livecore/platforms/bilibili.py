"""Bilibili Live adapter: HTTP handshake + danmaku binary protocol."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .. import parser as parser_mod
from .. import protocol as proto
from ..bili_http import HttpConfig, fetch_danmu_endpoint
from ..schema import SchemaStore, schema_for
from ..types import DanmuEndpoint, LiveEvent
from .base import FrameKind, InboundFrame, LiveEndpoint, PlatformAdapter


class BilibiliAdapter(PlatformAdapter):
    """Speaks the Bilibili live danmaku protocol (op=2 heartbeat, op=5 notify)."""

    name = "bilibili"
    display_name = "哔哩哔哩直播"

    #: Bilibili force-closes a connection that stops sending heartbeats for 30s.
    heartbeat_interval_sec = 30.0
    heartbeat_immediate = True
    #: Bilibili's comet servers do not answer RFC 6455 ping frames.
    transport_keepalive_ping = False

    HEARTBEAT_BODY = b"[object Object]"
    DEFAULT_WEB_ORIGIN = "https://live.bilibili.com"

    def __init__(
        self,
        *,
        http_config: HttpConfig | None = None,
        protover: int = proto.PROTO_ZLIB,
        uid: int = 0,
        auth_token: str = "",
        buvid: str = "",
        origin: str = DEFAULT_WEB_ORIGIN,
        schema: SchemaStore | None = None,
    ) -> None:
        self.http_config = http_config or HttpConfig()
        self.protover = protover
        self.uid = uid
        #: Optional SESSDATA/access token for authenticated mode (see HttpConfig.require_token).
        self.auth_token = auth_token
        #: buvid3 cookie value; getDanmuInfo expects one since 2025-06-27.
        self.buvid = buvid
        self.origin = origin
        #: Hot-reloadable protobuf field map. Editing the schema file (or pushing
        #: an overlay) changes how *_V2 commands decode on a live connection.
        self.schema = schema or schema_for(self.name)

    # ------------------------------------------------------------------ rooms

    def convert_endpoint(self, endpoint: object) -> LiveEndpoint:
        if isinstance(endpoint, DanmuEndpoint):
            return LiveEndpoint(
                host=endpoint.host,
                port=endpoint.wss_port,
                token=endpoint.token,
                room_id=endpoint.room_id,
                path="/sub",
                secure=True,
            )
        wss_port = getattr(endpoint, "wss_port", None)
        host = getattr(endpoint, "host", None)
        if host is None or wss_port is None:
            raise TypeError("bilibili: expected DanmuEndpoint or LiveEndpoint")
        return LiveEndpoint(
            host=str(host),
            port=int(wss_port),
            token=str(getattr(endpoint, "token", "") or ""),
            room_id=int(getattr(endpoint, "room_id", 0) or 0),
            path="/sub",
            secure=True,
        )

    async def fetch_endpoint(self, room_id: int) -> LiveEndpoint:
        room_id = self.normalize_room_id(room_id)
        native = await fetch_danmu_endpoint(room_id, config=self.http_config)
        endpoint = self.convert_endpoint(native)
        if self.buvid and not endpoint.extras.get("buvid"):
            endpoint = replace(endpoint, extras={**endpoint.extras, "buvid": self.buvid})
        return endpoint

    # -------------------------------------------------------------- transport

    def connection_headers(self, endpoint: LiveEndpoint) -> dict[str, str]:
        return {
            "Origin": self.origin,
            "Referer": f"{self.origin}/{endpoint.room_id}",
        }

    # ---------------------------------------------------------------- protocol

    def build_auth_packet(self, endpoint: LiveEndpoint) -> bytes:
        token = self.auth_token or endpoint.token
        buvid = str(endpoint.extras.get("buvid") or self.buvid)
        return proto.encode_auth(
            endpoint.room_id,
            token,
            uid=self.uid,
            protover=self.protover,
            buvid=buvid,
        )

    def build_heartbeat_packet(self) -> bytes:
        return proto.encode_packet(proto.OP_HEARTBEAT, self.HEARTBEAT_BODY)

    def decode_frame(self, raw: bytes, room_id: int) -> list[InboundFrame]:
        frames: list[InboundFrame] = []
        for packet in proto.expand_packets(raw, strict=False):
            if packet.op == proto.OP_AUTH_REPLY:
                frames.append(InboundFrame(kind=FrameKind.AUTH_OK, op=packet.op))
            elif packet.op == proto.OP_HEARTBEAT_REPLY:
                frames.append(
                    InboundFrame(
                        kind=FrameKind.HEARTBEAT,
                        popularity=proto.read_popularity(packet.body),
                        op=packet.op,
                    )
                )
            elif packet.op == proto.OP_NOTIFY:
                frame = self._decode_notify(room_id, packet.body)
                if frame is not None:
                    frames.append(frame)
        return frames

    def _decode_notify(self, room_id: int, body: bytes) -> InboundFrame | None:
        payload: Any = proto.parse_json_body(body)
        if payload is None:
            return None
        event: LiveEvent | None = parser_mod.parse_notify(room_id, payload, schema=self.schema)
        if event is None:
            return None
        return InboundFrame(kind=FrameKind.EVENT, events=(event,), op=proto.OP_NOTIFY)

    # ------------------------------------------------------------------- misc

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described["schema"] = self.schema.describe()
        return described

    def apply_protobuf_settings(self, settings: dict[str, Any] | None) -> bool:
        """Push the ``protobuf`` section of the external config into the schema.

        ``{"schema_path": ..., "overlay_path": ..., "poll_sec": ..., "commands": {...}}``
        — the ``commands`` sub-object is merged as an in-memory overlay, so a
        field renumbering can be hot-applied from ``config.json`` alone.
        """
        settings = dict(settings or {})
        commands = settings.get("commands")
        if not isinstance(commands, dict) or not commands:
            return False
        return self.schema.apply_overlay({"commands": commands})
