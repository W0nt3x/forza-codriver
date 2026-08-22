"""The co-driver itself: run."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    from ..runtime.run import run_stage
    from ..stage.schema import load

    stage = load(args.stage)
    record_dir = None
    if cfg.get("runtime.record.enabled") and not args.no_record:
        record_dir = Path(cfg.get("runtime.record.dir"))
    run_stage(
        stage,
        cfg,
        silent=args.silent,
        hud=not args.no_hud,
        max_frames=args.max_frames,
        record_dir=record_dir,
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "run",
        help="the co-driver: localise on a stage and speak its notes",
    )
    p.add_argument("stage", help="stage JSON produced by `codriver build`")
    p.add_argument(
        "--silent",
        action="store_true",
        help="no audio output; notes appear in the HUD only",
    )
    p.add_argument("--no-hud", action="store_true", help="no status line")
    p.add_argument(
        "--no-record",
        action="store_true",
        help="do not save this drive as a run for `codriver learn`",
    )
    p.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="stop after N telemetry frames (testing)",
    )
    p.set_defaults(func=cmd_run)
