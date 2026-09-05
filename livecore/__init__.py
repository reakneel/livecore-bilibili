"""LiveCore: modular Bilibili live-room intelligence framework."""

from .client import BiliLiveClient
from .types import DanmuEndpoint, EngineConfig, LiveEvent

__all__ = ["BiliLiveClient", "LiveEvent", "EngineConfig", "DanmuEndpoint"]
__version__ = "0.1.0"


def __getattr__(name: str):
    """Lazily expose the higher layers so ``import livecore`` stays cheap.

    Phase 5 pulls in optional heavy machinery (sqlite, config loading); keeping
    these behind a lazy attribute means ``from livecore import BiliLiveClient``
    never pays for them.
    """
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
        "fetch_danmu_endpoint": "bili_http",
    }
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
