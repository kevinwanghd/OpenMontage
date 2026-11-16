"""Contract tests for the director_brief artifact (v2.5 DIR-01).

The director brief is the creative contract every downstream agent reads
(DIR-06). Its seven fields are all required: a blank field is an unspecified
director intent, so the schema must reject it rather than let the pipeline
invent an arc.
"""
import pytest

from lib.checkpoint import (
    CheckpointValidationError,
    SUPPLEMENTARY_ARTIFACTS,
    _validate_artifacts_for_stage,
)
from schemas.artifacts import ARTIFACT_NAMES, validate_artifact

SEVEN_FIELDS = (
    "hook_strategy",
    "emotional_arc",
    "dramatic_question",
    "pacing_philosophy",
    "visual_tone",
    "sound_emotional_curve",
    "audience_emotional_trigger",
)

VALID_BRIEF = {
    "version": "1.0",
    "title": "The Voicemail",
    "hook": "A mother reheats soup for a daughter who will not stay.",
    "key_points": ["She calls", "She cannot stay", "He answers next month"],
    "tone": "restrained, aching",
    "style": "clean-professional",
    "target_platform": "tiktok",
    "target_duration_seconds": 35,
}


def _director_brief(**overrides) -> dict:
    data = {"version": "1.0"}
    data.update({field: f"{field} specified by the director" for field in SEVEN_FIELDS})
    data.update(overrides)
    return data


def test_artifact_is_registered_and_supplementary():
    assert "director_brief" in ARTIFACT_NAMES
    assert "director_brief" in SUPPLEMENTARY_ARTIFACTS


def test_valid_director_brief_passes_schema():
    validate_artifact("director_brief", _director_brief())


@pytest.mark.parametrize("field", SEVEN_FIELDS)
def test_missing_field_is_rejected(field):
    data = _director_brief()
    del data[field]
    with pytest.raises(Exception):
        validate_artifact("director_brief", data)


def test_blank_field_is_rejected():
    with pytest.raises(Exception):
        validate_artifact("director_brief", _director_brief(emotional_arc=""))


def test_unknown_field_is_rejected():
    with pytest.raises(Exception):
        validate_artifact("director_brief", _director_brief(shots="free text"))


def test_checkpoint_validates_director_brief_alongside_brief():
    _validate_artifacts_for_stage(
        "idea",
        "completed",
        {"brief": VALID_BRIEF, "director_brief": _director_brief()},
    )


def test_checkpoint_rejects_invalid_director_brief():
    bad = _director_brief()
    del bad["audience_emotional_trigger"]
    with pytest.raises(CheckpointValidationError, match="director_brief"):
        _validate_artifacts_for_stage(
            "idea",
            "completed",
            {"brief": VALID_BRIEF, "director_brief": bad},
        )


def test_legacy_idea_checkpoint_without_director_brief_still_valid():
    """Backward compatibility: director_brief is supplementary, not required.

    Projects checkpointed before v2.5 only carry ``brief`` at the idea stage.
    Checkpoint validation enforces CANONICAL_STAGE_ARTIFACTS, so an artifact
    added to SUPPLEMENTARY_ARTIFACTS must not invalidate those checkpoints or
    require a data migration. This test fails if director_brief is ever
    promoted to a hard requirement without a migration story.
    """
    _validate_artifacts_for_stage("idea", "completed", {"brief": VALID_BRIEF})