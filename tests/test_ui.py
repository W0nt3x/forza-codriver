"""The web UI server: the API over the same functions the CLI uses.

Tested through FastAPI's TestClient against a throwaway project root, so
stages/recordings/config written here never touch the real ones.
"""

from __future__ import annotations

import shutil
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from codriver.config import Config, find_config_dir
from codriver.record.synth import SynthSpec, write_synth
from codriver.ui.server import config_schema, create_app


@pytest.fixture
def project(tmp_path):
    """A private copy of config/ plus empty stages/, recordings/, voices/."""
    src_cfg = find_config_dir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    shutil.copy(src_cfg / "defaults.yaml", cfg_dir / "defaults.yaml")
    for d in ("stages", "recordings", "recordings/runs", "voices"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    cfg = Config.load(cfg_dir)
    return tmp_path, cfg


@pytest.fixture
def client(project):
    root, cfg = project
    app = create_app(cfg, root, host_for_links="192.0.2.10", port=8777)
    with TestClient(app) as c:
        yield c, root, cfg


def test_state_reports_the_lan_url_and_empty_project(client):
    c, root, cfg = client
    s = c.get("/api/state").json()
    assert s["lan_url"] == "http://192.0.2.10:8777"
    assert s["telemetry_port"] == cfg.get("telemetry.port")
    assert s["stages"] == [] and s["recordings"] == [] and s["voices"] == []
    assert s["job"]["busy"] is False


def test_index_and_qr_are_served(client):
    c, _, _ = client
    assert c.get("/").status_code == 200
    assert "codriver" in c.get("/").text
    qr = c.get("/api/qr.svg")
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")


def test_build_then_stage_detail_then_delete(client):
    c, root, cfg = client
    write_synth(root / "recordings" / "slalom.fzr",
                SynthSpec(shape="slalom", duration_s=50.0, speed_mps=18.0, size_m=60.0,
                          pause_at_s=None, jump_at_s=None))
    assert [r["file"] for r in c.get("/api/state").json()["recordings"]] == ["slalom.fzr"]

    r = c.post("/api/build", json={"capture": "slalom.fzr", "name": "slalom"})
    assert r.status_code == 200, r.text
    assert r.json()["notes"], "a slalom must produce notes"
    assert (root / "stages" / "slalom.json").is_file()

    detail = c.get("/api/stages/slalom").json()
    assert detail["name"] == "slalom"
    assert len(detail["line"]) == len(detail["markings"]) > 50
    assert detail["notes"] and detail["runs"] == []

    assert c.post("/api/stages/slalom/learn").status_code == 400, "no runs yet"
    assert c.post("/api/stages/slalom/rebuild").status_code == 200
    assert c.delete("/api/stages/slalom").status_code == 200
    assert c.get("/api/stages/slalom").status_code == 404


def test_build_of_unknown_recording_is_404(client):
    c, _, _ = client
    assert c.post("/api/build", json={"capture": "nope.fzr"}).status_code == 404


def test_config_schema_carries_comments_as_help(client):
    c, _, cfg = client
    fields = c.get("/api/config").json()["fields"]
    by_key = {f["key"]: f for f in fields}
    assert "telemetry.port" in by_key
    assert "5200-5300" in by_key["telemetry.port"]["help"], "the port warning must reach the UI"
    assert by_key["runtime.trigger.reaction_buffer_s"]["type"] == "float"
    assert by_key["stage.curvature.class_speed_bands_kmh"]["type"] == "list"
    assert by_key["runtime.record.enabled"]["type"] == "bool"
    assert not any(f["overridden"] for f in fields)


def test_config_put_writes_local_yaml_and_is_live(client):
    """The whole point: an edit in the browser reaches the running config
    within a poll, via local.yaml, the same hot-reload path as editing by
    hand."""
    c, root, cfg = client
    r = c.put("/api/config", json={"key": "runtime.trigger.reaction_buffer_s", "value": "2.4"})
    assert r.status_code == 200 and r.json()["value"] == 2.4
    assert cfg.get("runtime.trigger.reaction_buffer_s") == 2.4
    local = yaml.safe_load((root / "config" / "local.yaml").read_text(encoding="utf-8"))
    assert local == {"runtime": {"trigger": {"reaction_buffer_s": 2.4}}}

    fields = {f["key"]: f for f in c.get("/api/config").json()["fields"]}
    assert fields["runtime.trigger.reaction_buffer_s"]["overridden"] is True

    # lists and bools round-trip through their text forms
    c.put("/api/config", json={"key": "stage.curvature.class_speed_bands_kmh", "value": "25, 35, 45, 60, 80, 100"})
    assert cfg.get("stage.curvature.class_speed_bands_kmh") == [25, 35, 45, 60, 80, 100]
    c.put("/api/config", json={"key": "runtime.record.enabled", "value": False})
    assert cfg.get("runtime.record.enabled") is False

    # reset removes the override and prunes empty sections
    assert c.delete("/api/config/runtime.trigger.reaction_buffer_s").status_code == 200
    assert cfg.get("runtime.trigger.reaction_buffer_s") == 1.8
    local = yaml.safe_load((root / "config" / "local.yaml").read_text(encoding="utf-8"))
    assert "trigger" not in local.get("runtime", {})


def test_unknown_config_key_is_rejected(client):
    c, _, _ = client
    assert c.put("/api/config", json={"key": "nope.nope", "value": 1}).status_code == 404


def test_jobs_are_exclusive_and_stoppable(client):
    """capture and run share the telemetry port: one at a time, and the UI's
    stop button must actually end the job."""
    c, _, cfg = client
    free = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM)
    free.bind(("127.0.0.1", 0))
    cfg.data["telemetry"]["port"] = free.getsockname()[1]
    free.close()

    assert c.post("/api/capture", json={"name": "t"}).status_code == 200
    time.sleep(0.2)
    assert c.get("/api/state").json()["job"]["busy"] is True
    assert c.post("/api/capture", json={"name": "u"}).status_code == 409
    assert c.post("/api/stop").json()["ok"] is True
    assert c.get("/api/state").json()["job"]["busy"] is False
    # nothing arrived, so no empty capture file was left behind
    assert c.get("/api/state").json()["recordings"] == []


