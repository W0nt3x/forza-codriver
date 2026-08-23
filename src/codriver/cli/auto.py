"""Evening mode from the terminal: auto."""

from __future__ import annotations

import argparse
import sys

from ..config import Config
from ._common import err


def cmd_auto(args: argparse.Namespace, cfg: Config) -> int:
    from ..runtime.auto import load_stages, session_auto

    stages = load_stages(cfg.root / "stages")
    if not stages:
        err("no stages on disk yet: every race will only be recorded. Build or install stages first.")

    def show(event: dict) -> None:
        kind = event["kind"]
        if kind == "auto_started":
            err(f"auto: {len(event['stages'])} stage(s) loaded, listening on port {event['port']}. "
                f"Drive; races are recognised on their own. Ctrl+C stops.")
        elif kind == "race_started":
            err(f"\nrace #{event['race']} started")
        elif kind == "auto_matched":
            err(f"  stage recognised: {event['stage']} ({event['distance_m']} m from its start), calling it")
        elif kind == "auto_unmatched":
            err("  no stage matches this start: recording only, build it later")
        elif kind == "note":
            err(f"  >> {event['text']}")
        elif kind == "race_saved":
            where = f"run of {event['stage']}" if event.get("stage") else "new race"
            err(f"  saved {event['path']} ({event['seconds']} s, {where})")
        elif kind == "race_discarded":
            err(f"  a {event['seconds']} s blip was not a race, dropped")
        elif kind == "auto_status":
            line = (f"\r  {event['stage'] or 'unknown stage'}: {event['packets']:6d} pkts  {event.get('speed_kmh', 0.0):6.1f} km/h   "
                    if event.get("racing") else f"\r  waiting for a race ({event['races']} saved)            ")
            print(line, end="", file=sys.stderr, flush=True)

    result = session_auto(cfg, stages, cfg.path("capture.dir"), cfg.path("runtime.record.dir"),
                          silent=args.silent, hud=False, on_event=show)
    err(f"\n{result.races} race(s), {result.matched} with a stage, {len(result.saved)} saved, {result.discarded} dropped")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("auto", help="evening mode: recognise every race, call the ones you have stages for, record all")
    p.add_argument("--silent", action="store_true", help="no audio (testing)")
    p.set_defaults(func=cmd_auto)
