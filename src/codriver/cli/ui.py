"""The browser UI: ui."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config


def cmd_ui(args: argparse.Namespace, cfg: Config) -> int:
    from ..ui.server import serve

    # Stages, recordings and voices live next to config/, the project root.
    root = cfg.config_dir.parent
    serve(
        cfg,
        root,
        host="127.0.0.1" if args.local_only else "0.0.0.0",
        port=args.ui_port,
        open_browser=not args.no_browser,
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ui", help="open the browser UI (and a phone HUD on the same WLAN)")
    p.add_argument("--ui-port", type=int, default=8777, help="HTTP port (default 8777)")
    p.add_argument(
        "--local-only",
        action="store_true",
        help="only this PC can open the UI (no phone access)",
    )
    p.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    p.set_defaults(func=cmd_ui)
