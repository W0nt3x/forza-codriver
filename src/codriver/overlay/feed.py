"""Where the overlay gets its events when it runs on its own: the UI's
WebSocket, the same stream the web HUD and the phone read.

Reconnects forever with a short pause, so the overlay can be started before
the UI, or survive the UI restarting. Stop by setting the event.
"""

from __future__ import annotations

import json
import logging
import threading

from .state import OverlayState

log = logging.getLogger(__name__)


def ws_feed(url: str, state: OverlayState, stop: threading.Event, retry_s: float = 2.0) -> None:
    from websockets.sync.client import connect

    while not stop.is_set():
        try:
            with connect(url, open_timeout=3, close_timeout=1, max_size=1_000_000) as ws:
                log.info("overlay feed: connected to %s", url)
                state.set_connected(True)
                while not stop.is_set():
                    try:
                        message = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    if isinstance(message, (bytes, bytearray)):
                        continue
                    try:
                        state.handle_event(json.loads(message))
                    except ValueError:
                        continue
        except Exception as exc:  # refused, dropped, closed: all the same to us
            log.debug("overlay feed: %s", exc)
        finally:
            state.set_connected(False)
        stop.wait(retry_s)


def start_ws_feed(url: str, state: OverlayState) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=ws_feed, args=(url, state, stop), name="codriver-overlay-feed", daemon=True)
    thread.start()
    return thread, stop
