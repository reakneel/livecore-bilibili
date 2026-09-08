"""Real Bilibili WebSocket smoke/soak diagnostic.

This is intentionally opt-in: it connects to the real Bilibili live service and
is not collected as a normal pytest test. Run it directly with:

    python tests/test_live_websocket.py <room_id> [--duration SECONDS]

The diagnostic validates endpoint discovery, WebSocket connection, auth reply,
heartbeat handling, event parsing, and clean shutdown. It does not send any
viewer interaction actions.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import time

from livecore import BiliLiveClient, LiveEvent, fetch_danmu_endpoint
from livecore.logger import RingLogger


async def diagnose(room_id: int, duration: float) -> int:
    print(f"[1/6] getDanmuInfo: room_id={room_id}")
    started = time.monotonic()
    try:
        endpoint = await fetch_danmu_endpoint(room_id)
    except Exception as exc:
        print(f"FAILED after {time.monotonic() - started:.2f}s")
        print(f"exception={type(exc).__name__}")
        print(f"message={exc}")
        return 1

    print("OK")
    print(f"host={endpoint.host}")
    print(f"wss_port={endpoint.wss_port}")
    print(f"real_room_id={endpoint.room_id}")
    print(f"token_present={bool(endpoint.token)}")

    print("[2/6] websocket: starting")
    logger = RingLogger()
    client = BiliLiveClient(logger)
    states: list[str] = []
    events: collections.Counter[str] = collections.Counter()
    connected_at: float | None = None

    async def on_state(state: str) -> None:
        nonlocal connected_at
        states.append(state)
        print(f"state={state}")
        if state == "live" and connected_at is None:
            connected_at = time.monotonic()

    async def on_event(event: LiveEvent) -> None:
        events[event.kind] += 1

    client.on_state(on_state)
    client.on_event(on_event)

    try:
        await client.start(endpoint)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        print("[6/6] shutdown: stopping client")
        await client.stop()

    live_seen = connected_at is not None
    print("[3/6] auth: " + ("OK" if live_seen else "NOT CONFIRMED"))
    print("[4/6] heartbeat: " + ("client task active during run" if live_seen else "NOT CONFIRMED"))
    print("[5/6] events:")
    if events:
        for kind, count in sorted(events.items()):
            print(f"    {kind:<12} {count}")
    else:
        print("    none")
    print(f"states={','.join(states) if states else 'none'}")
    print(f"duration={duration:.1f}s")

    if live_seen:
        print("diagnosis=websocket_live_ok")
        return 0
    print("diagnosis=websocket_auth_not_confirmed")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Real Bilibili WebSocket diagnostic")
    parser.add_argument("room_id", type=int)
    parser.add_argument("--duration", type=float, default=60.0)
    args = parser.parse_args()
    if args.room_id <= 0:
        parser.error("room_id must be positive")
    if args.duration <= 0:
        parser.error("duration must be positive")
    return asyncio.run(diagnose(args.room_id, args.duration))


if __name__ == "__main__":
    raise SystemExit(main())
