"""Hot-reloadable protobuf field schema — platform-neutral.

Bilibili keeps moving live commands from plain JSON to a Base64 protobuf blob in
``data.pb`` (``INTERACT_WORD_V2``, ``SEND_GIFT_V2``, ``ONLINE_RANK_V3``, …) and
the private schema is *not* public API. Field numbers therefore drift from time
to time. Hard-coding them in ``parser.py`` means every drift becomes a code
change and a release.

This module keeps every field number **outside the code**:

* the shipped defaults live in ``livecore/schemas/<platform>.pb.json``;
* a user overlay (env var or :meth:`SchemaStore.apply_overlay`) can patch single
  fields without touching the package;
* both files are polled by mtime, so an edit is picked up by a *running*
  connection — no restart, no reconnect, no release.

The transport and every other platform are untouched: this is only consulted by
a platform's own decoder.

Schema document shape::

    {
      "version": 1,
      "platform": "bilibili",
      "commands": {
        "INTERACT_WORD_V2": {
          "source": "pb",
          "fields": {"uid": 1, "uname": 2, "medal_name": "9.3"}
        }
      }
    }

A field value is either a field number (``9``) or a dotted path into nested
messages (``"9.3"`` → ``message(9).message/text(3)``).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .pb import PbMessage

__all__ = [
    "CommandSchema",
    "FieldPath",
    "FieldReader",
    "SchemaError",
    "SchemaStore",
    "default_schema_path",
    "get_schema_store",
    "schema_for",
    "set_schema_store",
    "store_from_settings",
]

SOURCE_PB = "pb"
SOURCE_JSON = "json"
SCHEMA_VERSION = 1

_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")

#: Env vars checked (in order) for an overlay document path.
_OVERLAY_ENV = ("LIVECORE_SCHEMA_PATH", "LIVECORE_SCHEMA_{platform}")


class SchemaError(RuntimeError):
    """Raised when a schema document is structurally unusable."""


# --------------------------------------------------------------------------- paths


@dataclass(frozen=True, slots=True, order=True)
class FieldPath:
    """One location inside a protobuf message, as a chain of field numbers."""

    segments: tuple[int, ...]

    @property
    def key(self) -> str:
        return ".".join(str(segment) for segment in self.segments)

    @property
    def depth(self) -> int:
        return len(self.segments)

    @classmethod
    def parse(cls, value: Any, *, where: str = "") -> FieldPath:
        where = f" ({where})" if where else ""
        if isinstance(value, bool) or value is None:
            raise SchemaError(f"field path must be an int or 'a.b' string{where}")
        if isinstance(value, int):
            segments = (value,)
        elif isinstance(value, str):
            parts = [part.strip() for part in value.split(".") if part.strip()]
            if not parts:
                raise SchemaError(f"empty field path{where}")
            try:
                segments = tuple(int(part) for part in parts)
            except ValueError as exc:
                raise SchemaError(f"non-numeric field path {value!r}{where}") from exc
        elif isinstance(value, (list, tuple)):
            segments = tuple(int(part) for part in value)
        else:
            raise SchemaError(f"unsupported field path {value!r}{where}")
        if not segments or any(segment <= 0 for segment in segments):
            raise SchemaError(f"field numbers must be positive{where}: {value!r}")
        return cls(segments)


@dataclass(frozen=True, slots=True)
class CommandSchema:
    """Field map for a single live command.

    ``fields`` maps a logical name to one location. ``lists`` maps the logical
    name of a *repeated nested message* to the field map of one element, so a
    rank list can be described without inventing a second command.
    """

    cmd: str
    source: str = SOURCE_JSON
    fields: Mapping[str, FieldPath] = field(default_factory=dict)
    lists: Mapping[str, Mapping[str, FieldPath]] = field(default_factory=dict)
    ignore: frozenset[int] = frozenset()
    note: str = ""

    @property
    def is_protobuf(self) -> bool:
        return self.source == SOURCE_PB

    def path(self, name: str) -> FieldPath | None:
        return self.fields.get(name)

    def item_fields(self, name: str) -> Mapping[str, FieldPath]:
        return self.lists.get(name, {})

    def number(self, name: str) -> int | None:
        """First-hop field number, for callers that only need a top-level int."""
        path = self.fields.get(name)
        return path.segments[0] if path else None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.fields))

    def mapped_numbers(self) -> frozenset[int]:
        return frozenset(path.segments[0] for path in self.fields.values())


# --------------------------------------------------------------------------- reading


class FieldReader:
    """Reads logical fields from a protobuf blob using a :class:`CommandSchema`.

    Every read is total: an unknown logical name, a missing field, a truncated
    blob or a value of the wrong wire type all yield the supplied default. The
    decoder can therefore never be broken by a schema drift — it just reports
    fewer fields.
    """

    __slots__ = ("_blob", "_schema")

    def __init__(self, blob: PbMessage | None, schema: CommandSchema | None) -> None:
        self._blob = blob if blob is not None else PbMessage(b"")
        self._schema = schema

    @property
    def schema(self) -> CommandSchema | None:
        return self._schema

    @property
    def known(self) -> bool:
        """True when a schema was found for the command."""
        return self._schema is not None

    @property
    def blob(self) -> PbMessage:
        return self._blob

    def path(self, name: str) -> str:
        """Resolved dotted path, or ``""`` when the name is not mapped."""
        path = self._schema.path(name) if self._schema else None
        return path.key if path else ""

    def numbers(self) -> list[int]:
        """Top-level field numbers actually present in the blob (for drift checks)."""
        return sorted(self._blob.fields)

    def has(self, name: str) -> bool:
        return self._entry(name) is not None

    # ------------------------------------------------------------- internals

    def _resolve(self, name: str) -> FieldPath | None:
        if self._schema is None:
            return None
        return self._schema.path(name)

    def _parent(self, path: FieldPath) -> PbMessage | None:
        node = self._blob
        for segment in path.segments[:-1]:
            node = node.message(segment)
            if not node:
                return None
        return node

    def _entry(self, name: str):
        path = self._resolve(name)
        if path is None:
            return None
        parent = self._parent(path)
        if parent is None:
            return None
        return parent.first(path.segments[-1])

    # ---------------------------------------------------------------- reads

    def int(self, name: str, default: int = 0) -> int:
        entry = self._entry(name)
        return entry.as_int() if entry is not None else default

    def text(self, name: str, default: str = "") -> str:
        entry = self._entry(name)
        if entry is None:
            return default
        return entry.as_text() or default

    def raw(self, name: str) -> bytes:
        entry = self._entry(name)
        return entry.raw_bytes if entry is not None else b""

    def ints(self, name: str) -> list[int]:
        """Repeated/packed integer values (e.g. ``identities``)."""
        path = self._resolve(name)
        if path is None:
            return []
        parent = self._parent(path)
        if parent is None:
            return []
        return parent.integers(path.segments[-1])

    def message(self, name: str) -> PbMessage:
        """Nested message at ``name`` (empty message when absent)."""
        path = self._resolve(name)
        if path is None:
            return PbMessage(b"")
        node = self._blob
        for segment in path.segments:
            node = node.message(segment)
            if not node:
                return PbMessage(b"")
        return node

    def messages(self, name: str) -> list[PbMessage]:
        """Repeated nested messages at ``name`` (e.g. a rank list)."""
        path = self._resolve(name)
        if path is None:
            return []
        parent = self._parent(path)
        if parent is None:
            return []
        return [
            PbMessage(entry.raw_bytes)
            for entry in parent.values(path.segments[-1])
            if entry.raw_bytes
        ]

    def items(self, name: str) -> list["FieldReader"]:
        """One :class:`FieldReader` per element of a repeated message field.

        Uses the command schema's ``lists`` map, so callers get named access to
        each row (``uid``/``uname``/``rank``…) instead of raw field numbers.
        """
        item_fields = self._schema.item_fields(name) if self._schema else {}
        if not item_fields:
            return []
        element_schema = CommandSchema(
            cmd=f"{self._schema.cmd}.{name}" if self._schema else name,
            source=SOURCE_PB,
            fields=item_fields,
        )
        return [FieldReader(blob, element_schema) for blob in self.messages(name)]

    def unmapped(self, extra_ignore: Iterable[int] = ()) -> list[int]:
        """Top-level field numbers present in the blob but not explained by the schema.

        This is the drift signal: Bilibili added a field we do not map yet.
        Numbers listed in the command's ``ignore`` list (empty placeholders and
        fields we deliberately do not consume) are not reported.
        """
        if self._schema is None:
            return self.numbers()
        skip = self._schema.mapped_numbers() | self._schema.ignore | frozenset(extra_ignore)
        return [number for number in self.numbers() if number not in skip]


# --------------------------------------------------------------------------- store


def default_schema_path(platform: str) -> str:
    """Path of the schema document shipped inside the package."""
    return os.path.join(_SCHEMA_DIR, f"{platform}.pb.json")


def store_from_settings(platform: str = "bilibili", settings: Mapping[str, Any] | None = None) -> SchemaStore:
    """Build a store from a config section.

    Recognised keys: ``schema_path`` (replace the document), ``overlay_path``
    (merge a second document on top), ``poll_sec`` and ``commands`` (an inline
    per-command overlay merged last, highest priority).
    """
    settings = dict(settings or {})
    schema_path = str(settings["schema_path"]) if settings.get("schema_path") else None
    overlay_path = str(settings["overlay_path"]) if settings.get("overlay_path") else None
    poll_sec = settings.get("poll_sec")
    store = SchemaStore(
        platform,
        path=schema_path,
        overlay_path=overlay_path,
        poll_sec=float(poll_sec) if poll_sec else 2.0,
    )
    commands = settings.get("commands")
    if isinstance(commands, dict) and commands:
        store.apply_overlay({"commands": commands})
    return store


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"schema is not valid JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaError(f"schema root must be an object: {path}")
    return raw


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_document(document: Mapping[str, Any], *, where: str) -> dict[str, CommandSchema]:
    commands = document.get("commands")
    if not isinstance(commands, dict):
        raise SchemaError(f"schema has no 'commands' object{where}")
    parsed: dict[str, CommandSchema] = {}
    for cmd, body in commands.items():
        if not isinstance(body, dict):
            raise SchemaError(f"command {cmd!r} must be an object{where}")
        raw_fields = body.get("fields")
        if not isinstance(raw_fields, dict):
            raise SchemaError(f"command {cmd!r} has no 'fields' object{where}")
        source = str(body.get("source") or SOURCE_JSON).lower()
        if source not in (SOURCE_PB, SOURCE_JSON):
            raise SchemaError(f"command {cmd!r} has unknown source {source!r}{where}")
        fields: dict[str, FieldPath] = {}
        for name, value in raw_fields.items():
            fields[str(name)] = FieldPath.parse(value, where=f"{cmd}.{name}{where}")
        lists: dict[str, dict[str, FieldPath]] = {}
        raw_lists = body.get("lists")
        if raw_lists is not None:
            if not isinstance(raw_lists, dict):
                raise SchemaError(f"command {cmd!r} 'lists' must be an object{where}")
            for list_name, raw_item in raw_lists.items():
                if not isinstance(raw_item, dict):
                    raise SchemaError(f"command {cmd!r} list {list_name!r} must be an object{where}")
                if str(list_name) not in fields:
                    raise SchemaError(
                        f"command {cmd!r} list {list_name!r} has no matching entry in 'fields'{where}"
                    )
                lists[str(list_name)] = {
                    str(field): FieldPath.parse(value, where=f"{cmd}.{list_name}.{field}{where}")
                    for field, value in raw_item.items()
                }
        parsed[str(cmd)] = CommandSchema(
            cmd=str(cmd),
            source=source,
            fields=fields,
            lists=lists,
            ignore=_parse_ignore(body.get("ignore"), cmd=cmd, source=source, where=where),
            note=str(body.get("note") or ""),
        )
    return parsed


def _parse_ignore(raw: Any, *, cmd: str, source: str, where: str) -> frozenset[int]:
    """``ignore``: field numbers deliberately left out of the schema."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, (list, tuple)):
        raise SchemaError(f"command {cmd!r} 'ignore' must be a list{where}")
    numbers: set[int] = set()
    for value in raw:
        numbers.add(FieldPath.parse(value, where=f"{cmd}.ignore{where}").segments[0])
    return frozenset(numbers)


