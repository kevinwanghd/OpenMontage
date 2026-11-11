"""WP-K contract tests for the beat map stage (BEAT-01..08).

Guards:
- ``beat-scale-v1`` is a deterministic, table-driven duration->structure
  function: 90/120/180s are covered explicitly, the default 120s yields exactly
  seven segments with continuous, gap-free timestamps ending at the duration.
- Hook density is machine-checked: a candidate inside the 0-3s hard window, at
  least f(duration) candidates (120s -> 5), and reversals no further apart than
  the declared turn interval plus one beat of quantization.
- Marking is not validity: hook/cliffhanger markers are labelled unconfirmed
  and the narrative scaffold demands human review (BEAT-03/04/07/08).
- The serialized cliffhanger is on the final beat, and narrative structure
  fields (goal/obstacle/choice/action/consequence/causal chain) exist for each
  segment.
- The English trope library holds >= 5 culture-adapted tropes with unique ids
  and required fields, and the loader rejects a short library.
- Artifacts validate against ``schemas/artifacts/beat_map.schema.json``.

Run: pytest tests/contracts/test_shortdrama_beat.py -q
"""

from __future__ import annotations

import copy
import math

import pytest
import yaml
from jsonschema import Draft202012Validator

from lib import beat_map as bm

REPO_ROOT = bm.Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "artifacts" / "beat_map.schema.json"
TROPE_PATH = REPO_ROOT / "data" / "tropes" / "english_tropes.yaml"


@pytest.fixture(scope="module")
def schema_validator():
    import json

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# BEAT-01/05: deterministic duration scaling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [90.0, 120.0, 180.0])
def test_scaling_table_covers_anchor_durations(duration, schema_validator):
    bmap = bm.build_beat_map(duration)

    schema_validator.validate(bmap)
    assert bm.validate_beat_map(bmap) == []

    assert bmap["duration_s"] == pytest.approx(duration)
    assert bmap["scaling_rule"] == bm.SCALING_RULE_ID
    assert bmap["segments"][0]["start_s"] == pytest.approx(0.0)
    assert bmap["segments"][-1]["end_s"] == pytest.approx(duration)
    assert bmap["beats"][0]["start_s"] == pytest.approx(0.0)
    assert bmap["beats"][-1]["end_s"] == pytest.approx(duration)


@pytest.mark.parametrize("duration", [90.0, 120.0, 180.0])
def test_timestamps_are_continuous(duration):
    bmap = bm.build_beat_map(duration)
    for items in (bmap["segments"], bmap["beats"]):
        for prev, nxt in zip(items, items[1:]):
            assert prev["end_s"] == pytest.approx(nxt["start_s"], abs=1e-3)
        for item in items:
            assert item["end_s"] >= item["start_s"]


def test_default_120s_produces_seven_segments():
    bmap = bm.build_beat_map()
    assert bmap["duration_s"] == pytest.approx(bm.DEFAULT_DURATION_S)
    assert len(bmap["segments"]) == 7
    assert [s["label"] for s in bmap["segments"]] == list(bm.SEGMENT_LABELS)


def test_scaling_function_is_duration_dependent():
    counts = {d: bm.min_hook_candidates(d) for d in (90, 120, 180)}
    assert counts == {90: 4, 120: 5, 180: 8}
    # 120s is the documented default: >= 5 hook candidates.
    assert counts[120] >= 5
    # Segment ratios change with duration rather than naively stretching.
    assert bm.segment_ratios(90) != bm.segment_ratios(180)
    assert sum(bm.segment_ratios(120)) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("duration", [45.0, 100.0, 150.0, 240.0])
def test_other_durations_follow_declared_function(duration):
    bmap = bm.build_beat_map(duration)
    assert bm.validate_beat_map(bmap) == []
    hooks = [b for b in bmap["beats"] if b["hook_candidate"]]
    assert len(hooks) >= bm.min_hook_candidates(duration)
    assert bmap["metadata"]["scaling"]["min_hook_candidates"] == bm.min_hook_candidates(duration)


def test_build_is_deterministic():
    assert bm.build_beat_map(120) == bm.build_beat_map(120)
    assert bm.build_beat_map(90) == bm.build_beat_map(90)


@pytest.mark.parametrize("bad", [0, 10, 601, -5, float("nan"), "abc"])
def test_invalid_duration_rejected(bad):
    with pytest.raises(bm.BeatMapError):
        bm.build_beat_map(bad)


# ---------------------------------------------------------------------------
# BEAT-03/04: hook density and the 0-3s hard window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [90.0, 120.0, 180.0])
def test_hook_inside_zero_to_three_second_window(duration):
    bmap = bm.build_beat_map(duration)
    opening = [
        b
        for b in bmap["beats"]
        if b["hook_candidate"] and b["start_s"] <= 1e-3 and b["end_s"] <= bm.HOOK_WINDOW_S
    ]
    assert opening, "first hook candidate must sit inside 0-3s"


@pytest.mark.parametrize("duration,minimum", [(90.0, 4), (120.0, 5), (180.0, 8)])
def test_hook_density_scales_with_duration(duration, minimum):
    bmap = bm.build_beat_map(duration)
    hooks = [b for b in bmap["beats"] if b["hook_candidate"]]
    assert len(hooks) == minimum
    assert len(hooks) >= bm.min_hook_candidates(duration)
    assert bm.check_hook_density(bmap) == []


