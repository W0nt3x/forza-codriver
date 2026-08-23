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
    from ..overlay.feed import start_ws_feed

    try:
        overlay = Overlay(cfg)
    except ValueError as exc:  # a bad hotkey string in the config
        print(f"overlay: {exc}", file=sys.stderr)
        return 2
    url = args.url or str(cfg.get("overlay.feed_url"))
    print(f"overlay up, reading {url}. {overlay.hotkey_text} toggles edit mode (drag to move, "
          f"corner to resize). Forza must run in Borderless Windowed. Ctrl+C here closes it.", flush=True)
    feed_thread, stop = start_ws_feed(url, overlay.state)
    try:
        overlay.run()
    except KeyboardInterrupt:
        overlay.window.request_close()
    finally:
        stop.set()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("overlay", help="transparent on-screen overlay over Forza (Borderless Windowed)")
    p.add_argument("--url", default=None, help="event stream to read (default: overlay.feed_url, the UI's /ws)")
    p.set_defaults(func=cmd_overlay)
