"""Auto-record: a race is recognised from the packets themselves, and one
listener turns an evening into one recording per race."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from codriver.adapters.base import TelemetryFrame
from codriver.adapters.fh6 import FH6Adapter, pack_fields
from codriver.config import Config, find_config_dir
from codriver.record.capture import CaptureReader, CaptureWriter, read_all
from codriver.record.session import RaceDetector, race_filename, session_record


def _frame(race_on: bool, position: int, t: float = 0.0) -> TelemetryFrame:
    return TelemetryFrame(t=t, race_on=race_on, race_position=position)


# --------------------------------------------------------------------------
# the detector
# --------------------------------------------------------------------------


def test_free_roam_is_not_a_race_and_a_race_needs_a_few_frames():
    det = RaceDetector(start_frames=5, end_s=3.0, gap_s=5.0)
    # free roam: IsRaceOn set, speed irrelevant, position 0
    for i in range(50):
        assert det.update(_frame(True, 0), i * 0.03) is None
    assert det.in_race is False
    # the race: position appears; four frames are not enough, the fifth is
    t = 10.0
    for i in range(4):
        assert det.update(_frame(True, 7), t + i * 0.03) is None
    assert det.update(_frame(True, 7), t + 0.12) == "start"
    assert det.in_race is True
    # a blip of position 0 mid-race does not end it
    assert det.update(_frame(True, 0), t + 1.0) is None
    assert det.update(_frame(True, 6), t + 1.03) is None
    assert det.in_race is True
    # back in free roam (IsRaceOn, no position) for end_s: the event is over
    assert det.update(_frame(True, 6), t + 20.0) is None
    assert det.update(_frame(True, 0), t + 20.1) is None
    assert det.update(_frame(True, 0), t + 22.9) is None
    assert det.update(_frame(True, 0), t + 23.1) == "end"
    assert det.in_race is False


def _racing(position: int, t: float, race_time: float) -> TelemetryFrame:
    return TelemetryFrame(t=t, race_on=True, race_position=position, race_time=race_time)


def _blank(t: float) -> TelemetryFrame:
    """What the game sends while paused, rewinding, or on the results
    screen: IsRaceOn 0 and nothing else."""
    return TelemetryFrame(t=t, race_on=False, race_position=0, race_time=0.0)


def test_blank_packets_hold_the_race_and_the_resume_decides():
    """Captures show a rewind as a stretch of blank packets and a resume
    with the race clock a few seconds back; a pause resumes with the clock
    where it was; a restart resumes with it at zero. Only the last ends the
    race. Blank packets alone end it after gap_s, the fallback."""
    det = RaceDetector(start_frames=1, end_s=3.0, gap_s=90.0, restart_s=5.0)
    assert det.update(_racing(3, 0.0, 0.0), 0.0) == "start"
    assert det.update(_racing(3, 60.0, 58.5), 60.0) is None
    # a rewind: 5.5 s of blank packets, then racing with the clock 4 s back
    for i in range(165):
        assert det.update(_blank(60.03 + i * 0.033), 60.03 + i * 0.033) is None
    assert det.update(_racing(3, 65.6, 54.6), 65.6) is None, "a rewind is the same race"
    assert det.in_race is True
    # a long pause: two and a half minutes of blank, longer than gap_s
    for i in range(int(150 / 0.033)):
        change = det.update(_blank(70.0 + i * 0.033), 70.0 + i * 0.033)
        if change == "end":
            break
    else:
        pytest.fail("a blank stretch longer than gap_s must end the race")
    assert det.in_race is False
    # a new race after that: the usual start
    assert det.update(_racing(1, 230.0, 0.5), 230.0) == "start"
    assert det.update(_racing(1, 260.0, 30.5), 260.0) is None
    # a restart: blank, then racing with the clock at zero
    for i in range(120):
        det.update(_blank(260.03 + i * 0.033), 260.03 + i * 0.033)
    assert det.update(_racing(1, 264.0, 0.4), 264.0) == "end", "the clock started over: a new race"
    assert det.in_race is False
    assert det.update(_racing(1, 264.03, 0.43), 264.03) == "start", "and that new race begins"
    # the resume packet counts towards the next race's start_frames
    det2 = RaceDetector(start_frames=2, end_s=3.0, gap_s=90.0, restart_s=5.0)
    det2.update(_racing(1, 0.0, 0.0), 0.0)
    assert det2.update(_racing(1, 0.03, 0.03), 0.03) == "start"
    det2.update(_racing(1, 40.0, 40.0), 40.0)
    for i in range(60):
        det2.update(_blank(40.03 + i * 0.033), 40.03 + i * 0.033)
    assert det2.update(_racing(1, 42.0, 0.2), 42.0) == "end"
    assert det2.update(_racing(1, 42.03, 0.23), 42.03) == "start", "and the restart is the next race"
    # a pause: the clock continues where it stopped
    det2.update(_racing(1, 50.0, 8.0), 50.0)
    for i in range(300):
        det2.update(_blank(50.03 + i * 0.033), 50.03 + i * 0.033)
    assert det2.update(_racing(1, 60.0, 8.03), 60.0) is None and det2.in_race


def test_the_next_event_from_the_results_screen_is_another_race():
    """The race clock runs on from one event into the next when you go
    there from the results screen (seen in the captures), so the clock
    cannot tell them apart. Where the car resumes can: the next event is
    nowhere this race went. A restart resumes at the line, a rewind on the
    road: both stay this race."""

    def racing(t: float, race_time: float, x: float) -> TelemetryFrame:
        return TelemetryFrame(t=t, race_on=True, race_position=2, race_time=race_time, x=x, z=0.0)

    det = RaceDetector(start_frames=1, end_s=3.0, gap_s=300.0, restart_s=5.0, path_every=30, path_radius_m=150.0)
    assert det.update(racing(0.0, 0.0, 0.0), 0.0) == "start"
    t, x = 0.0, 0.0
    while x < 3000.0:  # a 3 km race at 30 m/s
        t += 0.033
        x += 1.0
        det.update(racing(t, t, x), t)
    # a rewind: blank, then back 200 m along the road, clock a bit back
    for i in range(150):
        det.update(_blank(t + i * 0.033), t + i * 0.033)
    t += 5.0
    assert det.update(racing(t, t - 7.0, x - 200.0), t) is None and det.in_race
    # a restart from the pause menu: blank, then the start line, clock running on
    for i in range(150):
        det.update(_blank(t + i * 0.033), t + i * 0.033)
    t += 5.0
    assert det.update(racing(t, t, 0.0), t) is None and det.in_race, "the line is this race's road"
    # the finish, the results screen, straight into the next event 8 km away
    for i in range(600):
        det.update(_blank(t + i * 0.033), t + i * 0.033)
    t += 20.0
    assert det.update(racing(t, t, 8000.0), t) == "end", "nowhere this race went: another race"
    assert det.update(racing(t + 0.033, t + 0.033, 8001.0), t + 0.033) == "start"


def test_a_loading_screen_silence_ends_a_race():
    det = RaceDetector(start_frames=1, end_s=3.0, gap_s=5.0)
    assert det.update(_frame(True, 3), 0.0) == "start"
    assert det.tick(4.0) is None
    assert det.tick(5.1) == "end"
    assert det.tick(9.0) is None, "only once"


def test_race_file_names_do_not_collide(tmp_path):
    a = race_filename(tmp_path, when=1_700_000_000.0)
    a.write_bytes(b"")
    b = race_filename(tmp_path, when=1_700_000_000.0)
    assert a != b and a.name.startswith("race-") and b.name.endswith("-2.fzr")


# --------------------------------------------------------------------------
# the adapter carries what the detector needs
# --------------------------------------------------------------------------


def test_fh6_frames_carry_race_position_and_driving_line():
    ad = FH6Adapter()
    data = pack_fields({"IsRaceOn": 1, "RacePosition": 5, "NormalizedDrivingLine": -40, "Speed": 10.0})
    f = ad.parse(data, 0.0)
    assert f.race_position == 5 and f.driving_line == -40
    fixture = Path(__file__).parent / "fixtures"
    packets = sorted(fixture.glob("*.bin"))
    if packets:
        real = ad.parse(packets[0].read_bytes(), 0.0)
        assert 0 <= real.race_position <= 255


# --------------------------------------------------------------------------
# the whole thing over UDP: free roam, race, menu, race -> two files
# --------------------------------------------------------------------------


def _datagram(race_on: int, position: int, x: float, t_s: float, race_time: float | None = None) -> bytes:
    return pack_fields({
        "IsRaceOn": race_on, "RacePosition": position, "TimestampMS": int(t_s * 1000),
        "PositionX": x, "PositionY": 0.0, "PositionZ": 0.0, "Speed": 25.0 if race_on else 0.0,
        "NormalizedDrivingLine": 20 if position else 0,
        "CurrentRaceTime": t_s if race_time is None else race_time,
    })


def _evening(path: Path, rate_hz: float = 30.0) -> dict:
    """2 s free roam, 6 s race, 4 s menu, 5 s race, 1 s menu."""
    phases = [(2.0, 1, 0), (6.0, 1, 3), (4.0, 0, 0), (5.0, 1, 2), (1.0, 0, 0)]
    counts = {"race_frames": 0, "other": 0}
    t = 0.0
    dt = 1.0 / rate_hz
    with CaptureWriter(path, header={"note": "evening"}) as w:
        for seconds, race_on, pos in phases:
            t0 = t  # each race runs its own clock from zero, as the game's does
            for _ in range(int(seconds * rate_hz)):
                w.add(_datagram(race_on, pos, t * 25.0, t, race_time=t - t0), int(t * 1e9))
                counts["race_frames" if pos else "other"] += 1
                t += dt
    return counts


def test_session_records_each_race_on_its_own(tmp_path):
    from codriver.record.replay import replay_file

    evening = tmp_path / "evening.fzr"
    _evening(evening)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = Config.load(find_config_dir())
    cfg.data["telemetry"]["port"] = port
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"
    # the replay runs at 4x and the detector's clocks are wall time: a 0.5 s
    # window here is 2 s of game time, the shipped defaults scaled down
    cfg.data["capture"]["auto"] = {"start_frames": 15, "end_s": 0.5, "gap_s": 1.0, "preroll_s": 0.5, "min_seconds": 1.0}

    out = tmp_path / "recordings"
    events: list[dict] = []
    stop = threading.Event()
    result: dict = {}
    runner = threading.Thread(
        target=lambda: result.update(r=session_record(cfg, out, on_event=events.append, should_stop=stop.is_set)),
        daemon=True)
    runner.start()
    time.sleep(0.3)
    replay_file(evening, host="127.0.0.1", port=port, speed=4.0)
    time.sleep(2.0)
    stop.set()
    runner.join(timeout=10.0)
    assert not runner.is_alive()

    r = result["r"]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "session_started" and kinds[-1] == "done"
    assert kinds.count("race_started") == 2 and kinds.count("race_saved") == 2, kinds
    assert len(r.races) == 2 and all(p.name.startswith("race-") for p in r.races)
    ad = FH6Adapter()
    for path, expect_pos, other_pos, expect_len_s in zip(r.races, (3, 2), (2, 3), (6.0, 5.0)):
        header, records = read_all(path)
        assert header["auto"] is True and header["race_index"] in (1, 2)
        frames = [ad.parse(d, 0.0) for _, d in records]
        racing = [f for f in frames if f.race_position == expect_pos]
        others = [f for f in frames if f.race_position != expect_pos]
        assert len(racing) >= expect_len_s * 30 * 0.95, "the race itself is all there"
        # pre-roll (2 s of game time, 60 frames) plus the tail: blank packets
        # are held until the next race's clock reset says the race is over,
        # so the whole 4 s menu (120 frames) sits in the first file
        assert len(others) <= 190, "no free roam beyond pre-roll and tail"
        assert not any(f.race_position == other_pos for f in frames), "and nothing of the other race"
    assert r.discarded == 0
    assert not (out / "evening.fzr").exists()


def test_session_endpoint_is_a_job_like_capture(client_session):
    c, root, cfg = client_session
    free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    free.bind(("127.0.0.1", 0))
    cfg.data["telemetry"]["port"] = free.getsockname()[1]
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"
    free.close()
    r = c.post("/api/session", json={})
    assert r.status_code == 200, r.text
    time.sleep(0.3)
    assert c.get("/api/state").json()["job"]["kind"] == "session"
    assert c.post("/api/capture", json={"name": "t"}).status_code == 409, "one listener on the port"
    assert c.post("/api/stop").json()["ok"] is True
    time.sleep(0.3)
    assert c.get("/api/state").json()["job"]["busy"] is False
    assert c.get("/api/state").json()["recordings"] == [], "no race, no file"


@pytest.fixture
def client_session(tmp_path):
    import shutil

    from starlette.testclient import TestClient

    from codriver.ui.server import create_app

    src_cfg = find_config_dir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    shutil.copy(src_cfg / "defaults.yaml", cfg_dir / "defaults.yaml")
    for d in ("stages", "recordings", "recordings/runs", "voices"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    cfg = Config.load(cfg_dir)
    app = create_app(cfg, tmp_path, host_for_links="192.0.2.10", port=8777)
    with TestClient(app, base_url="http://127.0.0.1:8777", headers={"X-Codriver": "1"}) as c:
        yield c, tmp_path, cfg
