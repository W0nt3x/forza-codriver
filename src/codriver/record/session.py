"""Auto-record: one listener for the whole evening, a new recording per race.

Nothing in the packet says "this is an event", but two fields only ever carry
values inside one. In every capture taken so far, ``RacePosition`` is 1 and
up from the first metre of a race to the finish and 0 everywhere else: free
roam (even at 90 km/h with ``IsRaceOn`` set), the pre-race menu, the results
screen. The racing line (``NormalizedDrivingLine``) behaves the same. So a
race is "IsRaceOn and a race position", debounced both ways: a handful of
packets to start, a few seconds without to end, a long silence (loading
screen) to end as well.

Each race becomes ``recordings/race-<date>-<time>.fzr`` with a couple of
seconds of pre-roll (the countdown), and shows up in the build list like any
recording. Short blips (a restart after ten seconds) are dropped.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters import get_adapter
from ..adapters.base import PacketError, TelemetryFrame
from ..config import Config
from ..net.udp import UdpListener
from .capture import CaptureWriter

log = logging.getLogger(__name__)

EventFn = Callable[[dict], None]


@dataclass
class RaceDetector:
    """Frames in, "start" / "end" out. Pure, so it is tested without a socket."""

    start_frames: int = 15
    end_s: float = 3.0
    gap_s: float = 5.0
    in_race: bool = False
    _hits: int = 0
    _last_racing_t: float | None = None
    _last_packet_t: float | None = None

    @staticmethod
    def racing(frame: TelemetryFrame) -> bool:
        """In an event: IsRaceOn and a race position. Free roam keeps IsRaceOn
        but reports position 0; menus send IsRaceOn 0."""
        return bool(frame.race_on) and int(frame.race_position) >= 1

    def update(self, frame: TelemetryFrame, t: float) -> str | None:
        self._last_packet_t = t
        if self.racing(frame):
            self._last_racing_t = t
            if not self.in_race:
                self._hits += 1
                if self._hits >= self.start_frames:
                    self.in_race = True
                    self._hits = 0
                    return "start"
            return None
        self._hits = 0
        if self.in_race and self._last_racing_t is not None and t - self._last_racing_t >= self.end_s:
            self.in_race = False
            return "end"
        return None

    def tick(self, t: float) -> str | None:
        """No packet arrived: a long silence (loading screen) ends a race too."""
        if self.in_race and self._last_packet_t is not None and t - self._last_packet_t >= self.gap_s:
            self.in_race = False
            self._hits = 0
            return "end"
        return None


@dataclass
class SessionResult:
    races: list[Path] = field(default_factory=list)
    discarded: int = 0
    packets: int = 0
    seconds: float = 0.0


def race_filename(directory: Path, when: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    path = directory / f"race-{stamp}.fzr"
    n = 2
    while path.exists():
        path = directory / f"race-{stamp}-{n}.fzr"
        n += 1
    return path


def session_record(
    cfg: Config,
    directory: Path | str,
    *,
    on_event: EventFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    status_interval_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
) -> SessionResult:
    """Listen until ``should_stop()``; write one capture per race.

    Events: ``session_started``, ``race_started`` (race, path),
    ``race_saved`` (path, packets, seconds, races), ``race_discarded``
    (seconds), throttled ``status`` (racing, races, packets, speed_kmh,
    idle), ``done`` (races, discarded).
    """
    adapter = get_adapter(cfg.get("telemetry.adapter"))
    emit = on_event or (lambda e: None)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    detector = RaceDetector(
        start_frames=int(cfg.get("capture.auto.start_frames", 15)),
        end_s=float(cfg.get("capture.auto.end_s", 3.0)),
        gap_s=float(cfg.get("capture.auto.gap_s", 5.0)),
    )
    preroll_s = float(cfg.get("capture.auto.preroll_s", 2.0))
    min_seconds = float(cfg.get("capture.auto.min_seconds", 20.0))
    flush_s = cfg.get("capture.flush_interval_s")
    header = {
        "adapter": adapter.name,
        "expected_packet_size": adapter.packet_size,
        "port": cfg.get("telemetry.port"),
        "note": "auto-recorded race",
        "auto": True,
        "codriver_version": __import__("codriver").__version__,
    }
    result = SessionResult()
    preroll: deque[tuple[int, bytes]] = deque()
    writer: CaptureWriter | None = None
    race_path: Path | None = None
    race_no = 0
    race_start_ns = 0
    start_ns: int | None = None
    last_ns: int | None = None
    last_status = 0.0

    def close_race(now_ns: int) -> None:
        nonlocal writer, race_path
        if writer is None:
            return
        writer.close()
        seconds = (now_ns - race_start_ns) / 1e9
        packets = writer.count
        path = race_path
        writer, race_path = None, None
        if seconds < min_seconds or path is None:
            if path is not None:
                path.unlink(missing_ok=True)
            result.discarded += 1
            emit({"kind": "race_discarded", "seconds": round(seconds, 1)})
            return
        result.races.append(path)
        emit({"kind": "race_saved", "path": str(path), "packets": packets,
              "seconds": round(seconds, 1), "races": len(result.races)})

    emit({"kind": "session_started", "port": cfg.get("telemetry.port"), "dir": str(directory)})
    with UdpListener(
        host=cfg.get("telemetry.bind_host"),
        port=cfg.get("telemetry.port"),
        rcvbuf=cfg.get("telemetry.socket_rcvbuf_bytes"),
        timeout_s=cfg.get("telemetry.socket_timeout_s"),
    ) as listener:
        try:
            while not (should_stop is not None and should_stop()):
                got = listener.recv()
                now = clock()
                if got is None:
                    if detector.tick(now) == "end" and last_ns is not None:
                        close_race(last_ns)
                    if now - last_status >= status_interval_s:
                        last_status = now
                        emit({"kind": "status", "idle": True, "racing": detector.in_race,
                              "races": len(result.races), "packets": writer.count if writer else 0})
                    continue
                data, t_ns = got
                if start_ns is None:
                    start_ns = t_ns
                last_ns = t_ns
                result.packets += 1
                preroll.append((t_ns, data))
                while preroll and (t_ns - preroll[0][0]) / 1e9 > preroll_s:
                    preroll.popleft()
                try:
                    frame = adapter.parse(data, (t_ns - start_ns) / 1e9)
                except PacketError:
                    frame = None
                if frame is not None:
                    change = detector.update(frame, now)
                    if change == "start":
                        race_no += 1
                        race_path = race_filename(directory)
                        writer = CaptureWriter(race_path, header={**header, "race_index": race_no},
                                               flush_interval_s=flush_s).open()
                        for p_ns, p_data in preroll:   # the countdown, and this datagram
                            writer.add(p_data, p_ns)
                        race_start_ns = t_ns
                        emit({"kind": "race_started", "race": race_no, "path": str(race_path)})
                        continue
                    if change == "end":
                        close_race(t_ns)
                if writer is not None:
                    writer.add(data, t_ns)
                if now - last_status >= status_interval_s:
                    last_status = now
                    emit({"kind": "status", "idle": False, "racing": detector.in_race,
                          "races": len(result.races), "packets": writer.count if writer else 0,
                          "speed_kmh": frame.speed_kmh if frame else 0.0})
        except KeyboardInterrupt:
            pass
        finally:
            if writer is not None and last_ns is not None:
                close_race(last_ns)
    result.seconds = (last_ns - start_ns) / 1e9 if start_ns is not None and last_ns is not None else 0.0
    emit({"kind": "done", "races": [str(p) for p in result.races], "discarded": result.discarded,
          "packets": result.packets})
    return result
