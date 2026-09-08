"""Production-style Bilibili live long-connection monitor.

Run forever until Ctrl+C/SIGTERM:
    python examples/run_long_connection.py 1814378608

Run a bounded soak test:
    python examples/run_long_connection.py 1814378608 --duration 1800

The human output is intentionally shaped like the LiveCore frontend stream:
[event type] user | gift | amount | meta summary

``--json`` emits the same normalized monitor projection as one JSON object per
line, making stdout directly consumable by log collectors or other tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from collections import Counter
from datetime import datetime
from typing import Any

from livecore import ConnectionSupervisor
from livecore.logger import RingLogger
from livecore.monitor import monitor_event
from livecore.types import LiveEvent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LiveCore Bilibili long connection monitor")
    parser.add_argument("room_id", type=int, help="Bilibili live room ID")
    parser.add_argument("--duration", type=float, default=None, help="Run seconds; default: forever")
    parser.add_argument("--json", action="store_true", help="Emit normalized monitor events as JSONL")
    parser.add_argument("--raw", action="store_true", help="Append the raw Bilibili command")
    return parser.parse_args()


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _user_text(payload: dict[str, Any]) -> str:
    user = payload.get("user") or {}
    if not user:
        return "-"
    extras = []
    if user.get("guard"):
        extras.append(f"guard={user['guard']}")
    if user.get("medal"):
        extras.append(f"medal={user['medal']}")
    return f"{user.get('name', '匿名')}" + (f" ({', '.join(extras)})" if extras else "")


def format_monitor_event(event: LiveEvent, *, raw: bool = False) -> str:
    payload = monitor_event(event)
    user = _user_text(payload)
    gift = payload.get("gift") or {}
    amount = payload.get("amount") or {}
    gift_text = "-" if not gift else f"{gift.get('name', '礼物')} x{gift.get('num', 1)}"
    amount_text = "-" if not amount else f"{amount.get('value')} {amount.get('currency')}"
    meta = payload.get("meta_summary") or "-"
    line = (
        f"[{timestamp()}] [{payload['kind_label']}/{payload['kind']}] "
        f"user={user} | gift={gift_text} | amount={amount_text} | meta={meta}"
    )
    if payload.get("text") and payload["kind"] == "danmaku":
        line += f" | text={payload['text']}"
    if raw and payload.get("raw_cmd"):
        line += f" | raw_cmd={payload['raw_cmd']}"
    return line


def on_log(entry: Any) -> None:
    level = getattr(entry, "level", "INFO")
    layer = getattr(entry, "layer", "livecore")
    message = getattr(entry, "message", str(entry))
    print(f"[{timestamp()}] [LOG/{level}/{layer}] {message}")


async def main() -> None:
    args = parse_args()
    if args.room_id <= 0:
        raise SystemExit("room_id must be positive")
    if args.duration is not None and args.duration <= 0:
        raise SystemExit("--duration must be greater than 0")

    log = RingLogger(maxlen=200)
    supervisor = ConnectionSupervisor(args.room_id, log)
    stop_event = asyncio.Event()
    counts: Counter[str] = Counter()
    installed_signals: list[signal.Signals] = []

    def on_state(state: str) -> None:
        print(f"[{timestamp()}] [STATE] {state}")

    def on_event(event: LiveEvent) -> None:
        counts[event.kind] += 1
        if args.json:
            print(json.dumps(monitor_event(event), ensure_ascii=False, default=str), flush=True)
        else:
            print(format_monitor_event(event, raw=args.raw), flush=True)

    log.on(on_log)
    supervisor.on_state(on_state)
    supervisor.on_event(on_event)

    def request_stop() -> None:
        if not stop_event.is_set():
            print(f"\n[{timestamp()}] shutdown requested", flush=True)
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
            installed_signals.append(sig)
        except (NotImplementedError, AttributeError):
            pass

    try:
        print("=" * 96)
        print("LiveCore Bilibili Long Connection Monitor")
        print("=" * 96)
        print(f"room_id={args.room_id}")
        print(f"duration={'unlimited' if args.duration is None else f'{args.duration}s'}")
        print(f"output={'jsonl' if args.json else 'monitor'}")
        print("Ctrl+C to stop")
        print()

        await supervisor.start()
        print(f"[{timestamp()}] [READY] supervisor started; waiting for events", flush=True)

        if args.duration is None:
            await stop_event.wait()
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
            except TimeoutError:
                print(f"[{timestamp()}] [TIMEOUT] duration reached")

    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] KeyboardInterrupt received")
    finally:
        print(f"[{timestamp()}] [SHUTDOWN] stopping supervisor...")
        try:
            await supervisor.stop()
        except Exception as exc:
            print(f"[{timestamp()}] [SHUTDOWN ERROR] {type(exc).__name__}: {exc}")

        for sig in installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, AttributeError):
                pass

        health = supervisor.snapshot()
        print()
        print("=" * 96)
        print("Session summary")
        print("=" * 96)
        print(f"room_id={health.room_id}")
        print(f"final_state={health.state}")
        print(f"reconnects={health.reconnects}")
        print(f"last_error={health.last_error or '<none>'}")
        print(f"events_total={sum(counts.values())}")
        print("event_counts:")
        for kind, count in sorted(counts.items()):
            print(f"  {kind:<12} {count}")
        print("=" * 96)
        print("LiveCore exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
