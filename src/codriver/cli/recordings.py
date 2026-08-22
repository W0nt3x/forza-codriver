"""Commands that work on capture files: info, verify, decode, synth, replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..adapters import get_adapter
from ..adapters.base import PacketError
from ..config import Config
from ..record import capture as cap
from ..record import replay as rep
from ..record.verify import verify_capture
from ._common import err


def cmd_info(args: argparse.Namespace, cfg: Config) -> int:
    summary = cap.summarize(args.path, gap_threshold_s=args.gap_threshold)
    print(f"{summary.path}")
    print(f"  header       {json.dumps(summary.header, sort_keys=True)}")
    print(f"  packets      {summary.packets}")
    print(f"  duration     {summary.duration_s:.2f} s")
    print(f"  rate         {summary.rate_hz:.1f} Hz")
    sizes = ", ".join(
        f"{n} bytes x{c}" for n, c in sorted(summary.size_histogram.items())
    )
    print(f"  sizes        {sizes}")
    print(
        f"  gaps         {len(summary.gaps)} longer than "
        f"{args.gap_threshold}s, {summary.gap_total_s:.2f}s total"
    )
    for g in summary.gaps[: args.max_gaps]:
        print(f"                 at {g.at_s:8.2f}s  for {g.duration_s:6.2f}s")
    if len(summary.gaps) > args.max_gaps:
        print(f"                 ... and {len(summary.gaps) - args.max_gaps} more")
    if summary.truncated:
        print("  NOTE         file ends in a truncated record (capture was killed)")
    return 0


def cmd_verify(args: argparse.Namespace, cfg: Config) -> int:
    report = verify_capture(args.path)
    print(report.render())
    return 0 if report.ok else 1


def cmd_decode(args: argparse.Namespace, cfg: Config) -> int:
    adapter = get_adapter(cfg.get("telemetry.adapter"))
    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    written = 0
    skipped = 0
    try:
        with cap.CaptureReader(Path(args.path)) as reader:
            for t_ns, payload in reader:
                try:
                    row = adapter.describe(payload)
                except PacketError:
                    skipped += 1
                    continue
                if args.race_on_only and not row["IsRaceOn"]:
                    continue
                row["t"] = t_ns / 1e9
                out.write(json.dumps(row) + "\n")
                written += 1
    finally:
        if args.output:
            out.close()
    err(f"{written} rows written, {skipped} undecodable")
    return 0


def cmd_synth(args: argparse.Namespace, cfg: Config) -> int:
    from ..record.synth import SynthSpec, write_synth

    spec = SynthSpec(
        shape=args.shape,
        duration_s=args.duration,
        speed_mps=args.speed_kmh / 3.6,
        size_m=args.size,
    )
    path = Path(args.output)
    count = write_synth(path, spec)
    err(
        f"wrote {count} synthetic packets ({spec.duration_s:.0f}s of "
        f"{spec.shape} at {args.speed_kmh:.0f} km/h) to {path}"
    )
    err(
        "This is generated, not recorded: it proves the tooling works, "
        "not that the packet layout is right. Only a real capture can do that."
    )
    print(path)
    return 0


def cmd_replay(args: argparse.Namespace, cfg: Config) -> int:
    host = args.host or cfg.get("replay.host")
    port = args.port if args.port is not None else cfg.get("replay.port")
    speed = args.speed if args.speed is not None else cfg.get("replay.speed")
    max_gap = args.max_gap if args.max_gap is not None else cfg.get("replay.max_gap_s")
    loop = args.loop or cfg.get("replay.loop")

    with cap.CaptureReader(Path(args.path)) as reader:
        records = list(reader)
    if not records:
        err("capture is empty")
        return 1

    total_s = records[-1][0] / 1e9
    err(
        f"replaying {len(records)} packets ({total_s:.1f}s) to {host}:{port} "
        f"at {speed}x{' looping' if loop else ''}"
        + (f", gaps clamped to {max_gap}s" if max_gap else "")
    )

    def progress(i: int, total: int, elapsed: float) -> None:
        print(
            f"\r  {elapsed:7.1f}s  {i + 1}/{total} packets   ",
            end="",
            file=sys.stderr,
            flush=True,
        )

    try:
        stats = rep.replay_records(
            records,
            host=host,
            port=port,
            speed=speed,
            loop=loop,
            max_gap_s=max_gap,
            spin_margin_s=cfg.get("replay.spin_margin_s"),
            progress=None if args.quiet else progress,
        )
    except KeyboardInterrupt:
        err("\ninterrupted")
        return 130
    err(f"\n{stats.summary()}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("info", help="summarise a capture")
    p.add_argument("path")
    p.add_argument("--gap-threshold", type=float, default=0.5)
    p.add_argument("--max-gaps", type=int, default=20)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("verify", help="check the packet layout against a capture")
    p.add_argument("path")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("decode", help="export a capture to NDJSON")
    p.add_argument("path")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.add_argument("--race-on-only", action="store_true")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser(
        "synth",
        help="generate a synthetic capture, so the tooling can be exercised "
        "without launching the game",
    )
    p.add_argument("-o", "--output", default="recordings/synthetic.fzr")
    p.add_argument("--shape", default="figure8", choices=["circle", "figure8", "slalom"])
    p.add_argument("--duration", type=float, default=90.0, help="seconds")
    p.add_argument("--speed-kmh", type=float, default=90.0)
    p.add_argument("--size", type=float, default=250.0, help="course size in metres")
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("replay", help="pump a capture back out over UDP")
    p.add_argument("path")
    p.add_argument("--host", default=None)
    p.add_argument("--speed", type=float, default=None, help="1.0 = original timing")
    p.add_argument("--loop", action="store_true")
    p.add_argument(
        "--max-gap",
        type=float,
        default=None,
        help="clamp stream gaps to N seconds (default: preserve them exactly)",
    )
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_replay)
