"""`om lesson` CLI contracts (v2.6 LESSON-02 / CARD-L03).

The CLI is the only channel that turns a spoken objection into a record, so it
must refuse vague input (unknown code, empty evidence, hard without fixture) and
must write a record the loader can read back.
"""
from __future__ import annotations

import json

from shortdrama import cli


def _run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip().startswith("{") else out


def test_capture_writes_a_lesson(tmp_path, capsys):
    code, payload = _run(
        [
            "lesson", "capture",
            "--reason-code", "MUSIC_UNAVAILABLE",
            "--stage", "assembly",
            "--evidence", "音乐和内容根本就不配",
            "--root", str(tmp_path),
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert payload["reason_code"] == "MUSIC_UNAVAILABLE"
    assert payload["enforcement"] == "soft"
    written = tmp_path / ".governance" / "pending-lessons"
    assert list(written.glob("*.yml"))


def test_capture_rejects_unknown_reason_code(tmp_path, capsys):
    code, payload = _run(
        ["lesson", "capture", "--reason-code", "NOPE", "--stage", "assembly",
         "--evidence", "x", "--root", str(tmp_path), "--json"],
        capsys,
    )
    assert code == 2
    assert "not registered" in payload["error"]


def test_capture_rejects_empty_evidence(tmp_path, capsys):
    code, payload = _run(
        ["lesson", "capture", "--reason-code", "PACING", "--stage", "assembly",
         "--evidence", "   ", "--root", str(tmp_path), "--json"],
        capsys,
    )
    assert code == 2
    assert "evidence" in payload["error"]


def test_hard_capture_requires_predicate_and_fixture(tmp_path, capsys):
    code, payload = _run(
        ["lesson", "capture", "--reason-code", "SUBTITLE_MISALIGN", "--stage", "delivery",
         "--evidence", "subtitle 1.36s early", "--enforcement", "hard",
         "--root", str(tmp_path), "--json"],
        capsys,
    )
    assert code == 2
    assert "predicate" in payload["error"]


def test_hard_capture_with_predicate_and_fixture_succeeds(tmp_path, capsys):
    code, payload = _run(
        ["lesson", "capture", "--reason-code", "SUBTITLE_MISALIGN", "--stage", "delivery",
         "--evidence", "subtitle 1.36s early", "--enforcement", "hard",
         "--predicate-kind", "subtitle_timing_matches_speech",
         "--fixture", "tests/fixtures/lessons/m1_subtitle_early.json",
         "--root", str(tmp_path), "--json"],
        capsys,
    )
    assert code == 0
    assert payload["enforcement"] == "hard"


def test_list_reads_the_repository_lessons(capsys):
    code, payload = _run(["lesson", "list", "--json"], capsys)
    assert code == 0
    assert payload["count"] >= 1
    ids = {lesson["id"] for lesson in payload["lessons"]}
    assert "shortdrama.music.audition_before_use" in ids


def test_list_reports_invalid_repository_file(tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("version: nope\nlessons: []\n", encoding="utf-8")
    code, payload = _run(["lesson", "list", "--path", str(bad), "--json"], capsys)
    assert code == 2
    assert "version" in payload["error"]

def test_list_tolerates_a_hard_lesson_without_reason_code(tmp_path, capsys, monkeypatch):
    from lib import lessons

    monkeypatch.setattr(lessons, "PRIMITIVES", {"probe": "test primitive"})
    path = tmp_path / "no_code.yml"
    path.write_text(
        "version: agentgate.io/lessons/v1\n"
        "lessons:\n"
        "  - id: test.hard.no_code\n"
        "    enforcement: hard\n"
        "    status: confirmed\n"
        "    predicate: {kind: probe}\n"
        "    regression_fixture: tests/fixtures/lessons/m1_subtitle_early.json\n"
        "    trigger: t\n"
        "    risk: r\n"
        "    fix: f\n"
        "    regression: g\n",
        encoding="utf-8",
    )
    code, payload = _run(["lesson", "list", "--path", str(path), "--json"], capsys)
    assert code == 0
    assert payload["lessons"][0]["reason_code"] is None
