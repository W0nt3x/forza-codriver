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


def _no_network(*args, **kwargs):
    raise OSError("network is disabled in tests")


@pytest.fixture
def client(project):
    root, cfg = project
    app = create_app(cfg, root, host_for_links="192.0.2.10", port=8777)
    # The shipped defaults point at the live community relay. Tests never
    # talk to it (or to GitHub); each test that needs a remote installs a fake.
    app.state.post_json = _no_network
    app.state.fetch = _no_network
    # What the real page does: opened as an IP, and every request carries the
    # page header. Tests for the guards themselves build their own clients.
    with TestClient(app, base_url="http://127.0.0.1:8777", headers={"X-Codriver": "1"}) as c:
        yield c, root, cfg


def test_state_reports_the_lan_url_and_empty_project(client):
    c, root, cfg = client
    s = c.get("/api/state").json()
    assert s["lan_url"] == "http://192.0.2.10:8777"
    assert s["telemetry_port"] == cfg.get("telemetry.port")
    assert s["stages"] == [] and s["recordings"] == [] and s["voices"] == []
    assert s["job"]["busy"] is False
    assert s["colours"]["1"] == cfg.get("display.colours.class_1") and s["colours"]["S"] == cfg.get("display.colours.straight")


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
    out = root / "stages" / "share" / "coast-road-sprint.json"
    assert out.is_file()
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["community"]["author"] == "W0nt3x"
    assert data["community"]["race"] == "coast-road-sprint"
    assert "upload/main/stages" in r.json()["upload_url"]


def test_restart_badge_marks_what_is_read_only_at_start(client):
    """Voice pack, audio device, telemetry port: read once when the co-driver
    starts. Live values (reaction buffer, crossfade) must not carry it."""
    c, _, _ = client
    by_key = {f["key"]: f for f in c.get("/api/config").json()["fields"]}
    assert by_key["audio.voice_pack"]["needs_restart"] is True
    assert by_key["audio.device"]["needs_restart"] is True
    assert by_key["telemetry.port"]["needs_restart"] is True
    assert by_key["runtime.record.enabled"]["needs_restart"] is True
    assert by_key["runtime.trigger.reaction_buffer_s"]["needs_restart"] is False
    assert by_key["audio.crossfade_ms"]["needs_restart"] is False
    assert by_key["stage.curvature.window_points"]["needs_restart"] is False
    assert by_key["logging.level"]["needs_restart"] is False, "applied live by the server"


def test_logging_level_applies_without_closing_the_program(client):
    """Nothing in the config should ever need the program closed. Logging was
    the one value read only at process start; the server now applies it."""
    import logging

    c, _, _ = client
    before = logging.getLogger().level
    try:
        c.put("/api/config", json={"key": "logging.level", "value": "DEBUG"})
        assert logging.getLogger().level == logging.DEBUG
        c.delete("/api/config/logging.level")
        assert logging.getLogger().level == logging.INFO
    finally:
        logging.getLogger().setLevel(before)


def test_state_names_the_words_a_voice_pack_is_missing(client):
    """A pack generated before a word joined the vocabulary plays that word
    as a beep. The state must say which words, so the UI can point at
    Generate instead of leaving someone to hear a beep at a ford."""
    from codriver.voice.vocab import VOCABULARY

    c, root, cfg = client
    pack = root / "voices" / "old"
    pack.mkdir()
    tokens = {t: f"{t}.wav" for t in VOCABULARY if t != "water"}
    (pack / "manifest.yaml").write_text(
        yaml.safe_dump({"name": "old", "language": "en", "tokens": tokens}),
        encoding="utf-8",
    )
    s = c.get("/api/state").json()
    assert s["voices"] == ["old"]
    assert s["voice_gaps"] == {"old": ["water"]}


