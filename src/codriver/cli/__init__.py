"""Command line entry point: ``python -m codriver <command>``.

Each command group lives in its own module and registers its subparsers;
this file only assembles them and handles the global options.

    telemetry    fields, listen, capture, scan:       talking to the game
    recordings   info, verify, decode, synth, replay: working on .fzr files
    stages       build, notes, gpx, learn:            recon to pace notes
    voice        voice generate / check / say:        voice packs
    runtime      run:                                 the co-driver
    ui           ui:                                  the browser UI
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..config import Config
from ..record.capture import CaptureError
from . import auto, overlay, recordings, runtime, stages, telemetry, ui, voice
from ._common import setup_logging


def cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    print(f"# merged from {cfg.config_dir}")
    print(f"# defaults.yaml + local.yaml ({'present' if cfg.local_path.is_file() else 'absent'})")
    if args.key:
        print(json.dumps(cfg.get(args.key), indent=2, default=str))
    else:
        print(cfg.describe())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codriver",
        description="Rally pace-note co-driver for Forza Horizon 6.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config-dir",
        help="directory containing defaults.yaml (default: nearest config/)",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="override telemetry.port / replay.port",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("config", help="print the merged configuration")
    p.add_argument("key", nargs="?", help="dotted key, e.g. runtime.trigger")
    p.set_defaults(func=cmd_config)

    telemetry.register(sub)
    recordings.register(sub)
    stages.register(sub)
    voice.register(sub)
    runtime.register(sub)
    ui.register(sub)
    overlay.register(sub)
    auto.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Set logging up before the config is read, so that a typo warning from
    # local.yaml is formatted like every other message rather than falling
    # through to the last-resort handler.
    setup_logging(args.log_level or "INFO")
    try:
        cfg = Config.load(args.config_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.log_level is None:
        logging.getLogger().setLevel(cfg.get("logging.level").upper())

    # A --port on the command line overrides both sides, so one flag works for
    # `capture` and `replay` alike.
    if args.port is not None:
        cfg.data["telemetry"]["port"] = args.port
        cfg.data["replay"]["port"] = args.port

    try:
        return args.func(args, cfg)
    except (CaptureError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