def test_turns_are_spaced_within_declared_interval():
    for duration in (90.0, 120.0, 180.0):
        bmap = bm.build_beat_map(duration)
        turns = [b["start_s"] for b in bmap["beats"] if b["turn"]]
        assert len(turns) >= 2
        bound = bm.turn_interval_s(duration) + bm.TARGET_BEAT_S
        for prev, nxt in zip(turns, turns[1:]):
            assert 0 < nxt - prev <= bound + 1e-3


def test_hook_markers_are_not_claimed_as_valid():
    bmap = bm.build_beat_map(120)
    confirmation = bmap["metadata"]["human_confirmation"]
    assert confirmation["hook_valid"] == "unconfirmed"
    assert bmap["metadata"]["narrative"]["human_review_required"] is True


# ---------------------------------------------------------------------------
# BEAT-07: serialized cliffhanger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [90.0, 120.0, 180.0])
def test_last_beat_is_cliffhanger(duration):
    bmap = bm.build_beat_map(duration)
    assert bmap["beats"][-1]["cliffhanger"] is True
    assert not any(b["cliffhanger"] for b in bmap["beats"][:-1])
    last_segment = bmap["segments"][-1]
    assert last_segment["label"] == "RESOLUTION"
    assert last_segment["end_s"] == pytest.approx(bmap["beats"][-1]["end_s"])


# ---------------------------------------------------------------------------
# BEAT-08: dialogue-driven narrative structure fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [90.0, 120.0, 180.0])
def test_narrative_scaffold_covers_goal_obstacle_choice_action_consequence(duration):
    bmap = bm.build_beat_map(duration)
    narrative = bmap["metadata"]["narrative"]
    for field in ("character_goal", "obstacle", "choice", "action", "consequence"):
        assert field in narrative
    assert narrative["human_review_required"] is True
    chain = narrative["causal_chain"]
    assert [entry["segment_id"] for entry in chain] == [
        s["segment_id"] for s in bmap["segments"]
    ]
    for entry in chain:
        for field in ("cause", "choice", "action", "consequence"):
            assert field in entry


# ---------------------------------------------------------------------------
# Machine validation catches tampering
# ---------------------------------------------------------------------------


def test_validation_catches_missing_hook():
    bmap = copy.deepcopy(bm.build_beat_map(120))
    for beat in bmap["beats"]:
        beat["hook_candidate"] = False
    problems = bm.check_hook_density(bmap)
    assert any("hook_candidate" in p for p in problems)


def test_validation_catches_timeline_gap_and_missing_cliffhanger():
    bmap = copy.deepcopy(bm.build_beat_map(120))
    bmap["segments"][1]["start_s"] += 5
    bmap["beats"][-1]["cliffhanger"] = False
    problems = bm.validate_beat_map(bmap)
    assert any("gap/overlap" in p for p in problems)
    assert any("cliffhanger" in p for p in problems)


def test_validation_catches_missing_narrative_fields():
    bmap = copy.deepcopy(bm.build_beat_map(120))
    bmap["metadata"]["narrative"].pop("obstacle")
    problems = bm.validate_beat_map(bmap)
    assert any("obstacle" in p for p in problems)


def test_schema_rejects_out_of_range_duration():
    bmap = copy.deepcopy(bm.build_beat_map(120))
    bmap["duration_s"] = 0
    with pytest.raises(Exception):
        bm.validate_schema(bmap)


# ---------------------------------------------------------------------------
# BEAT-06: English trope library
# ---------------------------------------------------------------------------


def test_trope_library_has_minimum_count_and_required_fields():
    tropes = bm.load_tropes()
    assert len(tropes) >= bm.MIN_TROPE_COUNT
    assert len(tropes) >= 5

    ids = [t["id"] for t in tropes]
    assert len(ids) == len(set(ids))
    for trope in tropes:
        for field in bm.REQUIRED_TROPE_FIELDS:
            assert trope[field], f"{trope['id']} missing {field}"
        assert isinstance(trope["hook_devices"], list) and trope["hook_devices"]
        assert trope["cultural_notes"]
        assert trope["sample_premise_en"]
    # Originality policy lives beside the library (idea layer only).
    raw = yaml.safe_load(TROPE_PATH.read_text(encoding="utf-8"))
    assert "idea layer" in raw["originality_policy"]["borrow"]
    assert "no substantial similarity" in raw["originality_policy"]["avoid"]


def test_trope_loader_rejects_short_library(tmp_path):
    short = tmp_path / "short.yaml"
    short.write_text(
        yaml.safe_dump(
            {
                "tropes": [
                    {
                        "id": "one",
                        "name": "One",
                        "summary": "s",
                        "conflict_engine": "c",
                        "hook_devices": ["h"],
                        "cultural_notes": "n",
                        "sample_premise_en": "p",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(bm.BeatMapError):
        bm.load_tropes(short)


def test_trope_loader_rejects_missing_fields(tmp_path):
    tropes = [
        {
            "id": f"t{i}",
            "name": f"T{i}",
            "summary": "s",
            "conflict_engine": "c",
            "hook_devices": ["h"],
            "cultural_notes": "n",
            "sample_premise_en": "p",
        }
        for i in range(5)
    ]
    tropes[2].pop("cultural_notes")
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump({"tropes": tropes}), encoding="utf-8")
    with pytest.raises(bm.BeatMapError):
        bm.load_tropes(path)


# ---------------------------------------------------------------------------
# Sanity: scaling function is coherent beyond the anchors
# ---------------------------------------------------------------------------


def test_anchor_profiles_are_normalised():
    for duration, ratios in bm._ANCHOR_RATIOS.items():
        assert len(ratios) == len(bm.SEGMENT_LABELS)
        assert sum(ratios) == pytest.approx(1.0)
    assert all(math.isfinite(v) for v in bm.segment_ratios(137))