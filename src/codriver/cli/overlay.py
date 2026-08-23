"""The on-screen overlay: overlay."""

from __future__ import annotations

import argparse
import sys

from ..config import Config


def cmd_overlay(args: argparse.Namespace, cfg: Config) -> int:
    if sys.platform != "win32":
        print("the overlay is Windows only (it is a Win32 layered window)", file=sys.stderr)
        return 2
    from ..overlay.app import Overlay

    try:
        overlay = Overlay(cfg)
    except ValueError as exc:  # a bad hotkey string in the config
        print(f"overlay: {exc}", file=sys.stderr)
        return 2
    print(f"overlay up. {overlay.hotkey_text} toggles edit mode (drag to move, corner to resize). "
          f"Forza must run in Borderless Windowed. Ctrl+C here closes it.", flush=True)
    try:
        overlay.run()
    except KeyboardInterrupt:
        overlay.window.request_close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("overlay", help="transparent on-screen overlay over Forza (Borderless Windowed)")
    p.set_defaults(func=cmd_overlay)
