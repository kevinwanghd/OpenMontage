"""Contract tests for the deterministic QA gate (shortdrama.qa)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortdrama import qa


def _plan(dialogue: bool = True) -> dict:
    shot = {
        "shot_id": "m1_001",
        "duration_s": 5,
        "reference_character_ids": ["ch_a"],
    }
    if dialogue:
        shot["dialogue"] = {"text": "hello", "speaker": "ch_a"}
    return {"version": "1.0", "scenes": [{"id": "sc1"}], "shots": [shot]}


def _probe(width=720, height=1280, duration=5.0, audio=True):
    return lambda _p: {"width": width, "height": height,
                       "duration": duration, "has_audio": audio}


def test_media_passes_when_portrait_with_dialogue_audio(tmp_path):
    result = qa.review_media(tmp_path, _plan(), probe=_probe())
    assert result["blocking"] == []


def test_media_blocks_landscape(tmp_path):
    result = qa.review_media(tmp_path, _plan(), probe=_probe(width=1280, height=720))
    assert any("not vertical" in b["issue"] for b in result["blocking"])


def test_media_blocks_dialogue_without_audio(tmp_path):
    result = qa.review_media(tmp_path, _plan(), probe=_probe(audio=False))
    assert any("no audio" in b["issue"] for b in result["blocking"])


def test_media_blocks_missing_clip(tmp_path):
    result = qa.review_media(tmp_path, _plan(), probe=lambda _p: None)
    assert any("clip missing" in b["issue"] for b in result["blocking"])


def test_subtitle_cue_longer_than_speech_is_a_warning(tmp_path):
    srt = tmp_path / qa.SUBTITLE_FILE
    srt.write_text(
        "1\n00:00:05,000 --> 00:00:10,000\nI can't stay for dinner.\n",
        encoding="utf-8",
    )
    result = qa.review_subtitles(srt)
    assert result["blocking"] == []
    assert any("outlives" in w["issue"] for w in result["warnings"])


def test_review_aggregates_and_decides(tmp_path):
    project = tmp_path / "proj"
    (project / "artifacts").mkdir(parents=True)
    (project / "artifacts" / "scene_plan.json").write_text(
        json.dumps(_plan()), encoding="utf-8"
    )
    (project / "artifacts" / "character_design.json").write_text(
        json.dumps({"version": "1.0", "characters": [{"id": "ch_a"}]}),
        encoding="utf-8",
    )
    (project / "ep01" / "assets").mkdir(parents=True)
    verdict = qa.review(project, "shot_gen", probe=_probe())
    assert verdict["passed"] is True
    # structural checks clean; visual review not yet run -> one warning (-5)
    assert verdict["score"] == 95
    assert any("not run" in w["issue"] for w in verdict["warnings"])

def test_visual_verdict_reject_blocks(tmp_path):
    project = tmp_path / "proj"
    (project / "artifacts").mkdir(parents=True)
    (project / "artifacts" / qa.VISUAL_VERDICT_FILE).write_text(
        json.dumps({"verdict": "reject", "score": 40,
                    "blocking": [{"shot": "m1_002", "issue": "street scene"}],
                    "warnings": []}),
        encoding="utf-8",
    )
    result = qa.review_visual(project)
    assert result["blocking"] and "street" in result["blocking"][0]["issue"]


def test_missing_visual_review_is_a_warning_not_silent_pass(tmp_path):
    result = qa.review_visual(tmp_path / "nope")
    assert result["blocking"] == []
    assert any("not run" in w["issue"] for w in result["warnings"])
