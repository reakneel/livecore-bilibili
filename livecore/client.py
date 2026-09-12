"""Bilibili-bound connection client.

The reusable transport lives in :mod:`livecore.connection`; this module keeps the
historical ``BiliLiveClient`` name so existing code and examples keep working
while other platforms plug into the same transport through their own adapter.
"""

from __future__ import annotations

from .connection import EventHandler, LiveConnection, State, StateHandler
from .logger import RingLogger
from .platforms import BilibiliAdapter, PlatformAdapter, get_adapter
from .types import DanmuEndpoint

__all__ = [
    "BiliLiveClient",
    "EventHandler",
    "LiveConnection",
    "State",
    "StateHandler",
]

DEFAULT_ADAPTER = BilibiliAdapter()


class BiliLiveClient(LiveConnection):
    """``LiveConnection`` pre-bound to the Bilibili adapter."""

    def __init__(self, log: RingLogger, adapter: PlatformAdapter | None = None) -> None:
        super().__init__(log, adapter or DEFAULT_ADAPTER)

    async def start(self, endpoint: DanmuEndpoint | object) -> None:  # type: ignore[override]
        """Connect; accepts the legacy ``DanmuEndpoint`` as well as ``LiveEndpoint``."""
        await super().start(endpoint)


def client_for(platform: str | None = None, log: RingLogger | None = None) -> LiveConnection:
    """Build a connection bound to ``platform`` (default: the registered default)."""
    return LiveConnection(log or RingLogger(), get_adapter(platform))
