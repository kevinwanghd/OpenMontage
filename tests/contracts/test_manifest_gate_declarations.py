"""v2.8 GATE-D1 — a gate applies because the manifest says so, not because the
stage name happens to be short-drama.

Before this, `checkpoint.py` hard-coded `pipeline_type == "short-drama"` for
both the artifact boundary gate and the generation-block gate. Every other
pipeline silently ran with no boundary protection at all.
"""

from __future__ import annotations

import pytest

from lib import checkpoint


class TestDeclarations:
    def test_short_drama_declares_its_stages(self):
        assert checkpoint._artifact_gate_stages("short-drama") == frozenset(
            {"keyframe", "shot_gen", "assembly"})

    def test_short_drama_declares_the_block_gate(self):
        assert checkpoint._generation_block_gate("short-drama") is True

    def test_undeclared_pipeline_gets_no_gate(self):
        assert checkpoint._artifact_gate_stages("animated-explainer") == frozenset()
        assert checkpoint._generation_block_gate("animated-explainer") is False

    def test_no_pipeline_type_means_no_gate(self):
        assert checkpoint._artifact_gate_stages(None) == frozenset()
        assert checkpoint._generation_block_gate(None) is False


class TestFailureConvention:
    def test_unknown_pipeline_name_raises(self):
        with pytest.raises(checkpoint.CheckpointValidationError):
            checkpoint._artifact_gate_stages("no-such-pipeline")

    def test_corrupt_manifest_degrades(self, monkeypatch, caplog):
        import lib.pipeline_loader as loader

        def _boom(name, *args, **kwargs):
            raise ValueError("schema violation")

        monkeypatch.setattr(loader, "load_pipeline_readonly", _boom)
        with caplog.at_level("WARNING"):
            assert checkpoint._artifact_gate_stages("short-drama") == frozenset()
            assert checkpoint._generation_block_gate("short-drama") is False
        assert "artifact gate" in caplog.text


class TestCheckpointUsesTheDeclaration:
    def test_undeclared_pipeline_does_not_run_the_artifact_gate(self, tmp_path, monkeypatch):
        """A completed `assets` checkpoint outside short-drama must not validate."""
        import lib.artifact_gate as artifact_gate

        calls: list = []

        def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("artifact gate must not run for this pipeline")

        monkeypatch.setattr(artifact_gate, "validate", _spy)
        try:
            checkpoint.write_checkpoint(
                tmp_path, "proj", "assets", "completed",
                artifacts={"asset_manifest": {"version": "1.0", "assets": []}},
                pipeline_type="animated-explainer",
            )
        except checkpoint.CheckpointValidationError as error:
            assert "ARTIFACT VIOLATION" not in str(error)
            assert "GENERATION BLOCK VIOLATION" not in str(error)
        assert calls == []