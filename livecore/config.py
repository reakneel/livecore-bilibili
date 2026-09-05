"""Phase 5.3 — externalised configuration with hot reload.

Credentials, room IDs and tuning knobs live in a JSON file outside the source
tree so they never end up in a commit (see ``config.example.json`` and the
``.gitignore`` entry for ``config.json``).

The store polls the file's mtime. A malformed edit is *not* fatal: the previous
snapshot stays live and the error is reported through the reload listeners, so a
running session survives a bad save.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, fields

from .types import EngineConfig

__all__ = ["AlertConfig", "StorageConfig", "ConfigError", "ConfigStore", "DEFAULT_CONFIG"]

DEFAULT_CONFIG: dict = {
    "rooms": [],
    "engine": {},
    "alert": {
        "enabled": False,
        "failure_threshold": 3,
        "cooldown_sec": 300,
        "channel": "log",
        "webhook_url": "",
        "email_to": "",
    },
    "storage": {
        "enabled": False,
        "path": "livecore.db",
        "retention_days": 7,
    },
    "ai": {"provider": "none"},
}


class ConfigError(RuntimeError):
    """Raised when a config file cannot be read or is structurally invalid."""


@dataclass(slots=True)
class AlertConfig:
    enabled: bool = False
    failure_threshold: int = 3
    cooldown_sec: float = 300
    channel: str = "log"
    webhook_url: str = ""
    email_to: str = ""


@dataclass(slots=True)
class StorageConfig:
    enabled: bool = False
    path: str = "livecore.db"
    retention_days: int = 7


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _pick(data: dict, cls: type) -> dict:
    """Keep only keys that ``cls`` actually declares, so typos can't crash a run."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


class ConfigStore:
    """Reads a JSON config file and re-reads it when the mtime changes."""

    def __init__(self, path: str, poll_sec: float = 2.0, autoload: bool = True) -> None:
        self.path = path
        self.poll_sec = poll_sec
        self._snapshot: dict = dict(DEFAULT_CONFIG)
        self._mtime: float | None = None
        # 从「刚刚检查过」开始，避免构造后第一次 maybe_reload 立刻穿透轮询窗口
        self._checked_at = time.time()
        self._listeners: list[Callable[[dict, dict], None]] = []
        self._error_listeners: list[Callable[[Exception], None]] = []
        if autoload:
            self.load()

    # ---------------------------------------------------------------- loading

    def load(self) -> dict:
        """(Re)read the file. Raises :class:`ConfigError` on unrecoverable input."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError as exc:
            raise ConfigError(f"配置文件不存在：{self.path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置根节点必须是 JSON 对象")
        merged = _merge(DEFAULT_CONFIG, raw)
        old = self._snapshot
        self._snapshot = merged
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None
        if old != merged:
            for fn in self._listeners:
                fn(old, merged)
        return merged

    def save(self, data: dict | None = None) -> None:
        payload = data if data is not None else self._snapshot
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._snapshot = _merge(DEFAULT_CONFIG, payload)

    def reload(self) -> bool:
        """Reload if the file changed. Returns True when the snapshot changed."""
        before = dict(self._snapshot)
        try:
            self.load()
        except ConfigError as exc:
            for fn in self._error_listeners:
                fn(exc)
            return False
        # 按值比较：load() 每次都会构造新 dict，不能比较对象标识
        return self._snapshot != before

    def maybe_reload(self) -> bool:
        """mtime-gated :meth:`reload`, safe to call on every scheduler tick."""
        now = time.time()
        if now - self._checked_at < self.poll_sec:
            return False
        self._checked_at = now
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if self._mtime is not None and mtime == self._mtime:
            return False
        return self.reload()

    # ---------------------------------------------------------------- access

    def on_reload(self, fn: Callable[[dict, dict], None]) -> None:
        self._listeners.append(fn)

    def on_error(self, fn: Callable[[Exception], None]) -> None:
        self._error_listeners.append(fn)

    @property
    def snapshot(self) -> dict:
        return self._snapshot

    def rooms(self) -> list[int]:
        raw = self._snapshot.get("rooms") or []
        rooms: list[int] = []
        for item in raw:
            try:
                rooms.append(int(item))
            except (TypeError, ValueError):
                continue
        return rooms

    def engine_config(self) -> EngineConfig:
        return EngineConfig(**_pick(self._snapshot.get("engine") or {}, EngineConfig))

    def alert_config(self) -> AlertConfig:
        return AlertConfig(**_pick(self._snapshot.get("alert") or {}, AlertConfig))

    def storage_config(self) -> StorageConfig:
        return StorageConfig(**_pick(self._snapshot.get("storage") or {}, StorageConfig))

    def ai_settings(self) -> dict:
        value = self._snapshot.get("ai") or {}
        return dict(value) if isinstance(value, dict) else {}
