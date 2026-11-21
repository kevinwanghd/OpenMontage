"""v2.7 GATE-04 — a review without evidence is not a review.

The 2026-11-17 cut was accepted on seven hand-picked stills. Nothing required a
contact sheet, per-timepoint frames, the audio track, or a transcript, so the
reviewer had no way to see that mouths never moved.
"""

from __future__ import annotations

import json

import pytest

from lib import evidence_pack


def _pack(tmp_path, shot="m1_002", *, frames=12, include_frames=True,
          include_audio=True, include_transcript=True, bind=True):
    project = tmp_path / "proj"
    directory = evidence_pack.pack_dir(project, shot)
    (directory / "frames").mkdir(parents=True, exist_ok=True)
    (directory / "contact_sheet.jpg").write_bytes(b"sheet")
    if include_frames:
        for point in evidence_pack.FRAME_TIMEPOINTS:
            (directory / "frames" / f"{point:03d}.jpg").write_bytes(b"f")
    if include_audio:
        (directory / "audio.wav").write_bytes(b"wav")
    if include_transcript:
        (directory / "transcript.json").write_text('{"text": "hi"}', encoding="utf-8")
    if bind:
        (directory / "pack.json").write_text(
            json.dumps({"shot_id": shot, "contact_sheet_frames": frames}), encoding="utf-8"
        )
    return project


class TestCompleteness:
    def test_complete_pack_passes(self, tmp_path):
        project = _pack(tmp_path)
        assert evidence_pack.validate(project, "m1_002")["blocking"] == []

    def test_missing_pack_blocks(self, tmp_path):
        project = tmp_path / "proj"
        issues = [b["issue"] for b in evidence_pack.validate(project, "m1_002")["blocking"]]
        assert "evidence pack missing" in issues

    @pytest.mark.parametrize("missing", ["audio", "transcript"])
    def test_missing_audio_or_transcript_blocks(self, tmp_path, missing):
        project = _pack(tmp_path, **{f"include_{missing}": False})
        issues = [b["issue"] for b in evidence_pack.validate(project, "m1_002")["blocking"]]
        assert any(missing in issue for issue in issues)

    def test_missing_timepoint_frame_blocks(self, tmp_path):
        project = _pack(tmp_path)
        (evidence_pack.pack_dir(project, "m1_002") / "frames" / "050.jpg").unlink()
        issues = [b["issue"] for b in evidence_pack.validate(project, "m1_002")["blocking"]]
        assert any("050.jpg" in issue for issue in issues)

    def test_thin_contact_sheet_blocks(self, tmp_path):
        project = _pack(tmp_path, frames=6)
        issues = [b["issue"] for b in evidence_pack.validate(project, "m1_002")["blocking"]]
        assert any("contact sheet" in issue for issue in issues)

    def test_unbound_pack_warns(self, tmp_path):
        project = _pack(tmp_path, bind=False)
        report = evidence_pack.validate(project, "m1_002")
        assert report["blocking"] == []
        assert "pack.json" in report["warnings"][0]["issue"]


class TestBinding:
    def test_bind_records_artifact_hash(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        clip = project / "clip.mp4"
        clip.write_bytes(b"clip-bytes")
        manifest_path = evidence_pack.bind(project, "m1_002", clip)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        import hashlib

        assert manifest["artifact_sha256"] == hashlib.sha256(b"clip-bytes").hexdigest()

    def test_bind_is_atomic(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        clip = project / "clip.mp4"
        clip.write_bytes(b"x")
        evidence_pack.bind(project, "m1_002", clip)
        directory = evidence_pack.pack_dir(project, "m1_002")
        assert list(directory.glob("*.tmp")) == []


class TestFindingCitation:
    def test_finding_without_evidence_is_rejected(self, tmp_path):
        error = evidence_pack.validate_finding({"issue": "no lipsync"}, tmp_path)
        assert error == "finding cites no evidence_path"

    def test_finding_with_missing_evidence_is_rejected(self, tmp_path):
        error = evidence_pack.validate_finding(
            {"issue": "no lipsync", "evidence_path": "nope.jpg"}, tmp_path
        )
        assert "does not exist" in error

    def test_finding_with_real_evidence_passes(self, tmp_path):
        (tmp_path / "frame.jpg").write_bytes(b"f")
        assert evidence_pack.validate_finding(
            {"issue": "no lipsync", "evidence_path": "frame.jpg"}, tmp_path
        ) is None

class TestQaIntegration:
    def _project(self, tmp_path, *, required: bool):
        project = tmp_path / "proj"
        (project / "artifacts").mkdir(parents=True)
        plan = {
            "version": "1.0",
            "evidence_pack_required": required,
            "shots": [{"shot_id": "m1_002", "duration_s": 4,
                       "dialogue": {"speaker": "ch_nora", "text": "hi"}}],
        }
        import json as _json
        (project / "artifacts" / "scene_plan.json").write_text(
            _json.dumps(plan), encoding="utf-8")
        return project

    def test_missing_pack_blocks_when_required(self, tmp_path):
        from shortdrama import qa
        project = self._project(tmp_path, required=True)
        report = qa.review(project, "shot_gen", probe=lambda p: None)
        assert any("evidence pack missing" in b["issue"] for b in report["blocking"])

    def test_pack_not_required_leaves_legacy_runs_alone(self, tmp_path):
        from shortdrama import qa
        project = self._project(tmp_path, required=False)
        report = qa.review(project, "shot_gen", probe=lambda p: None)
        assert not any("evidence pack" in b["issue"] for b in report["blocking"])
