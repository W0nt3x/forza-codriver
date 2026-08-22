"""Runtime end to end.

The simulation test is the acceptance test for the runtime: a car re-drives a
built stage at a speed the recon lap never used, and every note must be
spoken, in order, finishing with its reaction buffer intact. That the recon
speed does not matter is the whole design, the stage is geometry, the
trigger is live speed.

The socket test then proves the same loop wired through real UDP, an actual
replay and the run loop, silently and briefly.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from codriver.config import Config, find_config_dir
from codriver.record.synth import SynthSpec, write_synth
from codriver.runtime.locate import Locator, StageIndex
from codriver.runtime.player import BeepBank
from codriver.runtime.scheduler import Scheduler
from codriver.stage.build import build_stage
from codriver.stage.line import cumulative_distance


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    path = tmp / "course.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="slalom",
            duration_s=60.0,
            speed_mps=20.0,
            size_m=70.0,
            pause_at_s=None,
            jump_at_s=None,
        ),
    )
    cfg = Config.load(find_config_dir())
    stage, _ = build_stage(path, cfg, name="e2e")
    return stage, cfg, path


def test_redriving_the_stage_speaks_every_note_in_time(built):
    """Drive the stage line at 30 m/s, half again faster than the recon lap
    -- and hold the scheduler to its own contract with a stopwatch."""
    stage, cfg, _ = built
    assert stage.notes, "the course must generate notes for this test to mean anything"

    cumulative = cumulative_distance(stage.line)
    locator = Locator(StageIndex(stage.line, cumulative))
    bank = BeepBank()
    scheduler = Scheduler(notes=list(stage.notes), duration_fn=bank.duration)
    buffer_s = scheduler.reaction_buffer_s

    speed = 30.0
    dt = 1.0 / 30.0
    spoken: list[tuple[float, float, float]] = []  # (note_at_m, fired_t, duration)
    passed_at: dict[float, float] = {}

    # Walk the actual stage line, not an idealised path: the localisation and
    # the trigger run against the same geometry the game would produce.
    along_target = 0.0
    now = 0.0
    seg = 0
    first = True
    while along_target < stage.length_m - 1.0:
        while seg < len(cumulative) - 1 and cumulative[seg + 1] < along_target:
            seg += 1
        point = stage.line[min(seg, len(stage.line) - 1)]
        fix = locator.update(point.x, point.z, now)
        assert fix.ok
        if first:
            scheduler.relocate(fix.along_m)
            first = False
        for event in scheduler.tick(fix.along_m, speed, now):
            spoken.append((event.note.at_m, now, event.duration_s))
        for n in stage.notes:
            if n.at_m not in passed_at and fix.along_m >= n.at_m:
                passed_at[n.at_m] = now
        along_target += speed * dt
        now += dt

    assert scheduler.dropped == 0, "a clean constant-speed run must drop nothing"
    assert [m for m, _, _ in spoken] == [n.at_m for n in stage.notes], (
        "every note, in stage order"
    )
    # Notes already inside the lead distance when the run starts cannot honour
    # the buffer, no co-driver can finish 1.8 s early for a corner 0.7 s
    # away. They are spoken immediately instead (better late than silent),
    # so the margin contract applies only from beyond the initial lead.
    initial_lead_m = speed * (2.0 + buffer_s) * 1.3
    checked = 0
    for at_m, fired_t, duration in spoken:
        if at_m not in passed_at or at_m < initial_lead_m:
            continue
        checked += 1
        margin = passed_at[at_m] - (fired_t + duration)
        # The fix is quantised to the walked line, so allow modest slack --
        # but a note finishing later than ~70% of the buffer means the
        # trigger maths is broken, not imprecise.
        assert margin > buffer_s * 0.7, (
            f"note at {at_m:.0f} m finished only {margin:.2f}s before the corner"
        )
    assert checked >= len(spoken) - 3, "the exemption must stay an exemption"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_run_loop_speaks_over_real_udp(built):
    from codriver.record.replay import replay_file
    from codriver.runtime.run import run_stage

    stage, cfg, capture = built
    port = _free_port()
    cfg.data["telemetry"]["port"] = port
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"

    replayer = threading.Thread(
        target=lambda: replay_file(
            capture, host="127.0.0.1", port=port, speed=4.0, max_gap_s=0.2
        ),
        daemon=True,
    )

    result = {}

    def run() -> None:
        result["stats"] = run_stage(
            stage, cfg, silent=True, hud=False, max_frames=900
        )

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    time.sleep(0.3)
    replayer.start()
    runner.join(timeout=30.0)
    assert "stats" in result, "run loop did not finish"

    stats = result["stats"]
    assert stats.frames >= 900
    assert stats.fixes > 500, "the car was on its own recon line; it must track"
    assert stats.spoken >= 1, "at least the early notes must be spoken"
