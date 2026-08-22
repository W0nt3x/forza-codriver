"""Commands that talk to the live game: fields, listen, capture, scan."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ..adapters import get_adapter
from ..adapters.base import PacketError
from ..config import Config
from ..record import capture as cap
from ._common import err, frame_line, open_listener, waiting_hint


def cmd_fields(args: argparse.Namespace, cfg: Config) -> int:
    from ..adapters import fh6

    print(fh6.layout_table())
    return 0


def cmd_listen(args: argparse.Namespace, cfg: Config) -> int:
    adapter = get_adapter(cfg.get("telemetry.adapter"))
    print_hz = cfg.get("logging.listen_print_hz")
    min_interval = 1.0 / print_hz if print_hz > 0 else 0.0

    err(waiting_hint(cfg))
    seen = 0
    bad = 0
    start_ns: int | None = None
    last_print = 0.0

    with open_listener(cfg) as listener:
        try:
            while True:
                got = listener.recv()
                if got is None:
                    continue
                data, t_ns = got
                if start_ns is None:
                    start_ns = t_ns
                    err(f"stream started, {len(data)}-byte datagrams")
                try:
                    frame = adapter.parse(data, (t_ns - start_ns) / 1e9)
                except PacketError as exc:
                    bad += 1
                    if bad <= 3:
                        err(f"undecodable datagram: {exc}")
                    continue
                seen += 1
                now = time.monotonic()
                if now - last_print >= min_interval:
                    last_print = now
                    print(frame_line(frame))
                if args.count and seen >= args.count:
                    break
        except KeyboardInterrupt:
            err("")

    err(f"{seen} frames decoded, {bad} rejected, {listener.stats.rate_hz:.1f} Hz")
    return 0


def cmd_capture(args: argparse.Namespace, cfg: Config) -> int:
    from ..record.recon import capture_stream

    directory = Path(args.dir) if args.dir else cfg.path("capture.dir")
    path = Path(args.output) if args.output else cap.default_capture_path(
        directory, args.name
    )

    def show(event: dict) -> None:
        kind = event["kind"]
        if kind == "waiting":
            err(waiting_hint(cfg))
        elif kind == "started":
            err(f"recording {event['packet_size']}-byte datagrams to {path}")
        elif kind == "fixture":
            err(f"\nwrote test fixture to {event['path']}")
        elif kind == "status":
            if event.get("idle"):
                line = f"\r  {event['packets']} packets, stream idle ...      "
            else:
                line = (
                    f"\r  {event['elapsed_s']:7.1f}s  {event['packets']:7d} pkts  "
                    f"{'RACE' if event.get('race_on') else 'menu'}  "
                    f"{event.get('speed_kmh', 0.0):6.1f} km/h  "
                    f"{event.get('distance_m', 0.0):8.1f} m       "
                )
            print(line, end="", file=sys.stderr, flush=True)

    result = capture_stream(
        cfg,
        path,
        note=args.note or "",
        duration_s=args.duration,
        fixture_path=Path(args.fixture) if args.fixture else None,
        on_event=show,
    )

    err("")
    if result.packets == 0:
        err(
            "no packets received. Check Data Out is On, the port matches, and "
            "that you were actually driving (not sitting in a menu)."
        )
        return 1
    err(
        f"wrote {result.packets} packets ({result.bytes_written / 1e6:.1f} MB), "
        f"{result.race_frames} race-on, to {path}"
    )
    print(path)
    return 0


def cmd_scan(args: argparse.Namespace, cfg: Config) -> int:
    from ..net import scan as scanner

    ports = scanner.parse_port_spec(args.ports or scanner.DEFAULT_SPEC)
    configured = cfg.get("telemetry.port")
    if configured not in ports:
        ports = sorted({configured, *ports})

    err(
        f"listening on {len(ports)} ports for {args.duration:.0f}s.\n"
        f"  Start driving now, FH6 sends nothing in menus, pauses or replays,\n"
        f"  so a scan while parked in a garage will find exactly nothing."
    )

    def announce(hit) -> None:
        err(f"\n  {hit.describe()}")

    result = scanner.scan(
        ports,
        duration_s=args.duration,
        allow_reserved=args.allow_reserved,
        on_first_hit=announce,
    )

    if result.skipped_reserved:
        lo, hi = scanner.RESERVED_LO, scanner.RESERVED_HI
        err(
            f"\nskipped {len(result.skipped_reserved)} ports in {lo}-{hi}: the "
            f"game binds its own outgoing socket in that range, and taking it "
            f"would break Data Out rather than diagnose it."
        )
    if result.refused:
        err(
            f"\n{len(result.refused)} port(s) already in use by another "
            f"process: {sorted(result.refused)[:12]}"
        )
        if configured in result.refused:
            err(
                f"  Your configured port {configured} is one of them. That is "
                f"very likely the whole problem, SimHub, a motion rig, or a "
                f"`codriver listen` you left running."
            )

    err("")
    if not result.found:
        err(
            f"nothing received on any of {result.bound} ports in "
            f"{result.duration_s:.0f}s.\n"
            f"  1. Is Data Out actually On? "
            f"SETTINGS -> HUD AND GAMEPLAY -> Data Out\n"
            f"  2. Is Data Out IP Address 127.0.0.1?\n"
            f"  3. Were you driving? Menus, pauses and replays send nothing.\n"
            f"  4. If the in-game port is inside "
            f"{scanner.RESERVED_LO}-{scanner.RESERVED_HI}, change it to "
            f"{configured}, that range is unusable, whatever the guides say."
        )
        return 1

    err("found telemetry:")
    for hit in result.found:
        err(f"  {hit.describe()}")
    best = result.found[0]
    if best.port != configured:
        err(
            f"\nThe game is sending to {best.port}, but this project is "
            f"configured for {configured}. Either change Data Out IP Port to "
            f"{configured} in game, or put this in config/local.yaml:\n"
            f"\ntelemetry:\n  port: {best.port}\n"
        )
    else:
        err(
            f"\nPort {configured} matches your config. If a capture is still "
            f"coming back empty, something else is wrong, try "
            f"`codriver listen`."
        )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("fields", help="print the packet layout in use")
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("listen", help="decode and print live telemetry")
    p.add_argument("--count", type=int, default=0, help="stop after N frames")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("capture", help="record raw datagrams to a .fzr file")
    p.add_argument("-o", "--output", help="explicit output path")
    p.add_argument("--dir", help="output directory (default: capture.dir)")
    p.add_argument("--name", help="basename; default is a timestamp")
    p.add_argument("--note", help="free text stored in the capture header")
    p.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    p.add_argument(
        "--fixture",
        help="also write the first race-on datagram to this path, as a test fixture",
    )
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("scan", help="find which UDP port the game is actually sending to")
    p.add_argument(
        "--ports",
        default=None,
        help="port spec, e.g. 5301-5500,8000 (default: a wide sweep)",
    )
    p.add_argument("--duration", type=float, default=20.0, help="seconds to listen")
    p.add_argument(
        "--allow-reserved",
        action="store_true",
        help="also bind 5200-5300. Do not: the game binds its own outgoing "
        "socket there and this can break Data Out entirely.",
    )
    p.set_defaults(func=cmd_scan)