def test_a_fresh_clone_has_no_stages_folder_and_that_is_fine(client):
    """Nothing in git creates stages/ (stage files are ignored). The UI must
    come up without it, and the first Install or Build must create it."""
    import json as _json
    import shutil

    from codriver.stage.line import LinePoint
    from codriver.stage.schema import Stage, save

    c, root, cfg = client
    shared = Stage(name="coast-road-sprint",
                   line=[LinePoint(x=float(i) * 3, y=0.0, z=0.0) for i in range(40)],
                   length_m=117.0)
    tmp = root / "shared.json"
    save(shared, tmp)
    c.app.state.fetch = _fake_fetch({
        "/index.json": _json.dumps({"stages": [{"file": "coast-road-sprint.json", "name": "coast-road-sprint"}]}).encode(),
        "/stages/coast-road-sprint.json": tmp.read_bytes(),
    })
    shutil.rmtree(root / "stages")
    assert not (root / "stages").exists()

    assert c.get("/api/state").json()["stages"] == []
    assert c.get("/api/community").json()["stages"][0]["installed"] is False
    r = c.post("/api/community/install", json={"file": "coast-road-sprint.json"})
    assert r.status_code == 200, r.text
    assert (root / "stages" / "coast-road-sprint.json").is_file(), "install created the folder"


def _shared_stage_fetch(root, name="coast-road-sprint", author="nils"):
    """A fake community repo holding one stage, for preview and install tests."""
    import json as _json

    from codriver.stage.curvature import STRAIGHT
    from codriver.stage.line import LinePoint
    from codriver.stage.notes import Note
    from codriver.stage.schema import Stage, to_dict

    st = Stage(name=name,
               line=[LinePoint(x=float(i) * 3, y=0.0, z=0.0) for i in range(40)],
               markings=[STRAIGHT] * 40,
               notes=[Note(at_m=30.0, tokens=["3", "right"], severity=3, direction="right")],
               length_m=117.0)
    data = to_dict(st)
    data["community"] = {"race": name, "author": author}
    index = {"stages": [{"file": f"{name}.json", "name": name, "length_m": 117.0, "notes": 1, "author": author}]}
    return _fake_fetch({
        "/index.json": _json.dumps(index).encode(),
        f"/stages/{name}.json": _json.dumps(data).encode(),
    })


def test_community_preview_shows_a_stage_without_installing_it(client):
    c, root, cfg = client
    c.app.state.fetch = _shared_stage_fetch(root)

    r = c.get("/api/community/preview/coast-road-sprint.json")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "coast-road-sprint"
    assert len(p["line"]) == 40 and len(p["markings"]) == 40
    assert [n["text"] for n in p["notes"]] == ["3 right"]
    assert p["installed"] is False and p["runs"] == []
    assert p["community"]["author"] == "nils"
    assert not (root / "stages" / "coast-road-sprint.json").exists(), "a preview installs nothing"

    assert c.get("/api/community/preview/Evil.json").status_code == 400
    c.app.state.fetch = _fake_fetch({})
    assert c.get("/api/community/preview/coast-road-sprint.json").status_code == 502


