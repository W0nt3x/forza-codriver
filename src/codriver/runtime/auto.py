"""Evening mode: start once, then codriver does the rest on its own.

One listener for the whole session. The race detector (record/session.py)
says when an event starts; the matcher compares where the car is with the
first metres of every stage on disk; a match starts a ``CoDriver`` for that
stage, which calls the corners exactly as ``codriver run`` would; no match
means the race is only recorded, for building later. Either way every race
is saved: a matched one as a run recording next to the stage's other runs
(Learn material), an unmatched one as ``race-<time>.fzr`` in recordings/.

Nothing here is new telemetry. It is the capture, the matcher and the
co-driver, wired so that the driver never touches the keyboard between races.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters import get_adapter
from ..adapters.base import PacketError
from ..config import Config
from ..net.udp import UdpListener
from ..record.capture import CaptureWriter
from ..record.session import RaceDetector, race_filename
from ..stage.line import cumulative_distance
from ..stage.schema import Stage, safe_stem
from .run import CoDriver

log = logging.getLogger(__name__)

EventFn = Callable[[dict], None]


@dataclass
class StageMatcher:
    """Which stage starts where the car is. The first ``head_m`` metres of
    every stage's line against the car's position; nearest wins if it is
    within ``radius_m``."""

    stages: list[Stage]
    radius_m: float = 40.0
    head_m: float = 120.0
    _heads: list[tuple[Stage, list[tuple[float, float]]]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        for st in self.stages:
            if len(st.line) < 2:
                continue
            cum = cumulative_distance(st.line)
            pts = [(p.x, p.z) for p, c in zip(st.line, cum) if c <= self.head_m]
            self._heads.append((st, pts or [(st.line[0].x, st.line[0].z)]))

    def match(self, x: float, z: float) -> tuple[Stage, float] | None:
        best: tuple[Stage, float] | None = None
        for st, pts in self._heads:
            d = min(math.hypot(px - x, pz - z) for px, pz in pts)
            if d <= self.radius_m and (best is None or d < best[1]):
                best = (st, d)
        return best


def load_stages(stages_dir: Path | str) -> list[Stage]:
    """Every loadable stage in the folder; a broken file is skipped and logged."""
    from ..stage.schema import StageError, load

    out: list[Stage] = []
    for path in sorted(Path(stages_dir).glob("*.json")):
        try:
            out.append(load(path))
        except (StageError, OSError, ValueError) as exc:
            log.warning("auto: skipping %s: %s", path.name, exc)
    return out


@dataclass
class AutoResult:
    races: int = 0
    matched: int = 0
    saved: list[Path] = field(default_factory=list)
    discarded: int = 0
    packets: int = 0


def session_auto(
    cfg: Config,
    stages: list[Stage],
    recordings_dir: Path | str,
    runs_dir: Path | str,
    *,
    silent: bool = False,
    hud: bool = False,
    on_event: EventFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    status_interval_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
) -> AutoResult:
    """Listen until ``should_stop()``. Per race: detect, match, call, record.

    Events, besides everything a ``CoDriver`` emits while a stage is matched:
    ``auto_started`` (stages, port), ``race_started`` (race), ``auto_matched``
    (stage, distance_m), ``auto_unmatched``, ``race_saved`` (path, stage,
    seconds, races), ``race_discarded`` (seconds), throttled ``auto_status``
    (racing, stage, races, packets, idle), ``auto_done`` (races, matched,
    discarded).
    """
    adapter = get_adapter(cfg.get("telemetry.adapter"))
    emit = on_event or (lambda e: None)
    recordings_dir, runs_dir = Path(recordings_dir), Path(runs_dir)
    recordings_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    detector = RaceDetector(
        start_frames=int(cfg.get("capture.auto.start_frames", 15)),
        end_s=float(cfg.get("capture.auto.end_s", 3.0)),
        gap_s=float(cfg.get("capture.auto.gap_s", 5.0)),
    )
    preroll_s = float(cfg.get("capture.auto.preroll_s", 2.0))
    min_seconds = float(cfg.get("capture.auto.min_seconds", 20.0))
    matcher = StageMatcher(
        stages,
        radius_m=float(cfg.get("runtime.auto.match_radius_m", 40.0)),
        head_m=float(cfg.get("runtime.auto.match_head_m", 120.0)),
    )
    match_window_s = float(cfg.get("runtime.auto.match_window_s", 15.0))
    flush_s = cfg.get("capture.flush_interval_s")
    header = {
        "adapter": adapter.name, "expected_packet_size": adapter.packet_size,
        "port": cfg.get("telemetry.port"), "note": "auto", "auto": True,
        "codriver_version": __import__("codriver").__version__,
    }

    result = AutoResult()
    preroll: deque[tuple[int, bytes]] = deque()
    writer: CaptureWriter | None = None
    race_path: Path | None = None
    race_start_ns = 0
    co: CoDriver | None = None
    matched: Stage | None = None
    match_deadline = 0.0
    last_match_try = 0.0
    unmatched_said = False
    start_ns: int | None = None
    last_ns: int | None = None
    last_status = 0.0

    def try_match(x: float, z: float, now: float) -> None:
        nonlocal co, matched, last_match_try, unmatched_said
        last_match_try = now
        hit = matcher.match(x, z)
        if hit is None:
            if not unmatched_said:
                unmatched_said = True
                emit({"kind": "auto_unmatched", "stages": len(stages)})
            return
        matched, dist = hit
        result.matched += 1
        co = CoDriver(matched, cfg, silent=silent, hud=hud, record_dir=None, on_event=emit)
        for p_ns, p_data in list(preroll):
            co.on_datagram(p_data, p_ns, now)
        emit({"kind": "auto_matched", "stage": matched.name, "distance_m": round(dist, 1)})

    def close_race(now_ns: int) -> None:
        nonlocal writer, race_path, co, matched
        if co is not None:
            co.finish()
            co = None
        if writer is None:
            matched = None
            return
        writer.close()
        seconds = (now_ns - race_start_ns) / 1e9
        path, writer, race_path = race_path, None, None
        if path is None:
            matched = None
            return
        if seconds < min_seconds:
            path.unlink(missing_ok=True)
            result.discarded += 1
            emit({"kind": "race_discarded", "seconds": round(seconds, 1)})
            matched = None
            return
        if matched is not None:
            # a run of a known stage: where Learn looks for it
            dest = runs_dir / f"{safe_stem(matched.name)}_{time.strftime('%Y%m%d_%H%M%S')}.fzr"
            try:
                path.replace(dest)
                path = dest
            except OSError as exc:
                log.warning("auto: could not move %s to runs: %s", path.name, exc)
        result.saved.append(path)
        emit({"kind": "race_saved", "path": str(path), "stage": matched.name if matched else None,
              "seconds": round(seconds, 1), "races": len(result.saved)})
        matched = None

    emit({"kind": "auto_started", "stages": [s.name for s in stages], "port": cfg.get("telemetry.port")})
    with UdpListener(
        host=cfg.get("telemetry.bind_host"),
        port=cfg.get("telemetry.port"),
        rcvbuf=cfg.get("telemetry.socket_rcvbuf_bytes"),
        timeout_s=cfg.get("telemetry.socket_timeout_s"),
    ) as listener:
        try:
            while not (should_stop is not None and should_stop()):
                cfg.poll()
                got = listener.recv()
                now = clock()
                if got is None:
                    if detector.tick(now) == "end" and last_ns is not None:
                        close_race(last_ns)
                    elif co is not None:
                        co.on_idle(now)
                    if now - last_status >= status_interval_s:
                        last_status = now
                        emit({"kind": "auto_status", "idle": True, "racing": detector.in_race,
                              "stage": matched.name if matched else None, "races": len(result.saved),
                              "packets": writer.count if writer else 0})
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
                        result.races += 1
                        race_path = race_filename(recordings_dir)
                        writer = CaptureWriter(race_path, header={**header, "race_index": result.races},
                                               flush_interval_s=flush_s).open()
                        for p_ns, p_data in preroll:
                            writer.add(p_data, p_ns)
                        race_start_ns = t_ns
                        unmatched_said = False
                        match_deadline = now + match_window_s
                        emit({"kind": "race_started", "race": result.races, "path": str(race_path)})
                        try_match(frame.x, frame.z, now)
                        continue
                    if change == "end":
                        close_race(t_ns)
                if writer is not None:
                    writer.add(data, t_ns)
                    if frame is not None:
                        if co is None and now < match_deadline and now - last_match_try >= 1.0:
                            try_match(frame.x, frame.z, now)
                        if co is not None:
                            co.on_datagram(data, t_ns, now)
                if now - last_status >= status_interval_s:
                    last_status = now
                    emit({"kind": "auto_status", "idle": False, "racing": detector.in_race,
                          "stage": matched.name if matched else None, "races": len(result.saved),
                          "packets": writer.count if writer else 0,
                          "speed_kmh": frame.speed_kmh if frame else 0.0})
        except KeyboardInterrupt:
            pass
        finally:
            if last_ns is not None:
                close_race(last_ns)
            elif co is not None:
                co.finish()
    emit({"kind": "auto_done", "races": result.races, "matched": result.matched,
          "saved": [str(p) for p in result.saved], "discarded": result.discarded})
    return result
