"""Real Bilibili diagnostic for the HTTP/WebSocket connection path.

This is intentionally opt-in: it talks to the real Bilibili service and therefore
is not collected as a normal pytest test. Run it directly with:

    python tests/test_real_bilibili.py <room_id>

The diagnostic reports each stage without printing cookies or the full auth token.
"""

from __future__ import annotations

import asyncio
import sys
import time

from livecore.bili_http import BiliHttpError, fetch_danmu_endpoint


async def diagnose(room_id: int) -> int:
    print(f"[1/2] getDanmuInfo: room_id={room_id}")
    started = time.monotonic()
    try:
        endpoint = await fetch_danmu_endpoint(room_id)
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"FAILED after {elapsed:.2f}s")
        print(f"exception={type(exc).__name__}")
        print(f"message={exc}")
        if isinstance(exc, BiliHttpError) and "-352" in str(exc):
            print("diagnosis=http_352_at_danmu_endpoint")
        return 1

    elapsed = time.monotonic() - started
    print(f"OK after {elapsed:.2f}s")
    print(f"host={endpoint.host}")
    print(f"wss_port={endpoint.wss_port}")
    print(f"room_id={endpoint.room_id}")
    print(f"token_present={bool(endpoint.token)}")
    print(f"token_length={len(endpoint.token)}")

    print("[2/2] websocket: not attempted by this diagnostic")
    print("diagnosis=http_danmu_endpoint_ok")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tests/test_real_bilibili.py <room_id>")
        return 2
    try:
        room_id = int(sys.argv[1])
    except ValueError:
        print("room_id must be an integer")
        return 2
    if room_id <= 0:
        print("room_id must be positive")
        return 2
    return asyncio.run(diagnose(room_id))


if __name__ == "__main__":
    raise SystemExit(main())
