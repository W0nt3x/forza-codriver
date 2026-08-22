"""Commands that build and inspect stages: build, notes, gpx."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config
from ._common import err


def cmd_build(args: argparse.Namespace, cfg: Config) -> int:
    from ..stage.build import build_stage
    from ..stage.schema import render_notes, save

    stage, report = build_stage(
        args.path, cfg, name=args.name, segment_index=args.segment
    )
    err(f"built {stage.name} from {args.path}")
    err(report.render())

    out = Path(args.output) if args.output else Path("stages") / f"{stage.name}.json"
    save(stage, out)
    err(f"\nwrote {out}")

    if not args.quiet:
        print()
        print(render_notes(stage))
    print(out)
    return 0


def cmd_notes(args: argparse.Namespace, cfg: Config) -> int:
    from ..stage.schema import load, render_notes

    stage = load(args.path)
    if args.tokens:
        from ..stage.notes import required_tokens

        for token in sorted(required_tokens(stage.notes)):
            print(token)
        return 0
    print(render_notes(stage))
    return 0


def cmd_gpx(args: argparse.Namespace, cfg: Config) -> int:
    from ..stage.gpx import to_gpx
    from ..stage.schema import load

    stage = load(args.path)
    text = to_gpx(stage, lat0=args.lat, lon0=args.lon, by_class=not args.single_track)
    out = Path(args.output) if args.output else Path(args.path).with_suffix(".gpx")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    err(
        f"wrote {out}, drop it on gpsvisualizer.com/map_input to eyeball "
        f"the classification (one colour per corner class)."
    )
    print(out)
    return 0


def cmd_learn(args: argparse.Namespace, cfg: Config) -> int:
    from ..stage.learn import learn_stage, runs_for_stage
    from ..stage.schema import load, render_notes, save

    stage_path = Path(args.stage)
    stage = load(stage_path)
    runs = [Path(r) for r in args.runs] or runs_for_stage(
        stage, Path(cfg.get("runtime.record.dir"))
    )
    if not runs:
        err(
            f"no runs found for '{stage.name}' in {cfg.get('runtime.record.dir')}. "
            f"Drive it with `codriver run` first (recording is on by default)."
        )
        return 1
    err(f"learning {stage.name} from {len(runs)} run(s):")
    for r in runs:
        err(f"  {r}")

    learned, report = learn_stage(stage, cfg, runs)
    err(report.render())

    out = Path(args.output) if args.output else stage_path
    if out == stage_path and not args.no_backup:
        backup = stage_path.with_suffix(".json.bak")
        backup.write_bytes(stage_path.read_bytes())
        err(f"previous stage kept at {backup}")
    save(learned, out)
    err(f"wrote {out}")
    if not args.quiet:
        print()
        print(render_notes(learned))
    print(out)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("build", help="turn a recon capture into a stage JSON")
    p.add_argument("path", help="capture (.fzr) to build from")
    p.add_argument("-o", "--output", help="output path (default: stages/<name>.json)")
    p.add_argument("--name", help="stage name (default: the capture's filename)")
    p.add_argument(
        "--segment",
        type=int,
        default=None,
        help="which continuous run to use (default: the longest)",
    )
    p.add_argument("--quiet", action="store_true", help="do not print the notes")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser(
        "learn",
        help="fold recorded runs into a stage: average the line, note the "
        "speeds actually driven, rebuild the notes",
    )
    p.add_argument("stage", help="stage JSON to improve")
    p.add_argument(
        "runs",
        nargs="*",
        help="run captures (.fzr); default: every recordings/runs/<stage>_*.fzr",
    )
    p.add_argument("-o", "--output", help="write here instead of in place")
    p.add_argument("--no-backup", action="store_true", help="no .json.bak")
    p.add_argument("--quiet", action="store_true", help="do not print the notes")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("notes", help="print the pace notes in a stage")
    p.add_argument("path", help="stage JSON")
    p.add_argument(
        "--tokens",
        action="store_true",
        help="list the distinct tokens a voice pack would need instead",
    )
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("gpx", help="export a stage to GPX for gpsvisualizer.com")
    p.add_argument("path", help="stage JSON")
    p.add_argument("-o", "--output")
    p.add_argument("--lat", type=float, default=0.0, help="fake origin latitude")
    p.add_argument("--lon", type=float, default=0.0, help="fake origin longitude")
    p.add_argument(
        "--single-track",
        action="store_true",
        help="one track instead of one per corner class",
    )
    p.set_defaults(func=cmd_gpx)
