"""Tests for livecore.adapters."""

from __future__ import annotations

import pytest

from livecore.adapters import (
    AiAdapter,
    NoopAiAdapter,
    OutboundAdapter,
    SimulatorAdapter,
)
from livecore.types import Suggestion


def test_simulator_records_suggestions():
    s = SimulatorAdapter()
    sug = Suggestion(id="x", ts=0.0, text="hi", reason="r", source="rule")
    import asyncio
    asyncio.run(s.publish(sug))
    assert s.sent == [sug]


def test_noop_ai_raises():
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(NoopAiAdapter().complete("p", "t", "x", "neutral", 24))


def test_protocol_classes_exist():
    # typing/runtime check: classes satisfy the Protocol
    assert isinstance(SimulatorAdapter(), OutboundAdapter)
    assert isinstance(NoopAiAdapter(), AiAdapter)
