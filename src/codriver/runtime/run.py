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

``CoDriver`` is the co-driver for one stage, fed datagrams from outside.
``run_stage`` wraps it in a socket loop for ``codriver run``; the evening
mode (``runtime/auto.py``) feeds it from its own listener once a race has
been recognised and matched to a stage.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..adapters import get_adapter
from ..adapters.base import PacketError
from ..config import Config
from ..net.udp import UdpListener
from ..record.capture import CaptureWriter
from ..stage.line import cumulative_distance
from ..stage.schema import Stage, safe_stem
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


def note_brief(note) -> dict[str, Any]:
    """A note as the event stream carries it: enough to draw, nothing more."""
    return {
        "text": note.text,
        "tokens": list(note.tokens),
        "severity": note.severity,
        "direction": note.direction,
        "kind": note.kind,
        "at_m": note.at_m,
    }


class CoDriver:
    """The co-driver for one stage. Hand it every datagram (``on_datagram``),
    tell it when nothing arrives (``on_idle``), and ``finish`` when the drive
    is over. It owns the locator, the scheduler, the audio and, if asked, a
    recorder of everything it heard; it owns no socket.

    ``on_event`` receives dicts with a ``kind`` of waiting / localised / note
    / status / suspended / jump / done, the same facts the terminal HUD
    prints, for the browser UI and the overlay.
    """

    def __init__(
        self,
        stage: Stage,
        cfg: Config,
        *,
        silent: bool = False,
        hud: bool = True,
        record_dir: Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.stage = stage
        self.cfg = cfg
        self.emit = on_event or (lambda event: None)
        if not stage.notes:
            log.warning("stage has no notes; the co-driver will have nothing to say")

        cumulative = cumulative_distance(stage.line)
        self.index = StageIndex(stage.line, cumulative)
        self.locator = Locator(self.index)
        beeps = BeepBank(
            samplerate=cfg.get("audio.samplerate"),
            base_clip_s=cfg.get("audio.placeholder_clip_s"),
            crossfade_s=cfg.get("audio.crossfade_ms") / 1000.0,
        )
        self.bank = load_configured_bank(cfg, beeps)
        self.scheduler = Scheduler(notes=list(stage.notes), duration_fn=self.bank.duration)
        self.player: Player = make_player(
            samplerate=cfg.get("audio.samplerate"),
            blocksize=cfg.get("audio.blocksize"),
            device=cfg.get("audio.device"),
            gain_db=cfg.get("audio.gain_db"),
            silent=silent,
        )
        _apply_config(cfg, self.locator, self.scheduler, self.bank)
        self._reload_cb = lambda c: _apply_config(c, self.locator, self.scheduler, self.bank)
        cfg.on_reload(self._reload_cb)

        self.adapter = get_adapter(cfg.get("telemetry.adapter"))
        self.stats = RunStats()
        self.display = _Hud(enabled=hud)
        self._was_tracking = False
        self._suspended_announced = False
        self._start_ns: int | None = None
        self._last_status_emit = 0.0
        self._finished = False

        self.recorder: CaptureWriter | None = None
        if record_dir is not None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            self.recorder = CaptureWriter(
                Path(record_dir) / f"{safe_stem(stage.name)}_{stamp}.fzr",
                header={
                    "adapter": cfg.get("telemetry.adapter"),
                    "stage": stage.name,
                    "kind": "run",
                    "port": cfg.get("telemetry.port"),
                },
                flush_interval_s=cfg.get("capture.flush_interval_s"),
            ).open()

        self.voice_name = None if isinstance(self.bank, BeepBank) else getattr(self.bank, "name", "voice")
        if self.voice_name is None:
            log.warning("no voice pack loaded: you will hear placeholder beeps")
        self.emit({
            "kind": "waiting",
            "stage": stage.name,
            "notes": len(stage.notes),
            "length_m": stage.length_m,
            "port": cfg.get("telemetry.port"),
            "voice": self.voice_name,
        })

    # -- feeding ---------------------------------------------------------------

    def on_idle(self, now: float) -> None:
        """No datagram arrived: the game went quiet (pause, rewind, finish, menus)."""
        if self._was_tracking and not self._suspended_announced:
            self.stats.suspends += 1
            self._suspended_announced = True
            self.scheduler.flush()
            self.player.stop_all()
            self.display.event("-- stream suspended (pause/rewind/finish?)")
            self.emit({"kind": "suspended"})

    def on_datagram(self, data: bytes, t_ns: int, now: float) -> None:
        if self._start_ns is None:
            self._start_ns = t_ns
        if self.recorder is not None:
            self.recorder.add(data, t_ns)
        try:
            frame = self.adapter.parse(data, (t_ns - self._start_ns) / 1e9)
        except PacketError:
            return
        self.stats.frames += 1
        if not frame.race_on:
            return
        self._suspended_announced = False
        stats, scheduler, player, bank, display = self.stats, self.scheduler, self.player, self.bank, self.display

        fix = self.locator.update(frame.x, frame.z, frame.t)
        if fix.jumped:
            stats.rewinds += 1
            scheduler.flush()
            player.stop_all()
            display.event("-- position jump: rewound or restarted, re-localising")
            self.emit({"kind": "jump"})

        if fix.ok:
            stats.fixes += 1
            if not self._was_tracking or fix.jumped:
                stats.reacquires += 1
                scheduler.relocate(fix.along_m)
                display.event(
                    f"-- localised at {fix.along_m / 1000:.3f} km "
                    f"({fix.off_line_m:.1f} m off line)"
                )
                self.emit({"kind": "localised", "along_m": fix.along_m, "off_m": fix.off_line_m})
            self._was_tracking = True

            for event in scheduler.tick(fix.along_m, frame.speed, now):
                player.play(bank.render(event.note.tokens))
                self.emit({
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
            self._was_tracking = self._was_tracking and fix.state is TrackState.TRACKING

        nxt = scheduler.next_note
        if now - self._last_status_emit >= 0.2:
            self._last_status_emit = now
            self.emit({
                "kind": "status",
                "state": fix.state.value,
                "along_m": fix.along_m,
                "speed_kmh": frame.speed_kmh,
                "off_m": fix.off_line_m,
                "next": nxt.text if nxt else None,
                "next_at_m": nxt.at_m if nxt else None,
                "upcoming": [note_brief(n) for n in scheduler.upcoming(2)],
                "spoken": scheduler.spoken,
                "dropped": scheduler.dropped,
            })
        display.status(
            f"[{fix.state.value:^9}] {fix.along_m / 1000:7.3f} km  "
            f"{frame.speed_kmh:5.1f} km/h  off {fix.off_line_m:4.1f} m  "
            + (f"next: {nxt.text} @{nxt.at_m / 1000:.3f} km" if nxt else "no notes remaining")
        )

    # -- the end -----------------------------------------------------------------

    def finish(self) -> RunStats:
        """Close audio and recorder, report. Safe to call twice."""
        if self._finished:
            return self.stats
        self._finished = True
        stats = self.stats
        self.player.close()
        if self.recorder is not None:
            self.recorder.close()
            if self.recorder.count == 0:
                self.recorder.path.unlink(missing_ok=True)
            else:
                stats.recorded_to = self.recorder.path
                stats.recorded_packets = self.recorder.count
        stats.spoken = self.scheduler.spoken
        stats.dropped = self.scheduler.dropped
        self.emit({
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
    """The live loop for one stage: a socket feeding a ``CoDriver``. With
    ``record_dir`` every datagram heard is also written to a capture there, so
    each drive becomes material for ``codriver learn`` -- the game sends it
    anyway, recording it costs nothing. ``should_stop`` is polled every frame.
    """
    co = CoDriver(stage, cfg, silent=silent, hud=hud, record_dir=record_dir, on_event=on_event)
    print(
        f"co-driver ready: {stage.name}, {stage.length_m / 1000:.2f} km, "
        f"{len(stage.notes)} notes. Waiting for telemetry on "
        f"{cfg.get('telemetry.bind_host')}:{cfg.get('telemetry.port')} ...",
        file=sys.stderr,
    )
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
                    co.on_idle(now)
                    continue
                data, t_ns = got
                co.on_datagram(data, t_ns, now)
                if max_frames and co.stats.frames >= max_frames:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            stats = co.finish()

    print(f"\n{stats.summary()}", file=sys.stderr)
    if stats.recorded_to is not None:
        print(
            f"recorded {stats.recorded_packets} packets to {stats.recorded_to}\n"
            f"  fold it into the stage:  python -m codriver learn <stage.json>",
            file=sys.stderr,
        )
    return stats
