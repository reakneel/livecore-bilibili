"""Platform adapters.

Importing this package registers the built-in adapters. The Bilibili adapter is
registered as the default so existing call sites keep working unchanged.
"""

from __future__ import annotations

from .base import FrameKind, InboundFrame, LiveEndpoint, PlatformAdapter
from .bilibili import BilibiliAdapter
from .registry import (
    DEFAULT_PLATFORM,
    available_platforms,
    default_platform,
    get_adapter,
    register_adapter,
    unregister_adapter,
)

register_adapter(BilibiliAdapter(), default=True)

__all__ = [
    "DEFAULT_PLATFORM",
    "BilibiliAdapter",
    "FrameKind",
    "InboundFrame",
    "LiveEndpoint",
    "PlatformAdapter",
    "available_platforms",
    "default_platform",
    "get_adapter",
    "register_adapter",
    "unregister_adapter",
]
