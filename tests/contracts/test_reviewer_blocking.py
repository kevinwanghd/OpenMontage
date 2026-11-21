"""v2.7 GATE-01 — the reviewer's P0 findings must actually block a stage.

Context: the 2026-11-17 short-drama delivery was accepted with no lipsync at
all and a male voice track on two female characters. Two authority documents
disagreed about whether the reviewer blocks (`AGENT_GUIDE.md` said "advisory,
never directly blocks"; `pipeline_defs/short-drama.yaml` said a REJECT is
"regenerated, never accepted"), and no code enforced either reading.

These tests pin the resolution: P0 blocks, P1 does not, and the two documents
can no longer drift back into contradiction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.checkpoint import CheckpointValidationError, write_checkpoint
from shortdrama import qa

ROOT = Path(__file__).resolve().parents[2]


def _script() -> dict:
    return {
        "version": "1.0",
        "title": "T",
        "total_duration_seconds": 10,
        "sections": [{"id": "s1", "text": "Hi.", "start_seconds": 0, "end_seconds": 10}],
    }


def _write(tmp_path, review, status="completed", pipeline_type="short-drama"):
    return write_checkpoint(
        tmp_path,
        "proj",
        "script",
        status,
        artifacts={"script": _script()},
        pipeline_type=pipeline_type,
        human_approved=True,
        review=review,
    )


def _assert_not_review_blocked(
    tmp_path, review, status="completed", pipeline_type="short-drama"
):
    """The write may legitimately fail on pipeline prerequisites — but never on
    the P0 review gate, which is the only thing under test here."""
    try:
        write_checkpoint(
            tmp_path,
            "proj",
            "script",
            status,
            artifacts={"script": _script()},
            pipeline_type=pipeline_type,
            human_approved=True,
            review=review,
        )
    except CheckpointValidationError as error:
        assert "REVIEW VIOLATION" not in str(error), error


class TestCheckpointP0Gate:
    def test_p0_finding_blocks_completed(self, tmp_path):
        review = {
            "stage": "shot_gen",
            "blocking": [
                {
                    "shot": "m1_002",
                    "issue": "dialogue shot has no audio (no lipsync)",
                    "severity": "P0",
                }
            ],
        }
        with pytest.raises(CheckpointValidationError, match="REVIEW VIOLATION"):
            _write(tmp_path, review)

    def test_p0_blocks_even_with_human_approval(self, tmp_path):
        """Approval is not a licence to ship a P0; the fix must happen first."""
        review = {"findings": [{"issue": "female character voiced by a male track", "severity": "P0"}]}
        with pytest.raises(CheckpointValidationError, match="REVIEW VIOLATION"):
            _write(tmp_path, review)

    def test_p1_only_does_not_block(self, tmp_path):
        review = {
            "stage": "assembly",
            "warnings": [{"issue": "subtitle sits low in the safe area", "severity": "P1"}],
        }
        _assert_not_review_blocked(tmp_path, review)

    def test_unseverity_findings_do_not_block(self, tmp_path):
        """Legacy verdicts without severity must not become phantom blockers."""
        review = {"blocking": [{"issue": "older format finding"}]}
        _assert_not_review_blocked(tmp_path, review)

    def test_p0_does_not_block_non_completed_states(self, tmp_path):
        """awaiting_human is exactly where a P0 should be surfaced."""
        review = {"blocking": [{"issue": "no lipsync", "severity": "P0"}]}
        _assert_not_review_blocked(tmp_path, review, status="awaiting_human")

    def test_p0_is_advisory_outside_blocking_pipelines(self, tmp_path):
        """AGENT_GUIDE scopes P0 blocking to pipelines whose manifest declares
        blocking semantics; every other pipeline keeps the advisory default."""
        review = {"findings": [{"issue": "no lipsync", "severity": "P0"}]}
        _assert_not_review_blocked(
            tmp_path, review, pipeline_type="animated-explainer"
        )


class TestQaSeverityTagging:
    def _project(self, tmp_path: Path, payload: dict) -> Path:
        (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
        import json

        (tmp_path / "artifacts" / "qa_visual.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return tmp_path

    def test_visual_blocking_becomes_p0(self, tmp_path):
        project = self._project(
            tmp_path,
            {"verdict": "reject", "blocking": [{"issue": "no lipsync"}], "warnings": []},
        )
        result = qa.review_visual(project)
        assert result["blocking"][0]["severity"] == "P0"

    def test_visual_warnings_become_p1(self, tmp_path):
        project = self._project(
            tmp_path, {"verdict": "pass", "blocking": [], "warnings": [{"issue": "flat light"}]}
        )
        result = qa.review_visual(project)
        assert result["warnings"][0]["severity"] == "P1"

    def test_verdict_reject_without_detail_is_still_p0(self, tmp_path):
        project = self._project(tmp_path, {"verdict": "reject", "blocking": [], "warnings": []})
        result = qa.review_visual(project)
        assert result["blocking"][0]["severity"] == "P0"


class TestDocumentsAgree:
    """The contradiction that caused the incident must not come back."""

    def test_guide_declares_the_short_drama_exception(self):
        guide = (ROOT / "AGENT_GUIDE.md").read_text(encoding="utf-8")
        assert "Short-drama exception (binding)" in guide
        assert "cannot be written as `status='completed'`" in guide

    def test_guide_no_longer_claims_the_reviewer_never_blocks(self):
        guide = (ROOT / "AGENT_GUIDE.md").read_text(encoding="utf-8")
        assert "advisory, never directly blocks progression." not in guide

    def test_manifest_still_requires_a_pass(self):
        manifest = (ROOT / "pipeline_defs" / "short-drama.yaml").read_text(encoding="utf-8")
        assert "shortdrama-reviewer PASS verdict" in manifest
        assert "never accepted" in manifest