"""v2.7 CARD-E02 — the evidence producer must be wired into the run.

CARD-E01 built the producer. This pins the two integration points that make it
matter: a stage that has executed writes its evidence, and a write failure
blocks the stage instead of quietly continuing.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from lib import lesson_evidence
from shortdrama.driver import Driver


@pytest.fixture()
def driver(tmp_path) -> Driver:
    project = tmp_path / "demo"
    (project / "ep01" / "assets").mkdir(parents=True)
    (project / "ep01" / "artifacts").mkdir(parents=True)
    return Driver("demo", "ep01", projects_dir=tmp_path, profile="alpha")


class TestEvidenceWrittenOnStageCompletion:
    def test_executed_stage_produces_evidence_file(self, driver):
        driver._write_lesson_evidence("assembly", {})
        path = lesson_evidence.evidence_path(
            driver.projects_dir / driver.project, "assembly", episode=driver.episode
        )
        assert path.is_file()

    def test_stage_entry_records_the_path(self, driver):
        entry: dict = {}
        driver._write_lesson_evidence("assembly", entry)
        assert entry["lesson_evidence"].endswith("lesson_evidence_assembly.json")

    def test_write_failure_blocks_the_stage(self, driver):
        with mock.patch.object(lesson_evidence, "write", side_effect=OSError("disk full")):
            with pytest.raises(Exception) as error:
                driver._write_lesson_evidence("assembly", {})
        assert "lesson evidence could not be written" in str(error.value)

    def test_corrupt_source_blocks_the_stage(self, driver):
        project = driver.projects_dir / driver.project
        (project / "artifacts").mkdir(parents=True, exist_ok=True)
        (project / "artifacts" / "script.json").write_text(
            '{"sections": [{"id": "s2", "text": "hi", "start_seconds": 5, "end_seconds": 9}]}',
            encoding="utf-8")
        sidecar = project / "ep01" / "assets" / "assembly_tts_s2_ch_nora.mp3.json"
        sidecar.write_text('{"data": {"sentences": [{"text": "no times"}]}}', encoding="utf-8")
        with pytest.raises(Exception) as error:
            driver._write_lesson_evidence("assembly", {})
        assert "LessonEvidenceError" in str(error.value) or "lesson evidence" in str(error.value)


class TestPlanVisibility:
    def test_plan_exposes_pending_evidence(self, driver):
        plan = driver.plan()
        assert "lesson_evidence_pending" in plan
        assert isinstance(plan["lesson_evidence_pending"], list)

    def test_plan_does_not_block_on_missing_evidence(self, driver):
        """Planning stays read-only; it may only report what is pending."""
        plan = driver.plan()
        assert plan["status"] != "blocked" if "status" in plan else True