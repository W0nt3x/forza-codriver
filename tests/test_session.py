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
    # the finish: position drops to 0 and stays there; after end_s it ends
    assert det.update(_frame(True, 6), t + 20.0) is None
    assert det.update(_frame(False, 0), t + 20.1) is None
    assert det.update(_frame(False, 0), t + 22.9) is None
    assert det.update(_frame(False, 0), t + 23.1) == "end"
    assert det.in_race is False


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


def _datagram(race_on: int, position: int, x: float, t_s: float) -> bytes:
    return pack_fields({
        "IsRaceOn": race_on, "RacePosition": position, "TimestampMS": int(t_s * 1000),
        "PositionX": x, "PositionY": 0.0, "PositionZ": 0.0, "Speed": 25.0 if race_on else 0.0,
        "NormalizedDrivingLine": 20 if position else 0, "CurrentRaceTime": t_s,
    })


def _evening(path: Path, rate_hz: float = 30.0) -> dict:
    """2 s free roam, 6 s race, 4 s menu, 5 s race, 1 s menu."""
    phases = [(2.0, 1, 0), (6.0, 1, 3), (4.0, 0, 0), (5.0, 1, 2), (1.0, 0, 0)]
    counts = {"race_frames": 0, "other": 0}
    t = 0.0
    dt = 1.0 / rate_hz
    with CaptureWriter(path, header={"note": "evening"}) as w:
        for seconds, race_on, pos in phases:
            for _ in range(int(seconds * rate_hz)):
                w.add(_datagram(race_on, pos, t * 25.0, t), int(t * 1e9))
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
        # pre-roll (2 s of game time) plus the tail until the end debounce (2 s): about 120 frames
        assert len(others) <= 160, "no free roam beyond pre-roll and tail"
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
