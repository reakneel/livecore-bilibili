"""Outbound and AI adapters. Default outbound is a simulator — it never posts to Bilibili."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import Suggestion


@runtime_checkable
class OutboundAdapter(Protocol):
    async def publish(self, suggestion: Suggestion) -> None: ...


class SimulatorAdapter:
    """Records accepted suggestions locally. Does not call Bilibili send APIs."""

    def __init__(self) -> None:
        self.sent: list[Suggestion] = []

    async def publish(self, suggestion: Suggestion) -> None:
        self.sent.append(suggestion)


@runtime_checkable
class AiAdapter(Protocol):
    async def complete(self, persona: str, transcript: str, target: str, sentiment: str, max_len: int) -> str: ...


class NoopAiAdapter:
    async def complete(self, persona: str, transcript: str, target: str, sentiment: str, max_len: int) -> str:
        raise RuntimeError("未配置 AI 适配器，请使用规则引擎或自行注入 AiAdapter")
