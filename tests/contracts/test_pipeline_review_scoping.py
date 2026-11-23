"""v2.8 GATE-D3 — children's-education pipelines opt into blocking review.

`animated-explainer` and `character-animation` are the pipelines for the
children's-education direction. They get blocking review now; the two boundary
gates stay off until their manifests guarantee provenance and generation blocks.
"""

from __future__ import annotations

import pytest

from lib.checkpoint import CheckpointValidationError, _artifact_gate_stages, \
    _generation_block_gate, _review_blocking, write_checkpoint
from lib.pipeline_loader import load_pipeline

TARGETS = ("animated-explainer", "character-animation")


def _script() -> dict:
    return {
        "version": "1.0",
        "title": "T",
        "total_duration_seconds": 10,
        "sections": [{"id": "s1", "text": "Hi.", "start_seconds": 0, "end_seconds": 10}],
    }


class TestDeclarations:
    @pytest.mark.parametrize("pipeline", TARGETS)
    def test_target_pipelines_block_on_critical_review(self, pipeline):
        assert _review_blocking(pipeline) is True

    def test_unrelated_pipeline_stays_advisory(self):
        assert _review_blocking("cinematic") is False

    @pytest.mark.parametrize("pipeline", TARGETS)
    def test_boundary_gates_are_not_enabled(self, pipeline):
        assert _artifact_gate_stages(pipeline) == frozenset()
        assert _generation_block_gate(pipeline) is False

    @pytest.mark.parametrize("pipeline", TARGETS)
    def test_manifest_still_validates(self, pipeline):
        manifest = load_pipeline(pipeline)
        assert manifest["review_blocking"] is True


class TestCheckpointEnforcement:
    @pytest.mark.parametrize("pipeline", TARGETS)
    def test_critical_cannot_be_written_completed(self, tmp_path, pipeline):
        review = {"findings": [{"issue": "unsafe imagery for children",
                                "severity": "critical"}]}
        with pytest.raises(CheckpointValidationError, match="REVIEW VIOLATION"):
            write_checkpoint(
                tmp_path, "proj", "script", "completed",
                artifacts={"script": _script()},
                pipeline_type=pipeline, human_approved=True, review=review,
            )

    @pytest.mark.parametrize("pipeline", TARGETS)
    def test_suggestion_is_allowed(self, tmp_path, pipeline):
        review = {"findings": [{"issue": "could be brighter", "severity": "suggestion"}]}
        try:
            write_checkpoint(
                tmp_path, "proj", "script", "completed",
                artifacts={"script": _script()},
                pipeline_type=pipeline, human_approved=True, review=review,
            )
        except CheckpointValidationError as error:
            assert "REVIEW VIOLATION" not in str(error), error