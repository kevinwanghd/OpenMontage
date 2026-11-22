"""v2.7 GATE-05 — generate in blocks, cut into beats, chain only within a scene.

The 2026-11-17 cut generated one clip per 2-6s shot, which is why identity drifted
between neighbouring clips of the same scene and the result read as a montage of
fragments. The plan had no way to say "this 20s is one generation, cut it five ways".
"""

from __future__ import annotations

import pytest

from lib import generation_blocks as gb


def _plan(**scene) -> dict:
    base = {"id": "sc1", "type": "character_scene",
            "description": "kitchen", "start_seconds": 0, "end_seconds": 20}
    base.update(scene)
    return {"version": "1.0", "scenes": [base], "shots": []}


class TestBlockValidation:
    def test_valid_block_and_beats_pass(self):
        plan = _plan(generation_block_seconds=20,
                     edit_beats=[{"duration_s": 5}, {"duration_s": 5},
                                 {"duration_s": 5}, {"duration_s": 5}])
        assert gb.validate_scene_plan(plan) == []

    def test_legacy_scene_without_block_is_skipped(self):
        assert gb.validate_scene_plan(_plan()) == []

    def test_block_outside_range_blocks(self):
        plan = _plan(generation_block_seconds=45, edit_beats=[{"duration_s": 5}] * 9)
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("outside" in issue for issue in issues)

    def test_block_too_short_blocks(self):
        plan = _plan(generation_block_seconds=4, edit_beats=[{"duration_s": 4}])
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("outside" in issue for issue in issues)

    def test_beats_must_consume_the_block(self):
        plan = _plan(generation_block_seconds=20, edit_beats=[{"duration_s": 5}])
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("edit beats total" in issue for issue in issues)

    def test_beat_outside_range_blocks(self):
        plan = _plan(generation_block_seconds=10,
                     edit_beats=[{"duration_s": 1}, {"duration_s": 9}])
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("edit beat" in issue for issue in issues)

    def test_beats_without_block_blocks(self):
        plan = _plan(edit_beats=[{"duration_s": 4}])
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("without generation_block_seconds" in issue for issue in issues)

    def test_block_without_beats_blocks(self):
        plan = _plan(generation_block_seconds=10)
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("no edit_beats" in issue for issue in issues)

    def test_non_numeric_duration_blocks(self):
        plan = _plan(generation_block_seconds=10, edit_beats=[{"duration_s": "five"}])
        issues = [b["issue"] for b in gb.validate_scene_plan(plan)]
        assert any("numeric duration" in issue for issue in issues)


class TestChaining:
    def test_same_scene_continues_from_tail_frame(self):
        assert gb.chain_mode("sc1", "sc1") == "tail_frame"

    def test_scene_change_starts_fresh(self):
        assert gb.chain_mode("sc1", "sc2") == "fresh_start"

    def test_first_block_starts_fresh(self):
        assert gb.chain_mode(None, "sc1") == "fresh_start"

    def test_tail_frame_across_scenes_blocks(self):
        sequence = [
            {"scene_id": "sc1", "starts_from": "fresh_start"},
            {"scene_id": "sc2", "starts_from": "tail_frame"},
        ]
        issues = gb.validate_chaining(sequence)
        assert len(issues) == 1
        assert issues[0]["severity"] == "P0"

    def test_continuation_within_scene_is_allowed(self):
        sequence = [
            {"scene_id": "sc1", "starts_from": "fresh_start"},
            {"scene_id": "sc1", "starts_from": "tail_frame"},
            {"scene_id": "sc1", "starts_from": "tail_frame"},
        ]
        assert gb.validate_chaining(sequence) == []

    def test_fresh_start_mid_scene_blocks(self):
        """Restarting inside a scene throws away the continuity the block bought."""
        sequence = [
            {"scene_id": "sc1", "starts_from": "fresh_start"},
            {"scene_id": "sc1", "starts_from": "fresh_start"},
        ]
        assert len(gb.validate_chaining(sequence)) == 1

class TestWiring:
    """The validator was dead code until a stage actually called it."""

    def _project(self, tmp_path, scenes):
        import json
        from pathlib import Path

        project = Path(tmp_path) / "proj"
        (project / "artifacts").mkdir(parents=True)
        (project / "artifacts" / "scene_plan.json").write_text(
            json.dumps({"version": "1.0", "scenes": scenes}), encoding="utf-8")
        return project

    def test_qa_review_blocks_bad_beats(self, tmp_path):
        from shortdrama import qa

        project = self._project(tmp_path, [{
            "id": "s1", "type": "generated", "description": "d",
            "start_seconds": 0, "end_seconds": 20,
            "generation_block_seconds": 10,
            "edit_beats": [{"duration_s": 3}],
        }])
        report = qa.review(project, "scene_plan", probe=lambda path: None)
        assert any(b["issue"] == "generation block" for b in report["blocking"])

    def test_legacy_plan_only_warns(self, tmp_path):
        from shortdrama import qa

        project = self._project(tmp_path, [{
            "id": "s1", "type": "generated", "description": "d",
            "start_seconds": 0, "end_seconds": 20,
        }])
        report = qa.review(project, "scene_plan", probe=lambda path: None)
        assert not any(b["issue"] == "generation block" for b in report["blocking"])
        assert any("no generation block" in w["issue"] for w in report["warnings"])

    def test_checkpoint_refuses_a_completed_plan_with_bad_beats(self, tmp_path):
        import pytest
        from lib.checkpoint import CheckpointValidationError, write_checkpoint

        plan = {"version": "1.0", "scenes": [{
            "id": "s1", "type": "generated", "description": "d",
            "start_seconds": 0, "end_seconds": 20,
            "generation_block_seconds": 10,
            "edit_beats": [{"duration_s": 30}],
        }]}
        self._project(tmp_path, plan["scenes"])

        with pytest.raises(CheckpointValidationError, match="GENERATION BLOCK VIOLATION"):
            write_checkpoint(
                tmp_path, "proj", "scene_plan", "completed",
                artifacts={"scene_plan": plan}, pipeline_type="short-drama",
                human_approved=True,
            )
