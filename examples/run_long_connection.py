"""Production-style Bilibili live long-connection example.

Run forever until Ctrl+C/SIGTERM:
    python examples/run_long_connection.py 1814378608

Run a bounded soak test:
    python examples/run_long_connection.py 1814378608 --duration 1800

The example deliberately uses ConnectionSupervisor so endpoint discovery,
WebSocket lifecycle, heartbeat, reconnect and shutdown all exercise the SDK's
public supervision layer. Shutdown never calls loop.stop() or os._exit(); it
lets supervisor/client cancel and await their tasks before asyncio.run closes
the event loop.
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
from livecore.types import LiveEvent


EVENT_NAMES = {
    "danmaku": "弹幕",
    "gift": "礼物",
    "enter": "进场",
    "follow": "关注",
    "share": "分享",
    "guard": "舰长",
    "superchat": "醒目留言",
    "like": "点赞",
    "system": "系统",
    "popularity": "人气",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LiveCore Bilibili long connection example")
    parser.add_argument("room_id", type=int, help="Bilibili live room ID")
    parser.add_argument("--duration", type=float, default=None, help="Run seconds; default: forever")
    parser.add_argument("--json", action="store_true", help="Print every event as JSON")
    parser.add_argument("--raw", action="store_true", help="Also print raw command for each event")
    return parser.parse_args()


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def user_text(event: LiveEvent) -> str:
    if event.user is None:
        return ""
    extras = []
    if event.user.guard:
        extras.append(f"guard={event.user.guard}")
    if event.user.medal:
        extras.append(f"medal={event.user.medal}")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"{event.user.name}{suffix}"


def event_payload(event: LiveEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": event.id,
        "ts": event.ts,
        "kind": event.kind,
        "room_id": event.room_id,
        "text": event.text,
        "sentiment": event.sentiment,
        "raw_cmd": event.raw_cmd,
        "popularity": event.popularity,
    }
    if event.user is not None:
        payload["user"] = {
            "uid": event.user.uid,
            "name": event.user.name,
            "guard": event.user.guard,
            "medal": event.user.medal,
        }
    if event.gift is not None:
        payload["gift"] = {
            "name": event.gift.name,
            "num": event.gift.num,
            "price": event.gift.price,
        }
    return payload


def format_event(event: LiveEvent, *, raw: bool = False) -> str:
    name = EVENT_NAMES.get(event.kind, event.kind)
    user = user_text(event)

    if event.kind == "danmaku":
        body = f"{user}: {event.text}"
    elif event.kind == "gift" and event.gift:
        body = (
            f"{user} 送出 {event.gift.name} x{event.gift.num}"
            f" price={event.gift.price}"
        )
    elif event.kind == "superchat":
        body = f"{user}: {event.text}"
    elif event.kind == "enter":
        body = f"{user} 进入直播间"
    elif event.kind == "follow":
        body = f"{user} 关注了主播"
    elif event.kind == "share":
        body = f"{user} 分享了直播间"
    elif event.kind == "guard":
        body = f"{user} 成为/续费舰长 {event.text}".strip()
    elif event.kind == "like":
        body = f"{user} 点赞"
    elif event.kind == "popularity":
        body = f"online={event.popularity}"
    elif event.kind == "system":
        body = event.text or "system event"
    else:
        body = event.text or repr(event)

    if event.sentiment:
        body += f" sentiment={event.sentiment}"
    if raw and event.raw_cmd:
        body += f" raw_cmd={event.raw_cmd}"

    return f"[{timestamp()}] [{name}/{event.kind}] {body}"


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

    def on_log(entry: Any) -> None:
        level = getattr(entry, "level", "INFO")
        layer = getattr(entry, "layer", "livecore")
        message = getattr(entry, "message", str(entry))
        print(f"[{timestamp()}] [LOG/{level}/{layer}] {message}")

    def on_state(state: str) -> None:
        print(f"[{timestamp()}] [STATE] {state}")

    def on_event(event: LiveEvent) -> None:
        counts[event.kind] += 1
        if args.json:
            print(json.dumps(event_payload(event), ensure_ascii=False, default=str))
        else:
            print(format_event(event, raw=args.raw))

    log.on(on_log)
    supervisor.on_state(on_state)
    supervisor.on_event(on_event)

    def request_stop() -> None:
        if not stop_event.is_set():
            print(f"\n[{timestamp()}] shutdown requested")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
            installed_signals.append(sig)
        except (NotImplementedError, AttributeError):
            # Windows/unsupported event loops fall back to KeyboardInterrupt.
            pass

    try:
        print("=" * 72)
        print("LiveCore Bilibili Long Connection")
        print("=" * 72)
        print(f"room_id={args.room_id}")
        print(f"duration={'unlimited' if args.duration is None else f'{args.duration}s'}")
        print(f"output={'json' if args.json else 'human'}")
        print("Ctrl+C to stop")
        print()

        await supervisor.start()
        print(f"[{timestamp()}] [READY] supervisor started; waiting for events")

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
        print("=" * 72)
        print("Session summary")
        print("=" * 72)
        print(f"room_id={health.room_id}")
        print(f"final_state={health.state}")
        print(f"reconnects={health.reconnects}")
        print(f"last_error={health.last_error or '<none>'}")
        print(f"events_total={sum(counts.values())}")
        print("event_counts:")
        for kind, count in sorted(counts.items()):
            print(f"  {kind:<12} {count}")
        print("=" * 72)
        print("LiveCore exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
