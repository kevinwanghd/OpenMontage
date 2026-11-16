"""Predicate primitives must be deterministic, three-valued and fail-closed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import lesson_predicates as predicates
from lib import lessons

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lessons"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_all_five_primitives_are_registered():
    expected = {
        "subtitle_timing_matches_speech",
        "music_audition_confirmed",
        "approval_basis_recorded",
        "loudness_in_range",
        "version_binding_current",
    }
    assert set(predicates.EVALUATORS) == expected
    assert expected <= set(lessons.known_primitives())


def test_subtitle_incident_fixture_fails_and_fixed_fixture_passes():
    tolerance = {"kind": "subtitle_timing_matches_speech", "tolerance_s": 0.35}
    assert predicates.evaluate(tolerance, _fixture("m1_subtitle_early.json")) is False
    assert predicates.evaluate(tolerance, _fixture("m1_subtitle_fixed.json")) is True


def test_subtitle_without_detectable_speech_is_undecidable_not_a_pass():
    predicate = {"kind": "subtitle_timing_matches_speech"}
    context = {"subtitles": [{"start": 1.0, "end": 2.0}], "speech": []}
    assert predicates.evaluate(predicate, context) is None


def test_subtitle_silent_line_is_skipped():
    predicate = {"kind": "subtitle_timing_matches_speech"}
    context = {
        "subtitles": [{"start": 1.0, "end": 2.0}, {"start": 5.0, "end": 6.0}],
        "speech": [{"start": 1.02, "end": 2.0}],
        "silent_lines": [1],
    }
    assert predicates.evaluate(predicate, context) is True


def test_subtitle_gap_over_limit_fails():
    predicate = {"kind": "subtitle_timing_matches_speech"}
    context = {
        "subtitles": [{"start": 1.0, "end": 2.0}],
        "speech": [{"start": 1.0, "end": 2.3}],
    }
    assert predicates.evaluate(predicate, context) is False


def test_music_audition_must_be_recorded():
    predicate = {"kind": "music_audition_confirmed"}
    confirmed = {"edit_decisions": {"audio": {"music": {"asset_id": "a.mp3", "audition_confirmed": True}}}}
    missing = {"edit_decisions": {"audio": {"music": {"asset_id": "a.mp3"}}}}
    assert predicates.evaluate(predicate, confirmed) is True
    assert predicates.evaluate(predicate, missing) is False
    assert predicates.evaluate(predicate, {"edit_decisions": {}}) is None


def test_approval_basis_must_be_non_empty_text():
    predicate = {"kind": "approval_basis_recorded"}
    assert predicates.evaluate(predicate, {"checkpoint": {"approval_basis": "看过第 3 稿"}}) is True
    assert predicates.evaluate(predicate, {"checkpoint": {"approval_basis": "  "}}) is False
    assert predicates.evaluate(predicate, {"checkpoint": {}}) is False


def test_loudness_window_defaults_to_platform_target():
    predicate = {"kind": "loudness_in_range"}
    assert predicates.evaluate(predicate, {"loudness_lufs": -14.0}) is True
    assert predicates.evaluate(predicate, {"loudness_lufs": -11.2}) is False
    assert predicates.evaluate(predicate, {}) is None


def test_version_binding_must_match_current_version():
    predicate = {"kind": "version_binding_current"}
    assert predicates.evaluate(predicate, {"version_binding": "v7", "current_version": "v7"}) is True
    assert predicates.evaluate(predicate, {"version_binding": "v6", "current_version": "v7"}) is False
    assert predicates.evaluate(predicate, {"version_binding": "v7"}) is None


def test_unknown_kind_raises_instead_of_passing():
    with pytest.raises(predicates.PredicateError):
        predicates.evaluate({"kind": "not_registered"}, {})