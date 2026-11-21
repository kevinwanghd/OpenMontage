"""v2.7 GATE-02 — validate the artifact, not the code path that produced it.

Regression fixture: the 2026-11-17 delivery shipped a male voice track for two
female characters. The gate that should have caught it lived on the driver's
execution path, and the audio was a reused file, so nothing ran.

The gate must therefore work from the manifest alone: given a project directory,
it resolves each declared asset and checks the file matches what was claimed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from lib import artifact_gate

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "gate" / "male_voice_incident.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _project(tmp_path: Path, manifest_name: str, *, audio: bytes = b"fake-mp3") -> Path:
    """Materialise a project dir: character cards + one audio asset + manifest."""
    data = _fixture()
    project = tmp_path / "proj"
    (project / "artifacts" / "characters").mkdir(parents=True, exist_ok=True)
    (project / "ep01" / "assets").mkdir(parents=True, exist_ok=True)

    for character_id, card in data["character_cards"].items():
        (project / "artifacts" / "characters" / f"{character_id}.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8"
        )

    audio_path = project / "ep01" / "assets" / "tts_s2_nora.mp3"
    audio_path.write_bytes(audio)
    digest = hashlib.sha256(audio).hexdigest()

    manifest = json.loads(json.dumps(data["manifests"][manifest_name]))
    for asset in manifest.get("assets", []):
        provenance = asset.get("provenance")
        if isinstance(provenance, dict) and provenance.get("sha256") == "FILE_HASH":
            provenance["sha256"] = digest
    (project / "artifacts" / "asset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return project


class TestMaleVoiceIncident:
    """The incident itself must be reproducible as a failing gate."""

    def test_wrong_voice_blocks(self, tmp_path):
        project = _project(tmp_path, "bad_male_voice")
        report = artifact_gate.validate(project)
        issues = [b["issue"] for b in report["blocking"]]
        assert "voice does not match the character card" in issues

    def test_issue_names_both_voice_ids(self, tmp_path):
        project = _project(tmp_path, "bad_male_voice")
        finding = next(
            b for b in artifact_gate.validate(project)["blocking"]
            if b["issue"] == "voice does not match the character card"
        )
        assert "zh_male" in finding["evidence"]
        assert "zh_female" in finding["evidence"]

    def test_correct_voice_passes(self, tmp_path):
        project = _project(tmp_path, "good_female_voice")
        assert artifact_gate.validate(project)["blocking"] == []


class TestProvenance:
    def test_legacy_manifest_warns_but_does_not_block(self, tmp_path):
        """Projects mid-flight predate this gate; blocking them would strand runs."""
        project = _project(tmp_path, "legacy_without_provenance")
        report = artifact_gate.validate(project)
        assert report["blocking"] == []
        assert report["warnings"][0]["issue"].startswith("no provenance")

    def test_provenance_manifest_requires_the_fields(self, tmp_path):
        project = _project(tmp_path, "good_female_voice")
        manifest_path = project / "artifacts" / "asset_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assets"][0]["provenance"].pop("model")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        issues = [b["issue"] for b in artifact_gate.validate(project)["blocking"]]
        assert "incomplete provenance" in issues

    def test_hash_mismatch_blocks(self, tmp_path):
        """A file swapped after review must not pass as the reviewed asset."""
        project = _project(tmp_path, "good_female_voice")
        (project / "ep01" / "assets" / "tts_s2_nora.mp3").write_bytes(b"different-audio")
        issues = [b["issue"] for b in artifact_gate.validate(project)["blocking"]]
        assert "artifact does not match its recorded hash" in issues

    def test_missing_file_blocks(self, tmp_path):
        project = _project(tmp_path, "good_female_voice")
        (project / "ep01" / "assets" / "tts_s2_nora.mp3").unlink()
        issues = [b["issue"] for b in artifact_gate.validate(project)["blocking"]]
        assert "declared artifact is missing on disk" in issues


class TestCharacterBinding:
    def test_unregistered_character_blocks(self, tmp_path):
        project = _project(tmp_path, "good_female_voice")
        (project / "artifacts" / "characters" / "ch_nora.json").unlink()
        issues = [b["issue"] for b in artifact_gate.validate(project)["blocking"]]
        assert "voice cannot be verified against a character card" in issues

    def test_audio_without_voice_identity_blocks(self, tmp_path):
        project = _project(tmp_path, "good_female_voice")
        manifest_path = project / "artifacts" / "asset_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assets"][0]["provenance"].pop("voice_id")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        issues = [b["issue"] for b in artifact_gate.validate(project)["blocking"]]
        assert "dialogue audio has no voice identity" in issues


class TestManifestDiscovery:
    def test_absent_manifest_is_silent(self, tmp_path):
        """A missing manifest is another gate's finding; do not double-report."""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert artifact_gate.validate(empty) == {"blocking": [], "warnings": []}

    def test_non_generated_asset_kinds_are_skipped(self, tmp_path):
        project = _project(tmp_path, "good_female_voice")
        manifest_path = project / "artifacts" / "asset_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assets"][0]["type"] = "subtitle"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        assert artifact_gate.validate(project)["blocking"] == []


class TestCheckpointIntegration:
    def test_completed_short_drama_checkpoint_is_blocked(self, tmp_path):
        from lib.checkpoint import CheckpointValidationError, write_checkpoint

        project_dir = _project(tmp_path, "bad_male_voice")
        project_id = project_dir.name
        (tmp_path / project_id).rename(tmp_path / project_id)  # project already in place
        with pytest.raises(CheckpointValidationError, match="ARTIFACT VIOLATION"):
            write_checkpoint(
                tmp_path, project_id, "shot_gen", "completed",
                artifacts={"asset_manifest": {"version": "1.0", "assets": []}},
                pipeline_type="short-drama",
                human_approved=True,
            )

    def test_other_pipelines_are_unaffected(self, tmp_path):
        from lib.checkpoint import write_checkpoint

        _project(tmp_path, "bad_male_voice")
        # An explainer project has no short-drama manifest semantics.
        try:
            write_checkpoint(
                tmp_path, "proj", "script", "completed",
                artifacts={"script": {"version": "1.0", "title": "t",
                                      "total_duration_seconds": 1, "sections": []}},
                pipeline_type="animated-explainer",
                human_approved=True,
            )
        except Exception as error:  # prerequisites may still fire; not our gate
            assert "ARTIFACT VIOLATION" not in str(error)

class TestManifestRequiredAtCompletion:
    """A completed generating stage with no manifest is not a pass."""

    def test_missing_manifest_blocks_when_required(self, tmp_path):
        empty = tmp_path / "proj"
        empty.mkdir()
        report = artifact_gate.validate(empty, require_manifest=True)
        assert report["blocking"][0]["issue"] == "asset manifest missing at completion"

    def test_missing_manifest_is_quiet_for_read_only_callers(self, tmp_path):
        empty = tmp_path / "proj"
        empty.mkdir()
        assert artifact_gate.validate(empty)["blocking"] == []
