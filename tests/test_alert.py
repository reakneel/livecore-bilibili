"""Tests for livecore.alert — Phase 5.1 failure alerting."""

from __future__ import annotations

import pytest

from livecore.alert import Alert, Alerter, EmailSink, LogSink, ReconnectWatch, WebhookSink
from livecore.config import AlertConfig
from livecore.logger import RingLogger


class RecordingSink:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, alert) -> None:
        self.sent.append(alert)


@pytest.fixture()
def log():
    return RingLogger()


# ---------------------------------------------------------------- sinks


@pytest.mark.asyncio
async def test_log_sink_writes_to_ring_logger(log):
    sink = LogSink(log)
    await sink.send(Alert(key="k", level="warn", title="标题", detail="细节", ts=0))
    assert any(e.message == "[k] 标题 — 细节" for e in log.snapshot())


@pytest.mark.asyncio
async def test_webhook_sink_inert_without_url():
    sink = WebhookSink()
    assert sink.enabled is False
    await sink.send(None)  # 不应抛异常
    assert sink.failures == 0


@pytest.mark.asyncio
async def test_webhook_sink_counts_failure_when_httpx_missing(monkeypatch):
    import builtins

    sink = WebhookSink(webhook_url="https://example.invalid/hook")
    assert sink.enabled is True
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("no httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    await sink.send(None)
    assert sink.failures == 1


@pytest.mark.asyncio
async def test_email_sink_is_a_stub():
    sink = EmailSink()
    await sink.send(None)
    assert sink.outbox == []
    sink2 = EmailSink(recipient="a@b.c")
    assert sink2.enabled is True
    await sink2.send("alert")
    assert sink2.outbox == ["alert"]


# ---------------------------------------------------------------- alerter


@pytest.mark.asyncio
async def test_alerter_disabled_by_default(log):
    alerter = Alerter(AlertConfig(enabled=False), log=log)
    assert await alerter.raise_alert("k", "标题") is None
    assert alerter.history == []


@pytest.mark.asyncio
async def test_alerter_fans_out_to_all_sinks():
    a, b = RecordingSink(), RecordingSink()
    alerter = Alerter(AlertConfig(enabled=True), sinks=[a, b])
    alert = await alerter.raise_alert("k", "标题", "细节")
    assert alert is not None
    assert len(a.sent) == 1 and len(b.sent) == 1
    assert alert.title == "标题"


@pytest.mark.asyncio
async def test_alerter_dedupes_within_cooldown():
    sink = RecordingSink()
    alerter = Alerter(AlertConfig(enabled=True, cooldown_sec=300), sinks=[sink])
    await alerter.raise_alert("reconnect", "第一次")
    second = await alerter.raise_alert("reconnect", "第二次")
    assert second is None
    assert len(sink.sent) == 1


@pytest.mark.asyncio
async def test_alerter_separate_keys_not_deduped():
    sink = RecordingSink()
    alerter = Alerter(AlertConfig(enabled=True, cooldown_sec=300), sinks=[sink])
    await alerter.raise_alert("reconnect", "a")
    await alerter.raise_alert("quota", "b")
    assert len(sink.sent) == 2


@pytest.mark.asyncio
async def test_alerter_cooldown_expires():
    sink = RecordingSink()
    alerter = Alerter(AlertConfig(enabled=True, cooldown_sec=0), sinks=[sink])
    await alerter.raise_alert("k", "a")
    await alerter.raise_alert("k", "b")
    assert len(sink.sent) == 2


def test_alerter_prepends_log_sink_when_log_given(log):
    alerter = Alerter(AlertConfig(), log=log)
    assert any(isinstance(s, LogSink) for s in alerter.sinks)


def test_add_sink_appends():
    alerter = Alerter(AlertConfig())
    sink = RecordingSink()
    alerter.add_sink(sink)
    assert sink in alerter.sinks


# ---------------------------------------------------------------- reconnect watch


@pytest.mark.asyncio
async def test_reconnect_watch_silent_below_threshold():
    alerter = Alerter(AlertConfig(enabled=True), sinks=[RecordingSink()])
    watch = ReconnectWatch(alerter, threshold=3)
    assert await watch.record_failure() is None
    assert await watch.record_failure() is None
    assert watch.total == 2
    assert watch.tripped is False


@pytest.mark.asyncio
async def test_reconnect_watch_fires_at_threshold():
    alerter = Alerter(AlertConfig(enabled=True), sinks=[RecordingSink()])
    watch = ReconnectWatch(alerter, threshold=3)
    await watch.record_failure()
    await watch.record_failure()
    alert = await watch.record_failure("房间 1 断线")
    assert alert is not None
    assert watch.tripped is True
    assert "连续失败 3 次" in alert.title
    assert alert.level == "error"


@pytest.mark.asyncio
async def test_reconnect_watch_resets_on_success():
    alerter = Alerter(AlertConfig(enabled=True, cooldown_sec=0), sinks=[RecordingSink()])
    watch = ReconnectWatch(alerter, threshold=2)
    await watch.record_failure()
    watch.record_success()
    assert watch.consecutive == 0
    assert await watch.record_failure() is None
    assert await watch.record_failure() is not None
    assert watch.total == 3  # 累计失败数不因恢复而清零
