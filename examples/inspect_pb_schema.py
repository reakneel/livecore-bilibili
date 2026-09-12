"""Detect protobuf schema drift on a live Bilibili room.

The ``*_V2`` commands carry their payload as Base64 protobuf in ``data.pb`` whose
field numbers are not public API. When Bilibili adds or renumbers a field, the
decoder does not crash — it just silently loses data. This tool watches real
traffic and reports exactly that:

    python examples/inspect_pb_schema.py 5050
    python examples/inspect_pb_schema.py 5050 --duration 120 --verbose

Exit status is non-zero when drift is found, so it can gate a scheduled job:
edit ``livecore/schemas/bilibili.pb.json`` (or push an overlay via
``config.json`` → ``protobuf.commands``) and the running client picks it up on
its next poll — no restart, no reconnect.

Use ``--dump`` to print the full nested structure of one sample per command,
which is what you need to fill in a newly observed field number.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

import websockets

from livecore import protocol as proto
from livecore.platforms import BilibiliAdapter
from livecore.pb import PbMessage
from livecore.schema import SchemaStore, store_from_settings

DEFAULT_ROOM = 5050


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect Bilibili protobuf schema drift")
    parser.add_argument("room_id", type=int, nargs="?", default=DEFAULT_ROOM,
                        help=f"Live room id (default {DEFAULT_ROOM})")
    parser.add_argument("--duration", type=float, default=60.0, help="Capture seconds")
    parser.add_argument("--schema", default="", help="Override the schema document path")
    parser.add_argument("--overlay", default="", help="Overlay schema document path")
    parser.add_argument("--dump", action="store_true", help="Print nested structure per command")
    parser.add_argument("--verbose", action="store_true", help="Also list mapped-but-unseen fields")
    parser.add_argument("--min-samples", type=int, default=1, help="Ignore commands seen fewer times")
    return parser.parse_args()


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def dump_message(buf: bytes, depth: int = 0, max_depth: int = 3) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for number in sorted(PbMessage(buf).fields):
        entries = PbMessage(buf).values(number)
        rendered: list[Any] = []
        for entry in entries:
            if entry.wire_type == proto.PROTO_RAW:
                rendered.append(entry.as_int())
                continue
            raw = entry.raw_bytes
            text = entry.as_text()
            if text and len(text) <= 200 and sum(c.isprintable() or c == " " for c in text) / len(text) > 0.9:
                rendered.append(text)
                continue
            if depth < max_depth:
                child = dump_message(raw, depth + 1, max_depth)
                if child:
                    rendered.append(child)
                    continue
            rendered.append(f"<bytes:{len(raw)}>")
        out[str(number)] = rendered[0] if len(rendered) == 1 else rendered
    return out


class CommandReport:
    __slots__ = ("cmd", "samples", "numbers", "resolved", "sample")

    def __init__(self, cmd: str) -> None:
        self.cmd = cmd
        self.samples = 0
        self.numbers: set[int] = set()
        self.resolved: set[str] = set()
        self.sample: bytes = b""


def inspect(raw_pb: bytes, reader, report: CommandReport) -> None:
    report.samples += 1
    report.numbers.update(reader.numbers())
    if reader.schema is not None:
        for name in reader.schema.names():
            if reader.has(name):
                report.resolved.add(name)
    if not report.sample:
        report.sample = raw_pb


async def run(args: argparse.Namespace, store: SchemaStore) -> int:
    adapter = BilibiliAdapter(schema=store)
    endpoint = await adapter.fetch_endpoint(adapter.normalize_room_id(args.room_id))
    url = adapter.websocket_url(endpoint)
    print(f"[{stamp()}] schema={store.path}")
    print(f"[{stamp()}] room={endpoint.room_id} url={url}")
    print(f"[{stamp()}] watching {args.duration:.0f}s ...")

    reports: "OrderedDict[str, CommandReport]" = OrderedDict()
    header = {"Origin": adapter.origin, "Referer": f"{adapter.origin}/{endpoint.room_id}"}
    stop_at = time.time() + args.duration

    async with websockets.connect(
        url,
        ping_interval=None,
        close_timeout=adapter.close_timeout_sec,
        additional_headers=header,
    ) as ws:
        await ws.send(adapter.build_auth_packet(endpoint))

        async def heartbeat() -> None:
            last = time.time()
            while time.time() < stop_at:
                await asyncio.sleep(1)
                if time.time() - last >= adapter.heartbeat_interval_sec:
                    await ws.send(adapter.build_heartbeat_packet())
                    last = time.time()

        beat = asyncio.create_task(heartbeat())
        try:
            while time.time() < stop_at:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, stop_at - time.time()))
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if isinstance(raw, str):
                    raw = raw.encode()
                for packet in proto.expand_packets(raw, strict=False):
                    if packet.op != proto.OP_NOTIFY:
                        continue
                    payload = proto.parse_json_body(packet.body)
                    if not isinstance(payload, dict):
                        continue
                    cmd = str(payload.get("cmd", "")).split(":", 1)[0]
                    data = payload.get("data")
                    if not isinstance(data, dict) or not isinstance(data.get("pb"), str):
                        continue
                    try:
                        raw_pb = base64.b64decode(data["pb"], validate=False)
                    except (ValueError, TypeError):
                        continue
                    blob = PbMessage(raw_pb)
                    report = reports.setdefault(cmd, CommandReport(cmd))
                    inspect(raw_pb, store.reader(cmd, blob), report)
        finally:
            beat.cancel()

    print()
    print("=" * 92)
    print("PROTOBUF SCHEMA REPORT")
    print("=" * 92)

    drift = False
    for cmd, report in reports.items():
        if report.samples < args.min_samples:
            continue
        reader = store.reader(cmd, PbMessage(report.sample))
        if reader.schema is None:
            print(f"[{stamp()}] {cmd}: NOT IN SCHEMA  ({report.samples} samples, fields={sorted(report.numbers)})")
            drift = True
            continue
        unmapped = reader.unmapped()
        status = "DRIFT" if unmapped else "ok"
        print(f"[{stamp()}] {cmd}: {status}  samples={report.samples} fields={sorted(report.numbers)}")
        if unmapped:
            drift = True
            for number in sorted(set(unmapped)):
                print(f"    + field {number} present in traffic but NOT in schema "
                      f"(add it to commands.{cmd}.fields)")
            if args.dump:
                print(json.dumps(dump_message(report.sample), ensure_ascii=False, indent=6))
        if args.verbose:
            never = sorted(set(reader.schema.names()) - report.resolved)
            if never:
                print(f"    - mapped but never seen in this capture: {never}")
        if args.dump and not unmapped:
            print(json.dumps(dump_message(report.sample), ensure_ascii=False, indent=6))

    if not reports:
        print("no protobuf commands observed in the capture window")
    print("=" * 92)
    print("schema_drift=" + ("YES" if drift else "no"))
    return 1 if drift else 0


def main() -> None:
    args = parse_args()
    if args.schema or args.overlay:
        store = SchemaStore(
            "bilibili",
            path=args.schema or None,
            overlay_path=args.overlay or None,
        )
    else:
        store = store_from_settings("bilibili")
    if store.last_error is not None:
        print(f"schema error: {store.last_error}", file=sys.stderr)
    raise SystemExit(asyncio.run(run(args, store)))


if __name__ == "__main__":
    main()