def test_run_refuses_an_unknown_stage(client):
    c, _, _ = client
    assert c.post("/api/run", json={"stage": "ghost"}).status_code == 404


def test_schema_parser_on_the_shipped_defaults():
    cfg = Config.load(find_config_dir())
    fields = config_schema(cfg.defaults_path, cfg.data, {})
    keys = {f["key"] for f in fields}
    for key in ("telemetry.port", "stage.notes.long_min_m", "runtime.locate.search_forward_points",
                "audio.voice_pack", "stage.learn.max_shift_m"):
        assert key in keys
    assert all(f["help"] for f in fields if f["key"] in ("telemetry.port", "stage.curvature.window_points"))


def test_config_fields_are_tiered_and_labelled(client):
    """The Config tab shows three tiers. The essentials come first, in a fixed
    order, with friendly labels; stage.* values carry a rebuild marker."""
    c, _, _ = client
    fields = c.get("/api/config").json()["fields"]
    by_key = {f["key"]: f for f in fields}
    tiers = {f["tier"] for f in fields}
    assert tiers == {"essential", "more", "expert"}

    essentials = [f["key"] for f in fields if f["tier"] == "essential"]
    assert essentials[0] == "runtime.trigger.reaction_buffer_s"
    assert "audio.voice_pack" in essentials and "telemetry.port" in essentials
    assert [f["key"] for f in fields[: len(essentials)]] == essentials, "essentials lead the list"

    assert by_key["runtime.trigger.reaction_buffer_s"]["label"].startswith("Calls earlier or later")
    assert by_key["runtime.gaps.suspend_after_s"]["label"] == "Suspend after (seconds)"
    assert by_key["stage.notes.link_into_max_m"]["label"] == "Link into max (metres)"
    assert by_key["stage.notes.long_min_m"]["needs_rebuild"] is True
    assert by_key["runtime.trigger.reaction_buffer_s"]["needs_rebuild"] is False
    assert by_key["runtime.trigger.reaction_buffer_s"]["range"] == [0.8, 3.5, 0.1]
    assert by_key["telemetry.socket_rcvbuf_bytes"]["tier"] == "expert"
    assert by_key["audio.voice_pack"]["options"], "voice pack is a dropdown, never free text"
    assert "options" in by_key["audio.device"]


