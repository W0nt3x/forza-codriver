"""What the overlay knows, fed by the runtime's event dicts.

The same events the web HUD gets (``status`` every 0.2 s, ``note`` when a
call is spoken, ``suspended`` / ``jump`` / ``localised`` / ``done``) arrive
here from whatever transport is in use, and ``view()`` turns them into the
few things the renderer draws: the next call, the one after, the distance to
the next, and whether the picture is live, stale or idle.

Thread-safe: events come from the run thread or a socket thread, frames are
drawn on the window thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

STALE_AFTER_S = 2.0
"""No status for this long while tracking: dim the picture rather than show
a frozen arrow at a pause the runtime did not get to announce."""


@dataclass(frozen=True)
class NoteBrief:
    text: str
    tokens: tuple[str, ...]
    severity: int | None
    direction: str | None
    kind: str
    at_m: float
    in_m: float | None = None
    """Metres ahead of the car when the status was sent, as the runtime
    measured it; on a circuit that is the short way round the seam, which
    ``at_m - along_m`` gets wrong for the first corners of the next lap."""

    @classmethod
    def from_dict(cls, d: Any) -> "NoteBrief | None":
        if not isinstance(d, dict):
            return None
        try:
            tokens = tuple(str(t)[:40] for t in (d.get("tokens") or []))[:24]
            sev = d.get("severity")
            in_m = d.get("in_m")
            return cls(
                text=str(d.get("text") or " ".join(tokens))[:120],
                tokens=tokens,
                severity=int(sev) if sev is not None else None,
                direction=str(d["direction"])[:10] if d.get("direction") else None,
                kind=str(d.get("kind") or "corner")[:20],
                at_m=float(d.get("at_m", 0.0)),
                in_m=float(in_m) if in_m is not None else None,
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class View:
    """One frame's worth of facts for the renderer."""

    mode: str = "idle"
    """idle (no run), waiting (run up, no telemetry), tracking, suspended, lost, stale."""
    next: NoteBrief | None = None
    after: NoteBrief | None = None
    distance_m: float | None = None
    speed_kmh: float = 0.0
    connected: bool = False
    """Whether a source of events is attached at all (UI running / socket up)."""


_MODE_FROM_STATE = {"tracking": "tracking", "cold": "waiting", "lost": "lost", "suspended": "suspended"}


@dataclass
class OverlayState:
    connected: bool = False
    mode: str = "idle"
    along_m: float = 0.0
    speed_mps: float = 0.0
    status_t: float = 0.0
    upcoming: list[NoteBrief] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_connected(self, on: bool) -> None:
        with self._lock:
            self.connected = bool(on)
            if not on:
                self.mode = "idle"
                self.upcoming = []

    def handle_event(self, event: dict, now: float | None = None) -> None:
        """One runtime event. Events of other jobs (capture, scan) are ignored."""
        if not isinstance(event, dict):
            return
        if event.get("job") not in (None, "run", "auto"):
            return
        kind = event.get("kind")
        now = time.monotonic() if now is None else now
        with self._lock:
            if kind == "status":
                self.mode = _MODE_FROM_STATE.get(str(event.get("state", "")), self.mode)
                try:
                    self.along_m = float(event.get("along_m", self.along_m))
                    self.speed_mps = float(event.get("speed_kmh", 0.0)) / 3.6
                except (TypeError, ValueError):
                    pass
                raw = event.get("upcoming")
                if isinstance(raw, list):
                    self.upcoming = [n for n in (NoteBrief.from_dict(d) for d in raw[:8]) if n is not None][:2]
                self.status_t = now
            elif kind == "waiting":
                self.mode = "waiting"
                self.upcoming = []
            elif kind == "localised":
                self.mode = "tracking"
                self.status_t = now
            elif kind == "suspended":
                self.mode = "suspended"
            elif kind in ("done", "finished", "error", "auto_done") or (
                    kind == "started_job" and event.get("job") not in ("run", "auto")):
                self.mode = "idle"
                self.upcoming = []

    def view(self, now: float | None = None) -> View:
        now = time.monotonic() if now is None else now
        with self._lock:
            mode = self.mode
            nxt = self.upcoming[0] if self.upcoming else None
            after = self.upcoming[1] if len(self.upcoming) > 1 else None
            distance = None
            if nxt is not None:
                rolled = 0.0
                if mode == "tracking":
                    if now - self.status_t > STALE_AFTER_S:
                        mode = "stale"
                    else:
                        # between two status events, roll the position forward
                        # at the last known speed so the distance moves smoothly
                        rolled = self.speed_mps * max(0.0, now - self.status_t)
                ahead = nxt.in_m if nxt.in_m is not None else nxt.at_m - self.along_m
                distance = max(0.0, ahead - rolled)
            return View(mode=mode, next=nxt, after=after, distance_m=distance,
                        speed_kmh=self.speed_mps * 3.6, connected=self.connected)
