"""Phase 5.1 — failure alerting.

Two cooperating pieces:

* :class:`Alerter` fans an alert out to one or more sinks and de-duplicates
  identical alerts within a cooldown window, so a flapping connection produces
  one notification instead of one per retry.
* :class:`ReconnectWatch` counts consecutive reconnect failures and raises an
  alert once the threshold is crossed (recovering resets the counter).

**Every shipped sink is inert by default.** ``webhook`` only fires when a URL is
explicitly configured, and ``email`` only records intent — no SMTP credentials
are ever read from this repository. The point of the module is the hook, not a
bundled notifier.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .config import AlertConfig

__all__ = ["Alert", "AlertSink", "LogSink", "WebhookSink", "EmailSink", "Alerter", "ReconnectWatch"]

AlertLevel = Literal["warn", "error"]


@dataclass(slots=True)
class Alert:
    key: str
    level: AlertLevel
    title: str
    detail: str
    ts: float


@runtime_checkable
class AlertSink(Protocol):
    async def send(self, alert: Alert) -> None: ...


class LogSink:
    """Appends alerts to a :class:`~livecore.logger.RingLogger` (always safe)."""

    def __init__(self, log) -> None:
        self.log = log
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)
        self.log.push(alert.level, "infra", f"[{alert.key}] {alert.title} — {alert.detail}")


class WebhookSink:
    """POSTs a JSON body to a configurable URL (DingTalk / Feishu / Slack style).

    Does nothing unless ``webhook_url`` is set. Network errors are swallowed and
    counted so a broken notifier can never take the session down.
    """

    def __init__(self, webhook_url: str = "", timeout: float = 5.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.failures = 0
        self.sent: list[Alert] = []

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, alert: Alert) -> None:
        if not self.enabled:
            return
        try:
            import httpx  # 可选依赖，仅在真正需要时才导入
        except ImportError:
            self.failures += 1
            return
        payload = {
            "msgtype": "text",
            "text": {"content": f"[livecore][{alert.level}] {alert.title}\n{alert.detail}"},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(self.webhook_url, json=payload)
            self.sent.append(alert)
        except Exception:
            self.failures += 1


class EmailSink:
    """Placeholder sink — records what *would* be mailed.

    Wiring real SMTP means putting credentials somewhere; that belongs in the
    host application, not in this library. Inject a custom sink instead.
    """

    def __init__(self, recipient: str = "") -> None:
        self.recipient = recipient
        self.outbox: list[Alert] = []

    @property
    def enabled(self) -> bool:
        return bool(self.recipient)

    async def send(self, alert: Alert) -> None:
        if not self.enabled:
            return
        self.outbox.append(alert)


class Alerter:
    """De-duplicating fan-out across sinks."""

    def __init__(self, config: AlertConfig | None = None, sinks: list[AlertSink] | None = None, log=None) -> None:
        self.config = config or AlertConfig()
        self.sinks: list[AlertSink] = sinks if sinks is not None else []
        if log is not None:
            self.sinks.insert(0, LogSink(log))
        self._last_sent: dict[str, float] = {}
        self.history: list[Alert] = []

    def add_sink(self, sink: AlertSink) -> None:
        self.sinks.append(sink)

    def _in_cooldown(self, key: str, now: float) -> bool:
        last = self._last_sent.get(key)
        return last is not None and now - last < self.config.cooldown_sec

    async def raise_alert(
        self, key: str, title: str, detail: str = "", level: AlertLevel = "warn"
    ) -> Alert | None:
        """Emit an alert unless disabled or still in cooldown. Returns the alert."""
        if not self.config.enabled:
            return None
        now = time.time()
        if self._in_cooldown(key, now):
            return None
        alert = Alert(key=key, level=level, title=title, detail=detail, ts=now)
        self._last_sent[key] = now
        self.history.append(alert)
        for sink in self.sinks:
            await sink.send(alert)
        return alert


class ReconnectWatch:
    """Consecutive-failure counter that trips :class:`Alerter` at the threshold."""

    def __init__(self, alerter: Alerter, threshold: int = 3) -> None:
        self.alerter = alerter
        self.threshold = threshold
        self.consecutive = 0
        self.total = 0

    @property
    def tripped(self) -> bool:
        return self.consecutive >= self.threshold

    def record_success(self) -> None:
        self.consecutive = 0

    async def record_failure(self, detail: str = "") -> Alert | None:
        self.consecutive += 1
        self.total += 1
        if self.consecutive < self.threshold:
            return None
        return await self.alerter.raise_alert(
            key="reconnect",
            title=f"重连连续失败 {self.consecutive} 次",
            detail=detail or "检查网络、token 是否过期，或直播间是否已下播",
            level="error",
        )
