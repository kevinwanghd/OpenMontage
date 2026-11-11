"""T1 contract tests for the short-drama schema layer (requirements v2.4, ORC-07).

Guards:
- The 7 new drama schemas are valid JSON Schema (draft 2020-12) and accept/reject
  the right instances.
- scene_plan gains an OPTIONAL additive shots[] layer without breaking legacy
  scenes[]-only artifacts (backward compatibility).
- The closed legacy contracts (character_design, review) are NOT extended with
  drama fields (F5: new schemas, do not break existing pipelines).
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "artifacts"

NEW_SCHEMAS = [
    "drama_character",
    "review_decision",
    "beat_map",
    "episode_manifest",
    "shot_state",
    "submission_ledger",
    "capability_catalog",
]


def _load(name):
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", NEW_SCHEMAS)
def test_new_schema_is_valid_json_schema(name):
    Draft202012Validator.check_schema(_load(name))


POSITIVE = {
    "drama_character": {
        "version": "1.0", "character_id": "ch_ling", "name": "Ling",
        "canonical_description": "26yo woman, oval face, ear-length black hair",
        "wardrobe": [{"path": "ref/front.png", "angle": "front"}],
        "face_embedding_ref": "identity/ch_ling/insightface_v1", "voice_id": "en_f_calm",
    },
    "review_decision": {
        "version": "1.0", "review_id": "r1", "project": "p", "episode": "ep01",
        "stage": "shot_gen", "slot": "sh_012", "event_type": "decision",
        "candidates": ["assets/shots/sh_012_v1.mp4"], "face_similarity": 0.91,
        "decision": {"action": "accept", "decided_at": "2026-11-11T00:00:00Z", "decided_by": "owner"},
    },
    "beat_map": {
        "version": "1.0", "duration_s": 120,
        "segments": [{"segment_id": "s1", "label": "HOOK", "start_s": 0, "end_s": 3}],
        "beats": [{"beat_id": "b1", "start_s": 0, "end_s": 3, "hook_candidate": True}],
    },
    "episode_manifest": {
        "version": "1.0", "series_id": "s", "episode_id": "ep01", "project_id": "p",
        "target_platform": "tiktok", "duration_s": 120,
        "review_policy": {"mode": "sampled", "detail_ratio": 0.35, "face_sim_threshold": 0.75},
    },
    "shot_state": {
        "version": "1.0", "shot_id": "sh_001", "episode_id": "ep01",
        "state": "planned", "event_seq": 0, "reject_count": 0,
    },
    "submission_ledger": {
        "version": "1.0", "entry_id": "e1", "episode_id": "ep01", "intent": "shot_gen",
        "input_hash": "abc", "provider": "molica", "billing_state": "pending", "currency": "CNY",
    },
    "capability_catalog": {
        "version": "1.0",
        "entries": [{"model": "wan3.0-video", "operation": "image_to_video",
                     "verification_status": "MOCKED", "output_resolutions": ["480p"],
                     "first_frame_conditioning": "unknown"}],
    },
}


@pytest.mark.parametrize("name", NEW_SCHEMAS)
def test_positive_example_validates(name):
    Draft202012Validator(_load(name)).validate(POSITIVE[name])


NEGATIVE = {
    "drama_character": {"version": "1.0", "name": "Ling", "canonical_description": "x"},  # missing character_id
    "review_decision": {**POSITIVE["review_decision"],
                        "decision": {"action": "approve", "decided_at": "t", "decided_by": "owner"}},  # bad action
    "shot_state": {**POSITIVE["shot_state"], "state": "done"},  # bad state enum
    "submission_ledger": {**POSITIVE["submission_ledger"], "billing_state": "paid"},  # bad billing enum
    "episode_manifest": {**POSITIVE["episode_manifest"], "target_platform": "douyin"},  # bad platform
    "beat_map": {**POSITIVE["beat_map"], "duration_s": 0},  # below minimum
    "capability_catalog": {"version": "1.0", "entries": [{"model": "m", "operation": "o"}]},  # missing verification_status
}


@pytest.mark.parametrize("name", list(NEGATIVE))
def test_negative_example_rejected(name):
    validator = Draft202012Validator(_load(name))
    assert not validator.is_valid(NEGATIVE[name]), f"{name} should reject invalid instance"


# --- scene_plan backward compatibility (additive shots[] layer) ---

def _scene_plan_legacy():
    return {"version": "1.0", "scenes": [
        {"id": "s1", "type": "generated", "description": "d", "start_seconds": 0, "end_seconds": 5}]}


def test_scene_plan_legacy_scenes_only_still_valid():
    Draft202012Validator(_load("scene_plan")).validate(_scene_plan_legacy())


def test_scene_plan_drama_shots_valid():
    doc = _scene_plan_legacy()
    doc["shots"] = [{"shot_id": "sh_001", "duration_s": 5,
                     "shot_language": {"shot_size": "close_up", "camera_movement": "static"},
                     "dialogue": {"speaker": "ch_ling", "text": "You lied?"},
                     "needs_lipsync": True, "reference_character_ids": ["ch_ling"], "candidates": 2}]
    Draft202012Validator(_load("scene_plan")).validate(doc)


def test_scene_plan_shot_missing_duration_rejected():
    doc = _scene_plan_legacy()
    doc["shots"] = [{"shot_id": "sh_001"}]  # missing required duration_s
    assert not Draft202012Validator(_load("scene_plan")).is_valid(doc)


def test_scene_plan_shot_bad_shot_language_enum_rejected():
    doc = _scene_plan_legacy()
    doc["shots"] = [{"shot_id": "sh_001", "duration_s": 5, "shot_language": {"shot_size": "bogus"}}]
    assert not Draft202012Validator(_load("scene_plan")).is_valid(doc)


# --- closed legacy contracts must NOT be extended with drama fields (F5) ---

def test_legacy_character_design_not_extended():
    schema = _load("character_design")
    Draft202012Validator.check_schema(schema)
    assert "face_embedding" not in schema.get("properties", {})
    assert "canonical_description" not in schema.get("properties", {})


def test_legacy_review_schema_not_extended():
    schema = _load("review")
    Draft202012Validator.check_schema(schema)
    # Reviewer-agent findings schema, not the human shot-review decision schema
    assert "reason_code" not in schema.get("properties", {})
    assert "supersedes" not in schema.get("properties", {})