class SchemaStore:
    """Loads, validates and hot-reloads one platform's protobuf field schema."""

    def __init__(
        self,
        platform: str = "bilibili",
        *,
        path: str | None = None,
        overlay_path: str | None = None,
        poll_sec: float = 2.0,
        autoload: bool = True,
    ) -> None:
        self.platform = platform
        self.path = str(path) if path else default_schema_path(platform)
        self.poll_sec = poll_sec
        self._overlay_path = overlay_path or self._env_overlay_path()
        self._commands: dict[str, CommandSchema] = {}
        self._document: dict[str, Any] = {}
        self._inline_overlay: dict[str, Any] = {}
        self._mtimes: dict[str, float | None] = {}
        self._checked_at = time.time()
        self._listeners: list[Callable[[dict[str, CommandSchema], dict[str, CommandSchema]], None]] = []
        self._error_listeners: list[Callable[[Exception], None]] = []
        self.last_error: Exception | None = None
        if autoload:
            try:
                self.load()
            except (SchemaError, OSError, ValueError) as exc:
                # A broken or missing document must never take the connection
                # down: the decoder degrades to "no mapped fields" and reports
                # the problem through the error listeners instead.
                self.last_error = exc

    # ----------------------------------------------------------- environment

    def _env_overlay_path(self) -> str | None:
        for template in _OVERLAY_ENV:
            name = template.format(platform=self.platform.upper())
            value = os.environ.get(name)
            if value:
                return value
        return None

    # -------------------------------------------------------------- lifecycle

    def load(self) -> dict[str, CommandSchema]:
        """Re-read the document. Raises :class:`SchemaError` on unusable input."""
        document: dict[str, Any] = {}
        if os.path.isfile(self.path):
            document = _read_json(self.path)
            self._remember_mtime(self.path)
        if self._overlay_path and os.path.isfile(self._overlay_path):
            document = _deep_merge(document, _read_json(self._overlay_path))
            self._remember_mtime(self._overlay_path)
        if self._inline_overlay:
            document = _deep_merge(document, self._inline_overlay)
        if not document:
            raise SchemaError(f"no schema document found at {self.path}")

        parsed = _parse_document(document, where=f" [{self.path}]")
        old = self._commands
        self._commands = parsed
        self._document = document
        self.last_error = None
        if old != parsed:
            for listener in self._listeners:
                listener(old, parsed)
        return parsed

    def _remember_mtime(self, path: str) -> None:
        try:
            self._mtimes[path] = os.path.getmtime(path)
        except OSError:
            self._mtimes[path] = None

    def reload(self) -> bool:
        """Force a reload; returns True when the parsed schema changed."""
        before = self._commands
        try:
            self.load()
        except (SchemaError, OSError, ValueError) as exc:
            self.last_error = exc
            for listener in self._error_listeners:
                listener(exc)
            return False
        return self._commands != before

    def _watched(self) -> list[str]:
        paths = [self.path]
        if self._overlay_path:
            paths.append(self._overlay_path)
        return paths

    def maybe_reload(self) -> bool:
        """mtime-gated :meth:`reload`; cheap enough to call per inbound event."""
        now = time.time()
        if now - self._checked_at < self.poll_sec:
            return False
        self._checked_at = now
        changed = False
        for path in self._watched():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if self._mtimes.get(path) != mtime:
                changed = True
                break
        if not changed:
            return False
        return self.reload()

    # ---------------------------------------------------------------- overlay

    def apply_overlay(self, overlay: Mapping[str, Any] | None) -> bool:
        """Merge an in-memory overlay (e.g. the ``protobuf`` config section).

        Returns True when the effective schema changed.
        """
        incoming = dict(overlay or {})
        if incoming == self._inline_overlay:
            return False
        self._inline_overlay = incoming
        try:
            return self.reload()
        except (SchemaError, OSError, ValueError):  # pragma: no cover - reload swallows
            return False

    # -------------------------------------------------------------- observers

    def on_reload(self, fn: Callable[[dict[str, CommandSchema], dict[str, CommandSchema]], None]) -> None:
        self._listeners.append(fn)

    def on_error(self, fn: Callable[[Exception], None]) -> None:
        self._error_listeners.append(fn)

    # ----------------------------------------------------------------- access

    @property
    def document(self) -> dict[str, Any]:
        return self._document

    @property
    def commands(self) -> dict[str, CommandSchema]:
        return self._commands

    def command(self, cmd: str) -> CommandSchema | None:
        return self._commands.get(cmd)

    def protobuf_commands(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, cmd in self._commands.items() if cmd.is_protobuf))

    def knows(self, cmd: str) -> bool:
        return cmd in self._commands

    def reader(self, cmd: str, blob: PbMessage | None) -> FieldReader:
        return FieldReader(blob, self.command(cmd))

    def describe(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "path": self.path,
            "overlay_path": self._overlay_path or "",
            "version": self._document.get("version"),
            "commands": len(self._commands),
            "protobuf_commands": list(self.protobuf_commands()),
            "poll_sec": self.poll_sec,
            "last_error": str(self.last_error) if self.last_error else "",
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<SchemaStore platform={self.platform!r} commands={len(self._commands)}>"


# --------------------------------------------------------------------------- global


_DEFAULT_STORE: SchemaStore | None = None


def schema_for(platform: str = "bilibili") -> SchemaStore:
    """Process-wide default store for ``platform`` (created on first use)."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None or _DEFAULT_STORE.platform != platform:
        _DEFAULT_STORE = SchemaStore(platform)
    return _DEFAULT_STORE


def get_schema_store() -> SchemaStore | None:
    return _DEFAULT_STORE


def set_schema_store(store: SchemaStore | None) -> None:
    """Install a shared store (used by tests and by the supervisor)."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store
