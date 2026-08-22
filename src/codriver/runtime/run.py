"""The live co-driver loop: telemetry in, speech out.

Wires the pieces together and otherwise stays out of their way:

    UDP -> adapter -> Locator -> Scheduler -> ClipBank -> Player
                         ^                       ^
                         +------- config --------+   (hot-reloaded)

The config is polled every frame and pushed into the live objects, so editing
``config/local.yaml`` while driving changes lead times, search windows and
beep lengths within half a second. That loop, drive, listen, edit, drive --
is the entire tuning methodology; this file exists to serve it.

Works identically against the game and against ``codriver replay``. It cannot
tell the difference, which is the point.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..stage.schema import safe_stem
from ..adapters import get_adapter
from ..adapters.base import PacketError
from ..config import Config
from ..net.udp import UdpListener
from ..record.capture import CaptureWriter
from ..stage.line import cumulative_distance
from ..stage.schema import Stage
from ..voice.pack import load_configured_bank
from .locate import Locator, StageIndex, TrackState
from .player import BeepBank, Player, make_player
from .scheduler import Scheduler

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    frames: int = 0
    fixes: int = 0
    suspends: int = 0
    rewinds: int = 0
    reacquires: int = 0
    spoken: int = 0
    dropped: int = 0
    recorded_to: Path | None = None
    recorded_packets: int = 0

    def summary(self) -> str:
        return (
            f"{self.frames} frames, {self.fixes} tracked; "
            f"{self.spoken} notes spoken, {self.dropped} dropped; "
            f"{self.suspends} suspend(s), {self.rewinds} rewind(s), "
            f"{self.reacquires} reacquire(s)"
        )


@dataclass
class _Hud:
    """One live status line. Rewritten in place; play events get whole lines."""

    enabled: bool = True
    min_interval_s: float = 0.2
    _last: float = field(default=0.0, repr=False)

    def status(self, text: str) -> None:
        now = time.monotonic()
        if not self.enabled or now - self._last < self.min_interval_s:
            return
        self._last = now
        print(f"\r{text:<100}", end="", file=sys.stderr, flush=True)

    def event(self, text: str) -> None:
        if self.enabled:
            print(f"\r{text:<100}", file=sys.stderr)


def _apply_config(cfg: Config, locator: Locator, scheduler: Scheduler, bank) -> None:
    """Push the current config into the live objects. Called at start and on
    every hot-reload, this function is what makes tuning-while-driving work.
    ``bank`` is a BeepBank or a WavBank; both take the crossfade."""
    locator.search_back_points = cfg.get("runtime.locate.search_back_points")
    locator.search_forward_points = cfg.get("runtime.locate.search_forward_points")
    locator.lost_distance_m = cfg.get("runtime.locate.lost_distance_m")
    locator.lost_after_packets = cfg.get("runtime.locate.lost_after_packets")
    locator.suspend_after_s = cfg.get("runtime.gaps.suspend_after_s")
    locator.rewind_jump_m = cfg.get("runtime.gaps.rewind_jump_m")

    scheduler.reaction_buffer_s = cfg.get("runtime.trigger.reaction_buffer_s")
    scheduler.speed_curve_kmh = cfg.get("runtime.trigger.speed_curve_kmh")
    scheduler.speed_curve_mult = cfg.get("runtime.trigger.speed_curve_mult")
    scheduler.min_lead_m = cfg.get("runtime.trigger.min_lead_m")
    scheduler.max_lead_m = cfg.get("runtime.trigger.max_lead_m")
    scheduler.drop_if_later_than_s = cfg.get("runtime.queue.drop_if_later_than_s")

    # The bank may be beeps or a loaded voice pack; only beeps have a
    # tunable base clip length.
    if isinstance(bank, BeepBank):
        bank.retune(cfg.get("audio.placeholder_clip_s"))
    bank.crossfade_s = cfg.get("audio.crossfade_ms") / 1000.0


def run_stage(
    stage: Stage,
    cfg: Config,
    silent: bool = False,
    hud: bool = True,
    max_frames: int = 0,
    record_dir: Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunStats:
    """The live loop. With ``record_dir`` every datagram heard is also written
    to a capture there, so each drive becomes material for ``codriver learn``
    -- the game sends it anyway, recording it costs nothing.

    ``on_event`` receives dicts with a ``kind`` of waiting / localised /
    note / status / suspended / jump / done, the same facts the terminal
    HUD prints, for the browser UI. ``should_stop`` is polled every frame.
    """
    emit = on_event or (lambda event: None)
    last_status_emit = 0.0
    if not stage.notes:
        log.warning("stage has no notes; the co-driver will have nothing to say")

    cumulative = cumulative_distance(stage.line)
    index = StageIndex(stage.line, cumulative)
    locator = Locator(index)

    beeps = BeepBank(
        samplerate=cfg.get("audio.samplerate"),
        base_clip_s=cfg.get("audio.placeholder_clip_s"),
        crossfade_s=cfg.get("audio.crossfade_ms") / 1000.0,
    )
    bank = load_configured_bank(cfg, beeps)
    scheduler = Scheduler(notes=list(stage.notes), duration_fn=bank.duration)
    player: Player = make_player(
        samplerate=cfg.get("audio.samplerate"),
        blocksize=cfg.get("audio.blocksize"),
        device=cfg.get("audio.device"),
        gain_db=cfg.get("audio.gain_db"),
        silent=silent,
    )
    _apply_config(cfg, locator, scheduler, bank)
    cfg.on_reload(lambda c: _apply_config(c, locator, scheduler, bank))

    adapter = get_adapter(cfg.get("telemetry.adapter"))
    stats = RunStats()
    display = _Hud(enabled=hud)
    was_tracking = False
    suspended_announced = False
    start_ns: int | None = None

    print(
        f"co-driver ready: {stage.name}, {stage.length_m / 1000:.2f} km, "
        f"{len(stage.notes)} notes. Waiting for telemetry on "
        f"{cfg.get('telemetry.bind_host')}:{cfg.get('telemetry.port')} ...",
        file=sys.stderr,
    )

    recorder: CaptureWriter | None = None
    if record_dir is not None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        recorder = CaptureWriter(
            Path(record_dir) / f"{safe_stem(stage.name)}_{stamp}.fzr",
            header={
                "adapter": cfg.get("telemetry.adapter"),
                "stage": stage.name,
                "kind": "run",
                "port": cfg.get("telemetry.port"),
            },
            flush_interval_s=cfg.get("capture.flush_interval_s"),
        ).open()

    voice_name = None if isinstance(bank, BeepBank) else getattr(bank, "name", "voice")
    if voice_name is None:
        log.warning("no voice pack loaded: you will hear placeholder beeps")
    emit({
        "kind": "waiting",
        "stage": stage.name,
        "notes": len(stage.notes),
        "length_m": stage.length_m,
        "port": cfg.get("telemetry.port"),
        "voice": voice_name,
    })

    with UdpListener(
        host=cfg.get("telemetry.bind_host"),
        port=cfg.get("telemetry.port"),
        rcvbuf=cfg.get("telemetry.socket_rcvbuf_bytes"),
        timeout_s=cfg.get("telemetry.socket_timeout_s"),
    ) as listener:
        try:
            while True:
                if should_stop is not None and should_stop():
                    break
                cfg.poll()
                got = listener.recv()
                now = time.monotonic()

                if got is None:
                    # The game went quiet: pause, rewind, finish, menus.
                    if was_tracking and not suspended_announced:
                        stats.suspends += 1
                        suspended_announced = True
                        scheduler.flush()
                        player.stop_all()
                        display.event("-- stream suspended (pause/rewind/finish?)")
                        emit({"kind": "suspended"})
                    continue

                data, t_ns = got
                if start_ns is None:
                    start_ns = t_ns
                if recorder is not None:
                    recorder.add(data, t_ns)
                try:
                    frame = adapter.parse(data, (t_ns - start_ns) / 1e9)
                except PacketError:
                    continue
                stats.frames += 1
                if max_frames and stats.frames >= max_frames:
                    break
                if not frame.race_on:
                    continue
                suspended_announced = False

                fix = locator.update(frame.x, frame.z, frame.t)

                if fix.jumped:
                    stats.rewinds += 1
                    scheduler.flush()
                    player.stop_all()
                    display.event("-- position jump: rewound or restarted, re-localising")
                    emit({"kind": "jump"})

                if fix.ok:
                    stats.fixes += 1
                    if not was_tracking or fix.jumped:
                        stats.reacquires += 1
                        scheduler.relocate(fix.along_m)
                        display.event(
                            f"-- localised at {fix.along_m / 1000:.3f} km "
                            f"({fix.off_line_m:.1f} m off line)"
                        )
                        emit({"kind": "localised", "along_m": fix.along_m,
                              "off_m": fix.off_line_m})
                    was_tracking = True

                    for event in scheduler.tick(fix.along_m, frame.speed, now):
                        player.play(bank.render(event.note.tokens))
                        emit({
                            "kind": "note",
                            "text": event.note.text,
                            "at_m": event.note.at_m,
                            "lead_m": event.lead_m,
                            "duration_s": event.duration_s,
                            "severity": event.note.severity,
                        })
                        display.event(
                            f">> {event.note.text:<40} "
                            f"[{event.note.at_m / 1000:6.3f} km, "
                            f"lead {event.lead_m:3.0f} m, "
                            f"{event.duration_s:.2f}s]"
                        )
                else:
                    was_tracking = was_tracking and fix.state is TrackState.TRACKING

                nxt = scheduler.next_note
                if now - last_status_emit >= 0.2:
                    last_status_emit = now
                    emit({
                        "kind": "status",
                        "state": fix.state.value,
                        "along_m": fix.along_m,
                        "speed_kmh": frame.speed_kmh,
                        "off_m": fix.off_line_m,
                        "next": nxt.text if nxt else None,
                        "next_at_m": nxt.at_m if nxt else None,
                        "spoken": scheduler.spoken,
                        "dropped": scheduler.dropped,
                    })
                display.status(
                    f"[{fix.state.value:^9}] {fix.along_m / 1000:7.3f} km  "
                    f"{frame.speed_kmh:5.1f} km/h  off {fix.off_line_m:4.1f} m  "
                    + (
                        f"next: {nxt.text} @{nxt.at_m / 1000:.3f} km"
                        if nxt
                        else "no notes remaining"
                    )
                )
        except KeyboardInterrupt:
            pass
        finally:
            player.close()
            if recorder is not None:
                recorder.close()
                if recorder.count == 0:
                    recorder.path.unlink(missing_ok=True)
                else:
                    stats.recorded_to = recorder.path
                    stats.recorded_packets = recorder.count

    stats.spoken = scheduler.spoken
    stats.dropped = scheduler.dropped
    print(f"\n{stats.summary()}", file=sys.stderr)
    if stats.recorded_to is not None:
        print(
            f"recorded {stats.recorded_packets} packets to {stats.recorded_to}\n"
            f"  fold it into the stage:  python -m codriver learn <stage.json>",
            file=sys.stderr,
        )
    emit({
        "kind": "done",
        "summary": stats.summary(),
        "frames": stats.frames,
        "fixes": stats.fixes,
        "spoken": stats.spoken,
        "dropped": stats.dropped,
        "recorded_to": str(stats.recorded_to) if stats.recorded_to else None,
        "recorded_packets": stats.recorded_packets,
    })
    return stats
