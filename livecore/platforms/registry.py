"""Adapter registry so callers can reach a platform by name.

``livecore`` ships with the Bilibili adapter registered as the default, but every
layer takes a platform name or an adapter instance so a second platform can be
registered from application code without touching the core::

    from livecore.platforms import register_adapter, get_adapter
    register_adapter(MyPlatformAdapter())
    conn = LiveConnection(log, get_adapter("my-platform"))
"""

from __future__ import annotations

from collections.abc import Iterable

from .base import PlatformAdapter

DEFAULT_PLATFORM = "bilibili"

_REGISTRY: dict[str, PlatformAdapter] = {}


def register_adapter(
    adapter: PlatformAdapter,
    *,
    aliases: Iterable[str] = (),
    default: bool = False,
    replace: bool = False,
) -> PlatformAdapter:
    """Register ``adapter`` under its own name plus any ``aliases``."""
    if not adapter.name:
        raise ValueError("adapter.name must be a non-empty string")
    keys = [adapter.name, *aliases]
    for key in keys:
        existing = _REGISTRY.get(key)
        if existing is not None and existing is not adapter and not replace:
            raise ValueError(f"platform {key!r} is already registered by {existing!r}")
    for key in keys:
        _REGISTRY[key] = adapter
    if default:
        global DEFAULT_PLATFORM
        DEFAULT_PLATFORM = adapter.name
    return adapter


def unregister_adapter(name: str) -> PlatformAdapter | None:
    """Remove every alias pointing at the adapter registered as ``name``."""
    adapter = _REGISTRY.pop(name, None)
    if adapter is None:
        return None
    for key in [key for key, value in _REGISTRY.items() if value is adapter]:
        _REGISTRY.pop(key, None)
    return adapter


def get_adapter(name: str | None = None) -> PlatformAdapter:
    """Resolve an adapter by registry key; ``None`` returns the default."""
    key = name or DEFAULT_PLATFORM
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(f"unknown platform {key!r}; registered: {known}") from exc


def available_platforms() -> list[str]:
    """Registered platform names, default first."""
    names = sorted({adapter.name for adapter in _REGISTRY.values()})
    if DEFAULT_PLATFORM in names:
        names.remove(DEFAULT_PLATFORM)
        names.insert(0, DEFAULT_PLATFORM)
    return names


def default_platform() -> str:
    return DEFAULT_PLATFORM
