"""Replay a capture back out over UDP at its original timing.

This is the single largest productivity win in the project. Once it works, every iteration on the note generator, the
classifier thresholds, the trigger timing and the audio assembly happens
against a file instead of against the game.

Pacing comes from **arrival time**, not from ``TimestampMS``.
The documented advice is to derive timing from the packet timestamp, which is
right for runtime dt and wrong here: the game emits nothing at all during
pauses, rewinds and after the finish line, so there is no packet from which
to read a timestamp across exactly the gaps that the Data Out spec calls meaningful
signal, and the field can overflow to zero mid-stage. The payload is
forwarded byte-for-byte, ``TimestampMS`` included and untouched.

Timing on Windows
-----------------
The default timer period is ~15.6 ms, which is coarser than a 60 Hz frame at
16.7 ms: naive ``time.sleep`` would make replay jitter larger than the signal
being tuned. Two things fix that: ``timeBeginPeriod(1)`` for the process, and
a sleep-then-spin scheduler against absolute deadlines (so error never
accumulates). Achieved timing is measured and reported, if replay is not
faithful you should be able to see that, not assume it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from ..net.udp import UdpSender
from .capture import CaptureReader

log = logging.getLogger(__name__)

NS = 1_000_000_000


@contextlib.contextmanager
def high_resolution_timer() -> Iterator[bool]:
    """Request a 1 ms system timer period on Windows. No-op elsewhere."""
    period_set = False
    winmm = None
    try:
        import ctypes

        winmm = ctypes.WinDLL("winmm")  # type: ignore[attr-defined]
        period_set = winmm.timeBeginPeriod(1) == 0
        if not period_set:
            log.debug("timeBeginPeriod(1) refused; replay jitter may be ~15 ms")
    except (ImportError, AttributeError, OSError):
        pass  # not Windows, or winmm unavailable
    try:
        yield period_set
    finally:
        if period_set and winmm is not None:
            winmm.timeEndPeriod(1)


def sleep_until(target_ns: int, spin_margin_ns: int) -> None:
    """Block until ``perf_counter_ns() >= target_ns``.

    Sleeps until within ``spin_margin_ns`` of the deadline, then busy-waits.
    The spin is what buys sub-millisecond accuracy; the sleep is what keeps
    it from burning a core.
    """
    while True:
        remaining = target_ns - time.perf_counter_ns()
        if remaining <= 0:
            return
        if remaining > spin_margin_ns:
            time.sleep((remaining - spin_margin_ns) / NS)
        else:
            while time.perf_counter_ns() < target_ns:
                pass
            return


def build_schedule(
    times_ns: Sequence[int],
    speed: float = 1.0,
    max_gap_s: float | None = None,
) -> list[int]:
    """Turn recorded arrival times into playback offsets from t=0.

    ``speed`` compresses or stretches everything uniformly. ``max_gap_s``
    clamps only the long silences, useful when you want to skip past two
    minutes of menu, and wrong whenever you are testing how the runtime
    handles a pause, which is why it is off by default.
    """
    if speed <= 0:
        raise ValueError("replay speed must be positive")
    max_gap_ns = int(max_gap_s * NS) if max_gap_s is not None else None

    out: list[int] = []
    acc = 0
    prev: int | None = None
    for t in times_ns:
        if prev is not None:
            delta = t - prev
            if max_gap_ns is not None and delta > max_gap_ns:
                delta = max_gap_ns
            acc += delta
        out.append(int(acc / speed))
        prev = t
    return out


@dataclass
class ReplayStats:
    """Whether the replay actually hit its deadlines."""

    sent: int = 0
    loops: int = 0
    wall_s: float = 0.0
    late_total_ns: int = 0
    late_max_ns: int = 0
    late_over_2ms: int = 0
    timer_boosted: bool = False

    @property
    def late_mean_ms(self) -> float:
        return (self.late_total_ns / self.sent) / 1e6 if self.sent else 0.0

    @property
    def late_max_ms(self) -> float:
        return self.late_max_ns / 1e6

    def summary(self) -> str:
        return (
            f"{self.sent} packets in {self.wall_s:.2f}s "
            f"({self.loops} loop(s)); scheduling error "
            f"mean {self.late_mean_ms:.2f} ms, max {self.late_max_ms:.2f} ms, "
            f"{self.late_over_2ms} over 2 ms"
            + ("" if self.timer_boosted else "; 1 ms timer NOT active")
        )


ProgressCb = Callable[[int, int, float], None]
"""(index, total, elapsed_s), called every so often during replay."""


def replay_records(
    records: Sequence[tuple[int, bytes]],
    host: str = "127.0.0.1",
    port: int = 5400,
    speed: float = 1.0,
    loop: bool = False,
    max_gap_s: float | None = None,
    spin_margin_s: float = 0.0015,
    progress: ProgressCb | None = None,
    progress_every: int = 60,
    should_stop: Callable[[], bool] | None = None,
) -> ReplayStats:
    """Pump ``(t_ns, payload)`` records out over UDP on their original timing."""
    if not records:
        raise ValueError("nothing to replay: the capture has no records")

    schedule = build_schedule([t for t, _ in records], speed=speed, max_gap_s=max_gap_s)
    payloads = [p for _, p in records]
    spin_margin_ns = int(spin_margin_s * NS)
    stats = ReplayStats()

    with high_resolution_timer() as boosted, UdpSender(host, port) as sender:
        stats.timer_boosted = boosted
        run_start = time.perf_counter_ns()
        epoch = run_start
        # One extra tick after the last packet, so a loop restarts on beat
        # rather than immediately.
        loop_length = schedule[-1] + (schedule[-1] // len(schedule) if schedule else 0)

        while True:
            stats.loops += 1
            for i, offset in enumerate(schedule):
                if should_stop is not None and should_stop():
                    stats.wall_s = (time.perf_counter_ns() - run_start) / NS
                    return stats
                target = epoch + offset
                sleep_until(target, spin_margin_ns)
                sender.send(payloads[i])

                late = time.perf_counter_ns() - target
                if late > 0:
                    stats.late_total_ns += late
                    stats.late_max_ns = max(stats.late_max_ns, late)
                    if late > 2_000_000:
                        stats.late_over_2ms += 1
                stats.sent += 1

                if progress is not None and i % progress_every == 0:
                    progress(i, len(schedule), (time.perf_counter_ns() - run_start) / NS)

            if not loop:
                break
            epoch += loop_length

        stats.wall_s = (time.perf_counter_ns() - run_start) / NS

    return stats


def replay_file(
    path: Path | str,
    host: str = "127.0.0.1",
    port: int = 5400,
    **kwargs: object,
) -> ReplayStats:
    """Load a ``.fzr`` capture and replay it. See ``replay_records``."""
    with CaptureReader(Path(path)) as reader:
        records = list(reader)
    log.info("replaying %d packets from %s to %s:%d", len(records), path, host, port)
    return replay_records(records, host=host, port=port, **kwargs)  # type: ignore[arg-type]
