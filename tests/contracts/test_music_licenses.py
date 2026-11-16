"""Contract tests for lib/music_licenses.py (v2.5 AUD-10, D20).

A music bed may only ship from the registered local library, with commercial
rights, and attribution-required tracks must carry their credit.
"""
import json

import pytest

from lib import music_licenses as ml


def _track(file="warm-piano-01.mp3", commercial=True, attribution=False):
    return {
        "file": file,
        "title": "Warm Piano",
        "artist": "Example Artist",
        "source": "pixabay",
        "license": "Pixabay Content License",
        "commercial_use": commercial,
        "attribution_required": attribution,
        "url": "https://pixabay.com/music/1",
        "downloaded": "2026-11-16",
    }


def _library(*tracks):
    return {"version": 1, "tracks": list(tracks)}


def _decisions(asset_id="warm-piano-01.mp3", legacy=False):
    if legacy:
        return {"music": {"asset_id": asset_id, "volume": 0.3}}
    return {"audio": {"music": {"asset_id": asset_id, "volume": 0.3}}}


def test_valid_library_has_no_problems():
    assert ml.validate_library(_library(_track())) == []


def test_missing_library_file_is_an_error(tmp_path):
    with pytest.raises(ml.MusicLicenseError, match="not found"):
        ml.load(tmp_path / "licenses.json")


def test_missing_required_field_is_reported():
    track = _track()
    del track["downloaded"]
    problems = ml.validate_library(_library(track))
    assert any("downloaded" in p for p in problems)


def test_non_boolean_flags_are_reported():
    track = _track()
    track["commercial_use"] = "yes"
    assert any("commercial_use" in p for p in ml.validate_library(_library(track)))


def test_registered_commercial_track_passes():
    assert ml.check_edit_decisions(_decisions(), _library(_track())) == []


def test_unregistered_track_is_blocked():
    problems = ml.check_edit_decisions(_decisions("unknown.mp3"), _library(_track()))
    assert problems and "not registered" in problems[0]


def test_non_commercial_track_is_blocked():
    problems = ml.check_edit_decisions(_decisions(), _library(_track(commercial=False)))
    assert problems and "commercial_use" in problems[0]


def test_legacy_top_level_music_is_checked_too():
    problems = ml.check_edit_decisions(_decisions("unknown.mp3", legacy=True), _library(_track()))
    assert problems


def test_no_music_means_no_violations():
    assert ml.audit({}, _library(_track())) == []


def test_attribution_required_track_must_be_credited():
    library = _library(_track(attribution=True))
    problems = ml.check_attribution(_decisions(), library, rights_text="")
    assert problems and "attribution" in problems[0]
    assert ml.check_attribution(
        _decisions(), library, rights_text="Music: Example Artist - Warm Piano"
    ) == []


def test_attribution_required_with_empty_credit_is_blocked():
    track = _track(attribution=True)
    track["artist"] = " "
    track["title"] = ""
    problems = ml.check_attribution(_decisions(), _library(track), rights_text="")
    assert problems and "no artist/title" in problems[0]


def test_audit_combines_all_checks():
    problems = ml.audit(_decisions("unknown.mp3"), _library(), rights_text="")
    assert len(problems) == 1 and "not registered" in problems[0]