def test_audio_device_empty_means_default(client):
    c, _, cfg = client
    assert c.put("/api/config", json={"key": "audio.device", "value": ""}).status_code == 200
    assert cfg.get("audio.device") is None


def test_voice_dropdown_tells_the_truth_about_a_missing_pack(client):
    """A fresh clone with voice_pack pointing at a pack that was never
    generated must show that, not silently display the first existing pack as
    selected while the runtime falls back to beeps."""
    c, root, cfg = client
    options = {f["key"]: f for f in c.get("/api/config").json()["fields"]}["audio.voice_pack"]["options"]
    assert options[0]["value"] == cfg.get("audio.voice_pack")
    assert "not generated yet" in options[0]["label"]


def _fake_fetch(files):
    """A stand-in for HTTP: maps URL suffixes to bytes."""
    def fetch(url, timeout_s=8.0):
        for suffix, payload in files.items():
            if url.endswith(suffix):
                return payload
        raise OSError(f"404 {url}")
    return fetch


def test_community_list_and_install(client):
    c, root, cfg = client
    from codriver.stage.schema import save, Stage
    from codriver.stage.line import LinePoint
    from codriver.stage.notes import Note
    import json as _json

    shared = Stage(name="coast-road-sprint",
                   line=[LinePoint(x=float(i) * 3, y=0.0, z=0.0) for i in range(40)],
                   notes=[Note(at_m=30.0, tokens=["3", "right"], severity=3, direction="right")],
                   length_m=117.0)
    tmp = root / "shared.json"
    save(shared, tmp)
    index = {"stages": [
        {"file": "coast-road-sprint.json", "name": "coast-road-sprint", "length_m": 117.0, "notes": 1, "author": "nils"},
        {"file": "../evil.json", "name": "evil"},          # must be filtered out
    ]}
    c.app.state.fetch = _fake_fetch({
        "/index.json": _json.dumps(index).encode(),
        "/stages/coast-road-sprint.json": tmp.read_bytes(),
    })

    listing = c.get("/api/community").json()
    assert listing["available"] is True
    assert [s["file"] for s in listing["stages"]] == ["coast-road-sprint.json"], "unsafe names dropped"
    assert listing["stages"][0]["installed"] is False

    r = c.post("/api/community/install", json={"file": "coast-road-sprint.json"})
    assert r.status_code == 200, r.text
    assert (root / "stages" / "coast-road-sprint.json").is_file()
    assert c.get("/api/community").json()["stages"][0]["installed"] is True
    assert c.post("/api/community/install", json={"file": "coast-road-sprint.json"}).status_code == 409
    assert c.post("/api/community/install", json={"file": "../evil.json"}).status_code == 400
    detail = c.get("/api/stages/coast-road-sprint").json()
    assert detail["generator"]["installed_from"].endswith("stages/coast-road-sprint.json")


def test_community_unreachable_degrades_gracefully(client):
    c, _, _ = client
    c.app.state.fetch = _fake_fetch({})
    listing = c.get("/api/community").json()
    assert listing["available"] is False and "index" in listing["reason"]


def test_share_writes_a_clean_file_with_credits(client, monkeypatch):
    c, root, cfg = client
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    write_synth(root / "recordings" / "s.fzr",
                SynthSpec(shape="slalom", duration_s=40.0, speed_mps=18.0, size_m=60.0,
                          pause_at_s=None, jump_at_s=None))
    c.post("/api/build", json={"capture": "s.fzr", "name": "Coast Road Sprint"})
    assert (root / "stages" / "coast-road-sprint.json").is_file(), "names are lowercase slugs"

    r = c.post("/api/stages/coast-road-sprint/share", json={"author": "W0nt3x"})
    assert r.status_code == 200, r.text
    out = root / "share" / "coast-road-sprint.json"
    assert out.is_file()
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["community"]["author"] == "W0nt3x"
    assert data["community"]["race"] == "coast-road-sprint"
    assert "upload/main/stages" in r.json()["upload_url"]
