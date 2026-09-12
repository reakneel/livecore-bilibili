"""Production-style live long-connection monitor.

Run forever until Ctrl+C/SIGTERM:
    python examples/run_long_connection.py 1814378608

Run a bounded soak test:
    python examples/run_long_connection.py 1814378608 --duration 1800

Pick a platform adapter explicitly:
    python examples/run_long_connection.py 1814378608 --platform bilibili

The human output is intentionally shaped like the LiveCore frontend stream:
[event type] user | gift | amount | meta summary

``--json`` emits the same normalized monitor projection as one JSON object per
line, making stdout directly consumable by log collectors or other tools.

Connection lifecycle is delegated to ``ConnectionSupervisor``, which now drives
a platform-neutral ``LiveConnection`` through whatever adapter is selected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections import Counter
from datetime import datetime
from typing import Any

from livecore import ConnectionSupervisor
from livecore.logger import RingLogger
from livecore.monitor import monitor_event
from livecore.platforms import BilibiliAdapter, available_platforms, get_adapter
from livecore.schema import store_from_settings
from livecore.types import LiveEvent

#: Kinds whose ``text`` carries information beyond the user/gift columns.
TEXT_KINDS = {"danmaku", "superchat", "enter", "follow", "share"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LiveCore live long connection monitor")
    parser.add_argument("room_id", type=int, help="Live room ID (short IDs resolve to the real room)")
    parser.add_argument("--platform", default="bilibili", choices=available_platforms(),
                        help="Platform adapter to use")
    parser.add_argument("--duration", type=float, default=None, help="Run seconds; default: forever")
    parser.add_argument("--json", action="store_true", help="Emit normalized monitor events as JSONL")
    parser.add_argument("--raw", action="store_true", help="Append the raw platform command")
    parser.add_argument("--buvid", default="", help="Optional bilibili buvid3 (lowers risk-control odds)")
    parser.add_argument("--schema", default="", help="Override the protobuf schema document path")
    parser.add_argument("--schema-overlay", default="", help="Overlay protobuf schema document path (hot-reloaded)")
    parser.add_argument("--quiet-log", action="store_true", help="Suppress connection logs")
    return parser.parse_args()


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def make_console(json_mode: bool):
    """In ``--json`` mode stdout stays pure JSONL; everything human goes to stderr."""
    stream = sys.stderr if json_mode else sys.stdout

    def info(text: str = "") -> None:
        print(text, file=stream, flush=True)

    return info


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
    if payload.get("text") and payload["kind"] in TEXT_KINDS:
        line += f" | text={payload['text']}"
    if raw and payload.get("raw_cmd"):
        line += f" | raw_cmd={payload['raw_cmd']}"
    return line


def on_log(entry: Any) -> None:
    level = getattr(entry, "level", "INFO")
    layer = getattr(entry, "layer", "livecore")
    message = getattr(entry, "message", str(entry))
    print(f"[{timestamp()}] [LOG/{level}/{layer}] {message}", file=sys.stderr)


def build_adapter(args: argparse.Namespace) -> Any:
    """Resolve the adapter; the Bilibili path honours --buvid and the schema flags."""
    if args.platform != "bilibili":
        return get_adapter(args.platform)
    settings: dict[str, Any] = {}
    if args.schema:
        settings["schema_path"] = args.schema
    if args.schema_overlay:
        settings["overlay_path"] = args.schema_overlay
    return BilibiliAdapter(buvid=args.buvid, schema=store_from_settings("bilibili", settings))


async def main() -> None:
    args = parse_args()
    if args.room_id <= 0:
        raise SystemExit("room_id must be positive")
    if args.duration is not None and args.duration <= 0:
        raise SystemExit("--duration must be greater than 0")

    adapter = build_adapter(args)
    log = RingLogger(maxlen=200)
    supervisor = ConnectionSupervisor(args.room_id, log, adapter=adapter)
    stop_event = asyncio.Event()
    counts: Counter[str] = Counter()
    installed_signals: list[signal.Signals] = []
    info = make_console(args.json)

    def on_state(state: str) -> None:
        if args.json:
            print(json.dumps({"type": "state", "state": state}, ensure_ascii=False), file=sys.stderr, flush=True)
        else:
            info(f"[{timestamp()}] [STATE] {state}")

    def on_event(event: LiveEvent) -> None:
        counts[event.kind] += 1
        if args.json:
            print(json.dumps(monitor_event(event), ensure_ascii=False, default=str), flush=True)
        else:
            print(format_monitor_event(event, raw=args.raw), flush=True)

    if not args.quiet_log:
        log.on(on_log)
    supervisor.on_state(on_state)
    supervisor.on_event(on_event)

    def request_stop() -> None:
        if not stop_event.is_set():
            info(f"\n[{timestamp()}] shutdown requested")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
            installed_signals.append(sig)
        except (NotImplementedError, AttributeError):
            pass

    try:
        info("=" * 96)
        info("LiveCore Live Long Connection Monitor")
        info("=" * 96)
        info(f"platform={adapter.name} ({adapter.display_name})")
        info(f"room_id={args.room_id}")
        info(f"heartbeat={adapter.heartbeat_interval_sec:.0f}s via adapter")
        schema_info = getattr(adapter, "schema", None)
        if schema_info is not None:
            described = schema_info.describe()
            info(f"protobuf schema={described['path']}")
            info(f"protobuf commands={','.join(described['protobuf_commands']) or '<none>'}")
            if described["last_error"]:
                info(f"protobuf schema ERROR={described['last_error']}")
        info(f"duration={'unlimited' if args.duration is None else f'{args.duration}s'}")
        info(f"output={'jsonl' if args.json else 'monitor'}")
        info("Ctrl+C to stop")
        info()

        await supervisor.start()
        info(f"[{timestamp()}] [READY] supervisor started; waiting for events")

        if args.duration is None:
            await stop_event.wait()
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
            except TimeoutError:
                info(f"[{timestamp()}] [TIMEOUT] duration reached")

    except KeyboardInterrupt:
        info(f"\n[{timestamp()}] KeyboardInterrupt received")
    finally:
        info(f"[{timestamp()}] [SHUTDOWN] stopping supervisor...")
        try:
            await supervisor.stop()
        except Exception as exc:
            info(f"[{timestamp()}] [SHUTDOWN ERROR] {type(exc).__name__}: {exc}")

        for sig in installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, AttributeError):
                pass

        health = supervisor.snapshot()
        info()
        info("=" * 96)
        info("Session summary")
        info("=" * 96)
        info(f"platform={health.platform}")
        info(f"room_id={health.room_id}")
        info(f"final_state={health.state}")
        info(f"reconnects={health.reconnects}")
        info(f"last_error={health.last_error or '<none>'}")
        info(f"events_total={sum(counts.values())}")
        info("event_counts:")
        for kind, count in sorted(counts.items()):
            info(f"  {kind:<12} {count}")
        info("=" * 96)
        info("LiveCore exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
