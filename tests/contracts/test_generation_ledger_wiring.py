"""GATE-03 wiring — the ledger must be consulted before money is spent.

The module and its tests existed, but no provider called it, so duplicate
submissions were still possible. These tests pin the call site: a request that
is already in flight is refused *before* the network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import generation_jobs as jobs


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "artifacts").mkdir(parents=True)
    (project / "ep01" / "assets").mkdir(parents=True)
    return project


def _seed(project: Path, *, provider: str, model: str, prompt: str, params: dict) -> None:
    ledger = jobs.JobLedger(project / jobs.JOBS_FILE)
    fp = jobs.request_fingerprint(provider, model, prompt, params)
    ledger.submit(jobs.JobRecord(
        job_id="job-existing",
        request_fingerprint=fp,
        idempotency_key=jobs.idempotency_key(fp),
        provider=provider,
    ))


class TestLedgerScoping:
    def test_artifact_inside_a_project_finds_the_ledger(self, tmp_path):
        project = _project(tmp_path)
        ledger = jobs.ledger_for_artifact(project / "ep01" / "assets" / "clip.mp4")
        assert ledger is not None
        assert ledger.path == project / jobs.JOBS_FILE

    def test_artifact_outside_a_project_is_unscoped(self, tmp_path):
        assert jobs.ledger_for_artifact(tmp_path / "loose" / "clip.mp4") is None


class TestPixVerseRefusesDuplicate:
    def test_duplicate_is_refused_before_any_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIXVERSE_API_KEY", "test-key")
        from tools.video.pixverse_video import PixVerseVideo

        project = _project(tmp_path)
        prompt = "A woman lifts a duffel bag."
        params = {
            "model": "v6", "prompt": prompt, "duration": 5,
            "quality": "540p", "aspect_ratio": "9:16",
        }
        _seed(project, provider="pixverse", model="v6", prompt=prompt, params=params)

        result = PixVerseVideo().execute({
            "prompt": prompt,
            "operation": "text_to_video",
            "output_path": str(project / "ep01" / "assets" / "clip.mp4"),
        })

        assert result.success is False
        assert result.data["refused"] == "duplicate_submission"


class TestMiniMaxRefusesDuplicate:
    def test_video_duplicate_is_refused(self, tmp_path):
        from tools.video.minimax_video import MiniMaxVideo

        project = _project(tmp_path)
        prompt = "A slow push-in."
        identity = {"prompt": prompt}
        _seed(project, provider="minimax", model="MiniMax-H3", prompt=prompt, params=identity)

        result = MiniMaxVideo().execute({
            "prompt": prompt,
            "output_path": str(project / "ep01" / "assets" / "clip.mp4"),
        })

        assert result.success is False
        assert result.data["refused"] == "duplicate_submission"

    def test_image_duplicate_is_refused(self, tmp_path):
        from tools.graphics.minimax_image import MiniMaxImage

        project = _project(tmp_path)
        prompt = "A still portrait."
        _seed(project, provider="minimax", model="image-01", prompt=prompt, params={"prompt": prompt})

        result = MiniMaxImage().execute({
            "prompt": prompt,
            "output_path": str(project / "ep01" / "assets" / "frame.png"),
        })

        assert result.success is False
        assert result.data["refused"] == "duplicate_submission"


class TestTimeoutBecomesUnknown:
    """A dropped connection must leave `unknown`, which blocks a resubmission."""

    def test_minimax_image_transport_error_is_unknown(self, tmp_path, monkeypatch):
        import requests

        from tools.graphics.minimax_image import MiniMaxImage

        project = _project(tmp_path)
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")

        def _boom(*args, **kwargs):
            raise requests.ConnectionError("connection reset")

        monkeypatch.setattr("requests.post", _boom)
        result = MiniMaxImage().execute({
            "prompt": "A still portrait.",
            "output_path": str(project / "ep01" / "assets" / "frame.png"),
        })

        assert result.success is False
        ledger = jobs.JobLedger(project / jobs.JOBS_FILE)
        record = ledger.records()[-1]
        assert record.state == "unknown"
        assert ledger.resubmission_allowed(record.request_fingerprint) is False

    def test_pixverse_timeout_is_unknown(self, tmp_path, monkeypatch):
        from tools.video.pixverse_video import PixVerseVideo

        project = _project(tmp_path)
        monkeypatch.setenv("PIXVERSE_API_KEY", "test-key")

        def _timeout(*args, **kwargs):
            raise TimeoutError("no response")

        monkeypatch.setattr("tools.video.pixverse_video.PixVerseClient.image_to_video", _timeout)
        monkeypatch.setattr("tools.video.pixverse_video.PixVerseClient.upload_image", lambda *a, **k: 1)

        result = PixVerseVideo().execute({
            "prompt": "A woman lifts a duffel bag.",
            "image_path": __file__,
            "output_path": str(project / "ep01" / "assets" / "clip.mp4"),
        })

        assert result.success is False
        assert result.data["ledger_state"] == "unknown"
        ledger = jobs.JobLedger(project / jobs.JOBS_FILE)
        assert ledger.records()[-1].state == "unknown"