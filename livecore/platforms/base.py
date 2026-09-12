"""Platform abstraction: one adapter per live platform, one neutral transport.

The transport layer (:mod:`livecore.connection`) only knows how to open a socket,
keep it alive, reconnect with backoff and fan events out. Everything that is
platform specific — how to resolve a room, how to build the auth/heartbeat
packets, how to turn a raw frame into :class:`LiveEvent` values — lives behind
:class:`PlatformAdapter`.

Adding a new platform therefore means writing one adapter and registering it.
No change to the transport, supervisor, engine or any other platform's adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping

from ..types import LiveEvent


class FrameKind(str, Enum):
    """What a raw inbound frame means, in platform-neutral terms."""

    AUTH_OK = "auth_ok"
    HEARTBEAT = "heartbeat"
    EVENT = "event"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LiveEndpoint:
    """Platform-neutral connection target for one room."""

    host: str
    port: int = 443
    token: str = ""
    room_id: int = 0
    path: str = "/sub"
    secure: bool = True
    #: Platform private handshake values (e.g. Bilibili's ``buvid3``).
    extras: Mapping[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        scheme = "wss" if self.secure else "ws"
        default_port = 443 if self.secure else 80
        if self.port in (0, default_port):
            return f"{scheme}://{self.host}{self.path}"
        return f"{scheme}://{self.host}:{self.port}{self.path}"

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "room_id": self.room_id,
            "secure": self.secure,
            "token_present": bool(self.token),
        }


@dataclass(frozen=True, slots=True)
class InboundFrame:
    """One decoded inbound frame handed back to the transport."""

    kind: FrameKind
    events: tuple[LiveEvent, ...] = ()
    popularity: int = 0
    op: int = -1
    detail: str = ""


class PlatformAdapter(ABC):
    """Everything the transport needs to speak one live platform's protocol.

    Subclasses must at least implement :meth:`fetch_endpoint`,
    :meth:`build_auth_packet`, :meth:`build_heartbeat_packet` and
    :meth:`decode_frame`.
    """

    #: Stable registry key, e.g. ``"bilibili"``.
    name: ClassVar[str] = ""
    #: Human readable label used by logs and CLIs.
    display_name: ClassVar[str] = ""

    #: Application-level heartbeat cadence. Bilibili disconnects a connection
    #: that stops sending ``op=2`` heartbeat packets for 30s (hard cut at 60s).
    heartbeat_interval_sec: ClassVar[float] = 30.0
    #: Send one heartbeat immediately after connecting, then every interval.
    heartbeat_immediate: ClassVar[bool] = True

    #: Whether the underlying websocket library should run its own protocol-level
    #: keepalive ping.
    #:
    #: This must stay ``False`` for platforms whose server does not answer
    #: RFC 6455 ping frames — Bilibili is one of them. With ``websockets``'
    #: defaults (``ping_interval=20``, ``ping_timeout=20``, ``close_timeout=10``)
    #: the client tears down and reconnects a perfectly healthy connection every
    #: 50 seconds. Application-level heartbeats are the only keepalive needed.
    transport_keepalive_ping: ClassVar[bool] = False

    open_timeout_sec: ClassVar[float] = 10.0
    close_timeout_sec: ClassVar[float] = 5.0
    #: Bounded inbound queue so a slow consumer throttles instead of OOMing.
    max_queue: ClassVar[int] = 512
    max_frame_bytes: ClassVar[int] = 2_000_000

    # ------------------------------------------------------------------ rooms

    def normalize_room_id(self, room_id: int) -> int:
        """Validate and canonicalize a room id supplied by a caller."""
        try:
            room_id = int(room_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.name}: room_id must be an integer") from exc
        if room_id <= 0:
            raise ValueError(f"{self.name}: room_id must be positive")
        return room_id

    def coerce_endpoint(self, endpoint: object) -> LiveEndpoint:
        """Accept either a neutral :class:`LiveEndpoint` or a platform native one."""
        if isinstance(endpoint, LiveEndpoint):
            return endpoint
        return self.convert_endpoint(endpoint)

    @abstractmethod
    def convert_endpoint(self, endpoint: object) -> LiveEndpoint:
        """Translate a platform native endpoint object into a neutral one."""

    @abstractmethod
    async def fetch_endpoint(self, room_id: int) -> LiveEndpoint:
        """Resolve a room id into a connectable endpoint (handshake / token / host)."""

    # -------------------------------------------------------------- transport

    def websocket_url(self, endpoint: LiveEndpoint) -> str:
        return endpoint.url

    def connection_headers(self, endpoint: LiveEndpoint) -> dict[str, str]:
        """Extra HTTP headers for the websocket handshake."""
        return {}

    def transport_options(self) -> dict[str, Any]:
        """Keyword arguments forwarded to ``websockets.connect``."""
        return {
            "max_size": self.max_frame_bytes,
            "open_timeout": self.open_timeout_sec,
            "close_timeout": self.close_timeout_sec,
            "max_queue": self.max_queue,
            "ping_interval": 20.0 if self.transport_keepalive_ping else None,
            "ping_timeout": 20.0 if self.transport_keepalive_ping else None,
        }

    # ---------------------------------------------------------------- protocol

    @abstractmethod
    def build_auth_packet(self, endpoint: LiveEndpoint) -> bytes:
        """First frame sent after the socket opens."""

    @abstractmethod
    def build_heartbeat_packet(self) -> bytes:
        """Keepalive frame, repeated every :attr:`heartbeat_interval_sec`."""

    @abstractmethod
    def decode_frame(self, raw: bytes, room_id: int) -> list[InboundFrame]:
        """Translate one raw inbound frame into neutral frames."""

    # ------------------------------------------------------------------ misc

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
            "transport_keepalive_ping": self.transport_keepalive_ping,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<{type(self).__name__} name={self.name!r}>"
