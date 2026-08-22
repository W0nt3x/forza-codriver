"""Config loading, overlay and hot reload.

The development rules calls hot-reloadable thresholds the most important architectural
constraint in the project, so it gets tests rather than trust.
"""

from __future__ import annotations

import logging

import pytest
import yaml

from codriver.config import Config, deep_merge, find_config_dir


@pytest.fixture
def cfg_dir(tmp_path):
    (tmp_path / "defaults.yaml").write_text(
        yaml.safe_dump(
            {
                "telemetry": {"port": 5400, "adapter": "fh6"},
                "runtime": {"trigger": {"reaction_buffer_s": 1.8, "min_lead_m": 15.0}},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_dotted_access(cfg_dir):
    cfg = Config.load(cfg_dir)
    assert cfg.get("telemetry.port") == 5400
    assert cfg["runtime.trigger.reaction_buffer_s"] == 1.8
    assert cfg.section("runtime.trigger")["min_lead_m"] == 15.0


def test_missing_key_raises_rather_than_defaulting_silently(cfg_dir):
    cfg = Config.load(cfg_dir)
    with pytest.raises(KeyError, match="stage.curvature.window_points"):
        cfg.get("stage.curvature.window_points")
    assert cfg.get("stage.curvature.window_points", 7) == 7


def test_local_overlays_defaults_without_replacing_siblings(cfg_dir):
    (cfg_dir / "local.yaml").write_text(
        yaml.safe_dump({"runtime": {"trigger": {"reaction_buffer_s": 2.4}}}),
        encoding="utf-8",
    )
    cfg = Config.load(cfg_dir)
    assert cfg.get("runtime.trigger.reaction_buffer_s") == 2.4
    assert cfg.get("runtime.trigger.min_lead_m") == 15.0
    assert cfg.get("telemetry.port") == 5400


def test_lists_are_replaced_not_merged():
    merged = deep_merge({"bands": [30, 40, 50]}, {"bands": [25, 35]})
    assert merged["bands"] == [25, 35]


def test_reload_picks_up_an_edit(cfg_dir):
    cfg = Config.load(cfg_dir)
    assert cfg.get("telemetry.port") == 5400

    (cfg_dir / "local.yaml").write_text(
        yaml.safe_dump({"telemetry": {"port": 5401}}), encoding="utf-8"
    )
    assert cfg.poll(immediate=True) is True
    assert cfg.get("telemetry.port") == 5401
    assert cfg.reload_count == 1


def test_poll_is_a_noop_when_nothing_changed(cfg_dir):
    cfg = Config.load(cfg_dir)
    assert cfg.poll(immediate=True) is False
    assert cfg.reload_count == 0


def test_reload_callbacks_fire(cfg_dir):
    cfg = Config.load(cfg_dir)
    seen = []
    cfg.on_reload(lambda c: seen.append(c.get("telemetry.port")))
    (cfg_dir / "local.yaml").write_text(
        yaml.safe_dump({"telemetry": {"port": 5402}}), encoding="utf-8"
    )
    cfg.poll(immediate=True)
    assert seen == [5402]


def test_half_written_yaml_keeps_the_previous_values(cfg_dir, caplog):
    """You will save this file mid-edit while driving. It must not take the
    co-driver down with it."""
    cfg = Config.load(cfg_dir)
    (cfg_dir / "local.yaml").write_text("telemetry: {port: [unclosed", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert cfg.poll(immediate=True) is False
    assert cfg.get("telemetry.port") == 5400
    assert "config reload failed" in caplog.text


def test_unknown_local_key_is_reported_as_a_probable_typo(cfg_dir, caplog):
    (cfg_dir / "local.yaml").write_text(
        yaml.safe_dump({"runtime": {"trigger": {"reation_buffer_s": 2.0}}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        Config.load(cfg_dir)
    assert "reation_buffer_s" in caplog.text
    assert "typo" in caplog.text


def test_the_shipped_defaults_carry_every_documented_section():
    """defaults.yaml is meant to hold ALL thresholds from the note, runtime and audio designs
    from day one, so its shape stops moving."""
    cfg = Config.load(find_config_dir())
    for key in (
        "telemetry.port",
        "capture.dir",
        "replay.spin_margin_s",
        "stage.resample.spacing_m",
        "stage.curvature.window_points",
        "stage.curvature.comfortable_lateral_g",
        "stage.curvature.class_speed_bands_kmh",
        "stage.notes.collapse_window_points",
        "stage.notes.distance_buckets_m",
        "stage.hazards.jump_susp_max_stretch",
        "runtime.locate.search_forward_points",
        "runtime.gaps.suspend_after_s",
        "runtime.gaps.rewind_jump_m",
        "runtime.trigger.reaction_buffer_s",
        "runtime.trigger.speed_curve_kmh",
        "runtime.queue.link_window_s",
        "audio.samplerate",
        "audio.crossfade_ms",
        "audio.placeholder_clip_s",
    ):
        cfg.get(key)


def test_shipped_port_avoids_the_range_the_game_uses():
    """The Data Out spec: the official docs say to avoid 5200-5300. Nearly every guide
    online says 5300 anyway."""
    port = Config.load(find_config_dir()).get("telemetry.port")
    assert not (5200 <= port <= 5300), f"port {port} is in the range FH6 binds"


def test_class_speed_bands_are_ascending_and_six_long():
    bands = Config.load(find_config_dir()).get("stage.curvature.class_speed_bands_kmh")
    assert len(bands) == 6
    assert bands == sorted(bands)


def test_paths_resolve_against_the_project_root_not_the_cwd(cfg_dir, tmp_path, monkeypatch):
    """The UI writes voices/ and recordings/ next to config/; the runtime must
    find them there wherever the process was started from. Seen live: a voice
    pack generated in the UI came back as beeps because `run` looked in the
    current working directory."""
    (cfg_dir / "defaults.yaml").write_text(
        yaml.safe_dump({"audio": {"voices_dir": "voices"}, "capture": {"dir": "C:/abs/recordings"}}),
        encoding="utf-8",
    )
    cfg = Config.load(cfg_dir)
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    assert cfg.root == cfg_dir.parent
    assert cfg.path("audio.voices_dir") == cfg_dir.parent / "voices"
    assert cfg.path("capture.dir").as_posix() == "C:/abs/recordings"
