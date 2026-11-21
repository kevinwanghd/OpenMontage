"""v2.7 CARD-E01 — the evidence file hard lessons are judged against.

Before this producer existed, `driver._lesson_context()` looked for a file that
nothing wrote. Every measurement-based predicate therefore ran on a context with
no measurements in it, which is how a hard lesson can look armed while never
being able to fail.
"""

from __future__ import annotations

import json
import time

import pytest

from lib import lesson_evidence as ev
from lib import lesson_predicates

PREDICATE = {"tolerance_s": 0.35, "gap_s": 0.15}

SRT = """1
00:00:05,000 --> 00:00:10,000
I can't stay for dinner.

2
00:00:10,000 --> 00:00:14,000
I made too much soup.
"""

ASS = """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:05.00,0:00:10.00,Drama,,0,0,0,,I can't stay for dinner.
Dialogue: 0,0:00:18.00,0:00:22.00,Drama,,0,0,0,,Take some with you.
"""

TTS_SIDECAR = {
    "code": 20000000,
    "data": {"sentences": [{"startTime": 5.0, "endTime": 7.5, "text": "I can't stay for dinner."}]},
}

TTS_SIDECAR_MS = {
    "code": 20000000,
    "data": {"sentences": [{"startTime": 5000, "endTime": 7500, "text": "ms units"}]},
}


def _project(tmp_path, *, srt=None, ass=None, tts=None, report=None):
    project = tmp_path / "proj"
    assets = project / "ep01" / "assets"
    artifacts = project / "artifacts"
    assets.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    if srt:
        (assets / "assembly_subtitle_subtitle_default.srt").write_text(srt, encoding="utf-8")
    if ass:
        (project / "m1_subtitles.ass").write_text(ass, encoding="utf-8")
    if tts:
        (assets / "assembly_tts_s2_ch_nora.mp3.json").write_text(json.dumps(tts), encoding="utf-8")
    if report:
        (artifacts / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    return project


class TestParsing:
    def test_srt_cues(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_text(SRT, encoding="utf-8")
        cues = ev.parse_subtitles(path)
        assert cues[0] == {"start": 5.0, "end": 10.0}

    def test_ass_cues(self, tmp_path):
        path = tmp_path / "a.ass"
        path.write_text(ASS, encoding="utf-8")
        cues = ev.parse_subtitles(path)
        assert len(cues) == 2
        assert cues[1] == {"start": 18.0, "end": 22.0}

    def test_tts_seconds(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(TTS_SIDECAR), encoding="utf-8")
        assert ev.parse_tts_speech(path)[0] == {"start": 5.0, "end": 7.5}

    def test_tts_milliseconds_are_detected(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(TTS_SIDECAR_MS), encoding="utf-8")
        assert ev.parse_tts_speech(path)[0] == {"start": 5.0, "end": 7.5}

    def test_corrupt_tts_raises_instead_of_skipping(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"data": {"sentences": [{"text": "no times"}]}}), encoding="utf-8")
        with pytest.raises(ev.LessonEvidenceError):
            ev.parse_tts_speech(path)


class TestCollect:
    def test_missing_sources_produce_missing_fields(self, tmp_path):
        """Absence must stay absent — a default would silently disarm the lesson."""
        project = _project(tmp_path)
        assert ev.collect(project, "assembly") == {}

    def test_subtitles_and_speech_are_collected(self, tmp_path):
        project = _project(tmp_path, srt=SRT, tts=TTS_SIDECAR)
        evidence = ev.collect(project, "assembly")
        assert evidence["subtitles"][0] == {"start": 5.0, "end": 10.0}
        assert evidence["speech"][0] == {"start": 5.0, "end": 7.5}

    def test_silent_lines_are_computed(self, tmp_path):
        project = _project(tmp_path, srt=SRT, tts=TTS_SIDECAR)
        # Speech only covers 5-7.5; the 10-14 cue has no speech under it.
        assert ev.collect(project, "assembly")["silent_lines"] == [1]

    def test_loudness_and_version_come_from_render_report(self, tmp_path):
        project = _project(tmp_path, report={
            "audio": {"loudness_lufs": -14.6},
            "version_binding": "v1",
            "current_version": "v1",
        })
        evidence = ev.collect(project, "delivery")
        assert evidence["loudness_lufs"] == -14.6
        assert evidence["version_binding"] == "v1"


class TestPredicateIntegration:
    """The producer must actually drive the predicates it exists for."""

    def test_subtitle_predicate_fails_on_real_incident_evidence(self, tmp_path):
        # Incident shape: subtitles lead the speech by ~1.36s.
        project = _project(
            tmp_path,
            srt="1\n00:00:03,640 --> 00:00:06,000\nline\n",
            tts={"data": {"sentences": [{"startTime": 5.0, "endTime": 6.5}]}},
        )
        evidence = ev.collect(project, "assembly")
        verdict = lesson_predicates.subtitle_timing_matches_speech(PREDICATE, evidence)
        assert verdict is False

    def test_subtitle_predicate_passes_when_aligned(self, tmp_path):
        project = _project(tmp_path, srt=SRT, tts={"data": {"sentences": [
            {"startTime": 5.05, "endTime": 9.9},
            {"startTime": 10.05, "endTime": 13.9},
        ]}})
        evidence = ev.collect(project, "assembly")
        assert lesson_predicates.subtitle_timing_matches_speech(PREDICATE, evidence) is not False


class TestWrite:
    def test_write_is_atomic_and_readable(self, tmp_path):
        project = _project(tmp_path, srt=SRT, tts=TTS_SIDECAR)
        evidence = ev.collect(project, "assembly")
        path = ev.write(project, "assembly", evidence)
        assert json.loads(path.read_text(encoding="utf-8"))["subtitles"]
        leftovers = [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_write_replaces_previous_evidence(self, tmp_path):
        project = _project(tmp_path, srt=SRT)
        ev.write(project, "assembly", {"a": 1})
        path = ev.write(project, "assembly", {"b": 2})
        assert json.loads(path.read_text(encoding="utf-8")) == {"b": 2}

    def test_write_failure_leaves_no_partial_file(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        target_dir = ev.evidence_path(project, "assembly").parent

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(ev.os, "replace", boom)
        with pytest.raises(RuntimeError):
            ev.write(project, "assembly", {"a": 1})
        assert list(target_dir.glob("*.json")) == []