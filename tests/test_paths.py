"""resolve_inside is the one door from a name to a path. Hostile names of
every flavour must bounce off it, plain names must pass."""

from __future__ import annotations

import pytest

from codriver.paths import UnsafePath, inside, resolve_inside

HOSTILE = [
    "../../../etc/passwd",
    "..\\..\\win.ini",
    "..",
    "C:\\Windows\\win.ini",
    "/etc/passwd",
    "a/b.json",
    "a\\b.json",
    "x'; rm -rf / #",
    "x; y",
    "",
    "   ",
    ".hidden",
    "a\nb",
    "a\x00b",
    "x" * 200,
]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_never_become_paths(tmp_path, name):
    with pytest.raises(UnsafePath):
        resolve_inside(tmp_path / "stages", name, "stage")


@pytest.mark.parametrize("name", [
    "coast-road-sprint.json",
    "stage2_20260822_231027.fzr",
    "left.wav",
    "My Stage.fzr",
    "de",
])
def test_plain_names_pass_and_land_under_base(tmp_path, name):
    base = tmp_path / "stages"
    path = resolve_inside(base, name, "stage")
    assert path.parent == base and path.name == name
    assert inside(base, path)


def test_inside_compares_resolved_paths(tmp_path):
    base = tmp_path / "stages"
    base.mkdir()
    assert inside(base, base)
    assert inside(base, base / "a.json")
    assert inside(base, base / "sub" / "a.json")
    assert not inside(base, base / ".." / "a.json")
    assert not inside(base, tmp_path / "stages2" / "a.json"), "a sibling with a longer name is not inside"
    assert not inside(base, tmp_path)


def test_the_message_names_the_kind_of_thing_not_the_whole_input():
    with pytest.raises(UnsafePath) as exc:
        resolve_inside("x", "../" + "y" * 500, "recording")
    assert "recording" in str(exc.value) and len(str(exc.value)) < 200
