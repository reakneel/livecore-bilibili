"""LiveCore: modular live-room intelligence framework.

The core is platform agnostic: :class:`~livecore.connection.LiveConnection`
speaks to whatever platform adapter it is given, and ``bilibili`` is registered
as the default adapter so existing Bilibili code keeps working unchanged.
"""

from .client import BiliLiveClient
from .connection import LiveConnection
from .platforms import (
    BilibiliAdapter,
    FrameKind,
    InboundFrame,
    LiveEndpoint,
    PlatformAdapter,
    available_platforms,
    default_platform,
    get_adapter,
    register_adapter,
)
from .types import DanmuEndpoint, EngineConfig, LiveEvent

__all__ = [
    "BiliLiveClient",
    "BilibiliAdapter",
    "DanmuEndpoint",
    "EngineConfig",
    "FrameKind",
    "InboundFrame",
    "LiveConnection",
    "LiveEndpoint",
    "LiveEvent",
    "PlatformAdapter",
    "available_platforms",
    "default_platform",
    "get_adapter",
    "register_adapter",
]
__version__ = "0.1.0"


def __getattr__(name: str):
    """Lazily expose higher layers so importing the core stays cheap."""
    _LAZY = {
        "LiveEngine": "engine",
        "EventDispatcher": "dispatcher",
        "RoomContext": "context",
        "BehaviorScheduler": "scheduler",
        "WatchSimulator": "behavior",
        "ConfigStore": "config",
        "Alerter": "alert",
        "ReconnectWatch": "alert",
        "SqliteStore": "store",
        "RoomSupervisor": "multi",
        "ConnectionSupervisor": "supervisor",
        "MultiRoomSupervisor": "supervisor",
        "ConnectionHealth": "supervisor",
        "fetch_danmu_endpoint": "bili_http",
        "monitor_event": "monitor",
        "SchemaStore": "schema",
        "CommandSchema": "schema",
        "FieldReader": "schema",
        "FieldPath": "schema",
        "schema_for": "schema",
        "default_schema_path": "schema",
        "store_from_settings": "schema",
    }
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