def test_share_goes_through_the_relay_and_falls_back_to_the_upload_page(client, monkeypatch):
    c, root, cfg = client
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    write_synth(root / "recordings" / "s.fzr",
                SynthSpec(shape="slalom", duration_s=40.0, speed_mps=18.0, size_m=60.0,
                          pause_at_s=None, jump_at_s=None))
    c.post("/api/build", json={"capture": "s.fzr", "name": "Coast Road Sprint"})
    assert c.put("/api/config", json={"key": "community.relay_url", "value": "https://relay.example/"}).status_code == 200
    assert c.put("/api/config", json={"key": "community.relay_secret", "value": "s3cret"}).status_code == 200

    posted = {}

    def fake_post(url, payload, timeout_s=60.0, headers=None):
        posted["url"] = url
        posted["payload"] = payload
        posted["headers"] = headers
        return {"ok": True, "pr_url": "https://github.com/W0nt3x/codriver-stages/pull/9", "number": 9, "updated": False}

    c.app.state.post_json = fake_post
    r = c.post("/api/stages/coast-road-sprint/share", json={"author": "nils"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["via"] == "relay" and body["pr_url"].endswith("/pull/9") and body["updated"] is False
    assert posted["url"] == "https://relay.example/share", "one slash, whatever the config says"
    assert posted["payload"]["file"] == "coast-road-sprint.json"
    assert posted["payload"]["stage"]["format"] == "codriver-stage"
    assert posted["payload"]["stage"]["community"]["author"] == "nils"
    assert posted["headers"] == {"X-Codriver-Secret": "s3cret"}

    def broken(url, payload, timeout_s=60.0, headers=None):
        raise OSError("connection refused")

    c.app.state.post_json = broken
    body = c.post("/api/stages/coast-road-sprint/share", json={"author": "nils"}).json()
    assert body["via"] == "manual"
    assert "connection refused" in body["relay_error"]
    assert "upload/main/stages" in body["upload_url"]

    def refused(url, payload, timeout_s=60.0, headers=None):
        return {"error": "stage has no notes"}

    c.app.state.post_json = refused
    body = c.post("/api/stages/coast-road-sprint/share", json={"author": "nils"}).json()
    assert body["via"] == "manual" and "no notes" in body["relay_error"]


def test_a_stage_name_in_a_request_cannot_leave_the_stages_folder(client):
    """Windows accepts backslashes as separators, and a path parameter lets
    them through. Every endpoint that turns a name into stages/<name>.json
    must refuse anything that is not a plain name. (Encoded slashes never
    reach the handler, they fail routing; that is a refusal too.)"""
    c, root, cfg = client
    victim = root / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    refused = (400, 404, 405)
    for name in ("..\\victim", "..%5Cvictim", "..%2F..%2Fvictim", "C:\\x"):
        assert c.delete(f"/api/stages/{name}").status_code in refused, name
        assert c.get(f"/api/stages/{name}").status_code in refused, name
        assert c.post(f"/api/stages/{name}/share", json={}).status_code in refused, name
        assert c.post(f"/api/stages/{name}/learn").status_code in refused, name
    assert c.post("/api/run", json={"stage": "..\\victim"}).status_code == 400
    assert c.delete("/api/stages/..%5Cvictim").status_code == 400, "a backslash is a separator here"
    assert victim.is_file(), "nothing outside stages/ was touched"


def test_an_installed_stage_is_named_after_its_file_not_its_contents(client):
    """The JSON inside a shared file can say any name it likes. The file
    name was validated; the inner name was not, and it ends up in run
    recording file names. So the file name wins."""
    import json as _json

    from codriver.stage.schema import load

    c, root, cfg = client
    fetch = _shared_stage_fetch(root)
    raw = _json.loads(fetch("https://x/stages/coast-road-sprint.json"))
    raw["name"] = "../../../evil"
    c.app.state.fetch = _fake_fetch({
        "/index.json": fetch("https://x/index.json"),
        "/stages/coast-road-sprint.json": _json.dumps(raw).encode(),
    })
    assert c.post("/api/community/install", json={"file": "coast-road-sprint.json"}).status_code == 200
    assert load(root / "stages" / "coast-road-sprint.json").name == "coast-road-sprint"
    assert c.get("/api/state").json()["stages"][0]["name"] == "coast-road-sprint"


def test_voice_pack_name_cannot_be_a_path(client):
    c, root, cfg = client
    for bad in ("../x", "a/b", "a\\b", "..", "Sp ace"):
        r = c.post("/api/voice/generate", json={"lang": "en", "name": bad})
        assert r.status_code == 400, (bad, r.text)


def test_state_changing_requests_need_the_page_header(client):
    """A plain POST is a "simple request": a browser sends it cross-origin
    from any page, without asking. /api/stop, /api/scan and /share take no
    body, so a random website could have stopped a run or shared a stage.
    Requiring a custom header forces a CORS preflight, which nothing here
    answers. Reads stay open; they return nothing a page could act on."""
    c, root, cfg = client
    bare = TestClient(c.app, base_url="http://127.0.0.1:8777")
    assert bare.get("/api/state").status_code == 200
    assert bare.post("/api/stop").status_code == 403
    assert bare.post("/api/scan", json={"duration": 1}).status_code == 403
    assert bare.post("/api/stages/waterfall-trail/share").status_code == 403
    assert bare.delete("/api/stages/waterfall-trail").status_code == 403
    assert bare.put("/api/config", json={"key": "telemetry.port", "value": 5300}).status_code == 403
    assert cfg.get("telemetry.port") == 5400, "and nothing changed"
    assert c.post("/api/stop").status_code != 403, "the page itself is fine"


def test_a_domain_name_in_the_host_header_is_refused(client):
    """DNS rebinding: a page on evil.example whose name later resolves to
    127.0.0.1 would be same-origin with this API and could read it all. The
    UI is only ever opened as localhost or an IP, so a domain name in Host is
    never legitimate."""
    c, root, cfg = client
    for bad in ("evil.example", "evil.example:8777", "codriver.evil.example"):
        assert c.get("/api/state", headers={"host": bad}).status_code == 400, bad
    for ok in ("localhost:8777", "localhost", "127.0.0.1:8777", "192.168.2.44:8777",
               "[::1]:8777", "gaming-pc.localhost:8777"):
        assert c.get("/api/state", headers={"host": ok}).status_code == 200, ok
    for bind_only in ("0.0.0.0:8777", "[::]:8777"):
        assert c.get("/api/state", headers={"host": bind_only}).status_code == 400, bind_only


def test_a_browser_origin_must_match_the_host_it_talks_to(client):
    """Rebinding, second line: once a page is same-origin it can set the
    X-Codriver header itself. But its Origin is still the attacker's name,
    while Host is this PC. The real page has Origin == Host, always."""
    c, root, cfg = client
    page = {"X-Codriver": "1", "host": "127.0.0.1:8777"}
    assert c.post("/api/stop", headers={**page, "origin": "http://127.0.0.1:8777"}).status_code != 403
    assert c.post("/api/stop", headers={**page, "origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/stop", headers={**page, "origin": "http://192.168.2.44:8777"}).status_code == 403, \
        "another address of this very PC is still not the page that was opened"
    assert c.post("/api/stop", headers={**page, "origin": "null"}).status_code == 403
    assert c.put("/api/config", json={"key": "telemetry.port", "value": 5300},
                 headers={**page, "origin": "http://evil.example"}).status_code == 403
    assert cfg.get("telemetry.port") == 5400
    # reads are not gated by origin: a page that can read them is same-origin anyway
    assert c.get("/api/state", headers={"host": "127.0.0.1:8777", "origin": "http://evil.example"}).status_code == 200


def test_a_missing_host_header_is_refused_on_http_1_1():
    """Browsers always send Host. The guard is exercised directly, because no
    HTTP client worth the name lets a test omit it."""
    import asyncio

    from codriver.ui.server import BrowserGuard

    sent = []

    async def inner(scope, receive, send):
        sent.append("reached app")

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    guard = BrowserGuard(inner)
    scope = {"type": "http", "method": "GET", "path": "/api/state", "http_version": "1.1", "headers": []}
    asyncio.run(guard(scope, receive, send))
    assert sent and sent[0]["type"] == "http.response.start" and sent[0]["status"] == 400
    sent.clear()
    scope["http_version"] = "1.0"
    asyncio.run(guard(scope, receive, send))
    assert sent == ["reached app"]


def test_websocket_accepts_the_page_and_refuses_foreign_origins(client):
    c, root, cfg = client
    # The TestClient connects websockets as Host "testserver" whatever the
    # base_url says; a real browser sends the address bar host, so say so.
    host = {"host": "127.0.0.1:8777"}
    with c.websocket_connect("/ws", headers={**host, "origin": "http://127.0.0.1:8777"}):
        pass
    with c.websocket_connect("/ws", headers={"host": "192.168.2.44:8777", "origin": "http://192.168.2.44:8777"}):
        pass
    with pytest.raises(Exception):
        with c.websocket_connect("/ws", headers={**host, "origin": "http://evil.example"}):
            pass
    with pytest.raises(Exception):  # rebinding: a domain name as Host
        with c.websocket_connect("/ws", headers={"host": "evil.example:8777"}):
            pass
    with pytest.raises(Exception):  # origin is an IP, but not the one the page was opened at
        with c.websocket_connect("/ws", headers={**host, "origin": "http://192.168.2.44:8777"}):
            pass


def test_downloads_are_bounded(monkeypatch):
    """community.repo is configurable, so whatever answers there is not
    trusted to be small. A stage is a few hundred KB; five MB is generous."""
    import urllib.request

    from codriver.ui.server import _http_get

    class Resp:
        def __init__(self, size):
            self.size = size

        def read(self, n=-1):
            return b"x" * (self.size if n < 0 else min(n, self.size))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp(6_000_000))
    with pytest.raises(ValueError):
        _http_get("https://example.invalid/big.json")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp(10))
    assert _http_get("https://example.invalid/small.json") == b"x" * 10


def _poisoned_stage(root, name, **source):
    from codriver.stage.curvature import STRAIGHT
    from codriver.stage.line import LinePoint
    from codriver.stage.schema import Stage, save

    st = Stage(name=name, line=[LinePoint(x=float(i) * 3, y=0.0, z=0.0) for i in range(12)],
               markings=[STRAIGHT] * 12, source=source)
    save(st, root / "stages" / f"{name}.json")


def test_rebuild_does_not_follow_a_recording_path_from_inside_the_stage_file(client):
    """source.capture is written by the builder, but a stage file can come
    from anywhere since, and say "../../../../etc/passwd" there."""
    c, root, cfg = client
    (root / "victim.fzr").write_bytes(b"FZRAW")  # exists, so only the path check can stop it
    for bad in ("../victim.fzr", "..\\victim.fzr", "C:\\victim.fzr", "/etc/passwd", "", "a/b.fzr"):
        _poisoned_stage(root, "poisoned", capture=bad)
        r = c.post("/api/stages/poisoned/rebuild")
        assert r.status_code == 400, (bad, r.text)
        assert "Setup tab" in r.json()["detail"], (bad, r.text)


def test_build_refuses_a_recording_name_that_is_a_path(client):
    c, root, cfg = client
    for bad in ("../x.fzr", "..\\x.fzr", "C:\\x.fzr", "/x.fzr", ""):
        assert c.post("/api/build", json={"capture": bad}).status_code == 400, bad
    assert c.post("/api/build", json={"capture": "nope.fzr"}).status_code == 404


def test_voice_say_refuses_a_pack_that_is_a_path(client):
    c, root, cfg = client
    for bad in ("../x", "a\\b", "..", "a/b"):
        assert c.post("/api/voice/say", json={"text": "1 left", "pack": bad}).status_code == 400, bad


def test_inside_is_the_last_word_on_paths(tmp_path):
    from codriver.ui.server import _inside

    base = tmp_path / "stages"
    base.mkdir()
    assert _inside(base, base / "a.json")
    assert _inside(base, base)
    assert not _inside(base, base / ".." / "a.json")
    assert not _inside(base, tmp_path / "stages2" / "a.json")
    assert not _inside(base, tmp_path / "stagesx")


def test_strings_from_a_stage_file_are_one_line_and_capped():
    from codriver.stage.schema import Stage, clean_text, from_dict, to_dict

    assert clean_text("  a\n b\t c  ", 80) == "a b c"
    assert clean_text("x" * 200, 80) == "x" * 80
    assert clean_text(None) == ""
    data = to_dict(Stage(name="ok"))
    data["name"] = "evil\nstage" + "!" * 200
    data["notes"] = [{"at_m": 1.0, "tokens": ["3\nright", "x" * 100], "kind": "corner\n", "direction": "right\n"}]
    st = from_dict(data)
    assert st.name == ("evil stage" + "!" * 200)[:80]
    assert st.notes[0].tokens == ["3 right", "x" * 40]
    assert st.notes[0].kind == "corner" and st.notes[0].direction == "right"


def test_a_malformed_stage_file_is_a_bad_file_not_a_dead_server(client):
    """Structural garbage in one stage file (a number where the token list
    belongs) must not take /api/state down for every other stage."""
    c, root, cfg = client
    (root / "stages" / "broken.json").write_text(
        '{"format": "codriver-stage", "version": 1, "name": "b", '
        '"notes": [{"at_m": 1, "tokens": 5}], "line": [], "markings": []}',
        encoding="utf-8",
    )
    (root / "stages" / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert c.get("/api/state").status_code == 200
    assert [s["name"] for s in c.get("/api/state").json()["stages"]] == []
    r = c.get("/api/stages/broken")
    assert r.status_code == 400 and "not a usable stage file" in r.json()["detail"]
    assert c.post("/api/run", json={"stage": "broken"}).status_code == 400


HOSTILE_STRINGS = ["../../../etc/passwd", "..\\..\\win.ini", "x'; drop", "a;b", "x" * 5000]


def test_voice_generate_allowlists_engine_voice_and_language(client):
    c, root, cfg = client
    for hostile in HOSTILE_STRINGS:
        assert c.post("/api/voice/generate", json={"lang": "en", "voice": hostile}).status_code == 400, hostile
        assert c.post("/api/voice/generate", json={"lang": hostile}).status_code == 400, hostile
        assert c.post("/api/voice/generate", json={"lang": "en", "engine": hostile}).status_code == 400, hostile


def test_scan_duration_is_a_bounded_number(client):
    c, root, cfg = client
    assert c.post("/api/scan", json={"duration": "abc"}).status_code == 400
    assert c.post("/api/scan", json={"duration": None}).status_code == 400


def test_say_text_is_bounded(client):
    c, root, cfg = client
    assert c.post("/api/voice/say", json={"text": "x" * 5000}).status_code == 400
    assert c.post("/api/voice/say", json={"text": "   "}).status_code == 400


def test_config_strings_are_bounded(client):
    c, root, cfg = client
    assert c.put("/api/config", json={"key": "audio.voice_pack", "value": "x" * 5000}).status_code == 400
    assert cfg.get("audio.voice_pack") != "x" * 5000


def test_share_author_is_one_line_in_the_file(client, monkeypatch):
    c, root, cfg = client
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    write_synth(root / "recordings" / "s.fzr",
                SynthSpec(shape="slalom", duration_s=40.0, speed_mps=18.0, size_m=60.0,
                          pause_at_s=None, jump_at_s=None))
    c.post("/api/build", json={"capture": "s.fzr", "name": "Coast Road Sprint"})
    r = c.post("/api/stages/coast-road-sprint/share", json={"author": "evil\n# heading " + "a" * 100})
    assert r.status_code == 200, r.text
    data = yaml.safe_load((root / "stages" / "share" / "coast-road-sprint.json").read_text(encoding="utf-8"))
    assert "\n" not in data["community"]["author"] and len(data["community"]["author"]) <= 60


class _FakeOverlay:
    """Stands in for the Win32 overlay: records events, pretends to run."""

    instances = []

    def __init__(self, cfg):
        from codriver.overlay.state import OverlayState

        self.cfg = cfg
        self.state = OverlayState()
        self.events = []
        self._running = False
        _FakeOverlay.instances.append(self)

    def handle_event(self, event):
        self.events.append(event)
        self.state.handle_event(event)

    def start_in_thread(self):
        self._running = True

    @property
    def running(self):
        return self._running

    def stop(self, timeout_s=3.0):
        self._running = False


def test_overlay_starts_in_process_on_the_job_stream(client):
    """The Overlay button: same events the web HUD gets, no socket, and the
    late starter is brought up to date from the job history."""
    c, root, cfg = client
    c.app.state.overlay_factory = _FakeOverlay
    _FakeOverlay.instances.clear()
    assert c.get("/api/state").json()["overlay"] is False

    jobs = c.app.state.jobs
    jobs.kind = "run"
    jobs.emit({"kind": "status", "state": "tracking", "along_m": 50.0, "speed_kmh": 80.0,
               "upcoming": [{"text": "3 right", "tokens": ["3", "right"], "severity": 3,
                             "direction": "right", "kind": "corner", "at_m": 170.0}]})
    r = c.post("/api/overlay", json={"on": True})
    assert r.status_code == 200 and r.json()["overlay"] is True
    ov = _FakeOverlay.instances[-1]
    assert ov.state.view().next.text == "3 right", "history replayed: the overlay knows the next call at once"
    assert c.get("/api/state").json()["overlay"] is True

    jobs.emit({"kind": "status", "state": "tracking", "along_m": 60.0, "speed_kmh": 80.0, "upcoming": []})
    assert ov.events[-1]["along_m"] == 60.0, "live events reach the overlay"
    assert c.post("/api/overlay", json={"on": True}).json()["overlay"] is True, "idempotent"

    assert c.post("/api/overlay", json={"on": False}).json()["overlay"] is False
    jobs.emit({"kind": "status", "state": "tracking", "along_m": 70.0, "speed_kmh": 80.0, "upcoming": []})
    assert ov.events[-1]["along_m"] == 60.0, "unsubscribed after stop"
    jobs.kind = None


def test_page_and_static_files_are_always_revalidated(client):
    """After update.bat the browser must not run a cached app.js against the
    new server: that is a button that does nothing."""
    c, root, cfg = client
    for path in ("/", "/static/app.js", "/static/style.css"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
    assert "cache-control" not in {k.lower() for k in c.get("/api/state").headers} or         c.get("/api/state").headers.get("cache-control") != "no-cache"

