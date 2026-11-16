"""Lesson schema + loader contracts (v2.6 LESSON-03 / CARD-L01).

The point of these tests is fail-closed startup: a lesson that names an unknown
reason code or an unwired predicate must break loading instead of being skipped,
because a silently ignored lesson is exactly how the W1-W6 wiring gap happened.
"""
from __future__ import annotations

import pytest
import yaml

from lib import lessons
from lib.lessons import LessonConfigError


def _write(tmp_path, records, version=lessons.LESSONS_VERSION):
    path = tmp_path / "repository.yml"
    path.write_text(
        yaml.safe_dump({"version": version, "scope": "repository", "lessons": records},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _soft(**overrides):
    record = {
        "id": "shortdrama.example.soft",
        "enforcement": "soft",
        "status": "pending",
        "trigger": "写 edit_decisions 之前",
        "risk": "按标题臆测曲风，音乐与内容不配",
        "fix": "先读 genre/comment 标签并试听确认",
        "regression": "2026-11-16 M1 样片弦乐四重奏误标 piano",
    }
    record.update(overrides)
    return record


def _hard(**overrides):
    record = _soft(
        id="shortdrama.subtitle.timing_vs_speech",
        enforcement="hard",
        status="confirmed",
        reason_code="SUBTITLE_MISALIGN",
        applies_to_stages=["assembly", "delivery"],
        predicate={"kind": "subtitle_timing_matches_speech", "tolerance_s": 0.35},
        regression_fixture="tests/fixtures/lessons/m1_subtitle_early.json",
    )
    record.update(overrides)
    return record


def test_repository_file_loads_and_the_music_lesson_is_soft():
    loaded = lessons.load()
    by_id = {lesson.id: lesson for lesson in loaded}
    assert "shortdrama.music.audition_before_use" in by_id
    assert by_id["shortdrama.music.audition_before_use"].enforcement == "soft"


def test_empty_lesson_list_stays_valid(tmp_path):
    path = _write(tmp_path, [])
    assert lessons.load(path) == []


def test_soft_lesson_without_reason_code_or_predicate_loads(tmp_path):
    path = _write(tmp_path, [_soft()])
    loaded = lessons.load(path)
    assert loaded[0].reason_code is None
    assert loaded[0].predicate is None


def test_unknown_reason_code_fails_loading(tmp_path):
    path = _write(tmp_path, [_soft(reason_code="NOT_A_REAL_CODE")])
    with pytest.raises(LessonConfigError) as raised:
        lessons.load(path)
    assert "NOT_A_REAL_CODE" in str(raised.value)


def test_unknown_predicate_kind_fails_loading(tmp_path):
    path = _write(tmp_path, [_soft(predicate={"kind": "no_such_primitive"})])
    with pytest.raises(LessonConfigError) as raised:
        lessons.load(path)
    assert "no registered primitive" in str(raised.value)


def test_hard_lesson_without_fixture_fails_loading(tmp_path):
    record = _hard()
    record.pop("regression_fixture")
    path = _write(tmp_path, [record])
    with pytest.raises(LessonConfigError) as raised:
        lessons.load(path)
    assert "regression_fixture" in str(raised.value)


def test_hard_lesson_without_reason_code_loads(tmp_path, monkeypatch):
    # A lesson's identity is its predicate, not a review code (v2.6 §2).
    monkeypatch.setattr(lessons, "PRIMITIVES", {"subtitle_timing_matches_speech": "timing"})
    record = _hard()
    record.pop("reason_code")
    path = _write(tmp_path, [record])
    loaded = lessons.load(path)
    assert loaded[0].enforcement == "hard"
    assert loaded[0].reason_code is None


def test_hard_lesson_without_predicate_fails_loading(tmp_path):
    record = _hard()
    record.pop("predicate")
    path = _write(tmp_path, [record])
    with pytest.raises(LessonConfigError):
        lessons.load(path)


def test_hard_lesson_with_registered_primitive_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(lessons, "PRIMITIVES", {"subtitle_timing_matches_speech": "timing"})
    path = _write(tmp_path, [_hard()])
    loaded = lessons.load(path)
    assert loaded[0].enforcement == "hard"
    assert loaded[0].predicate["tolerance_s"] == 0.35


def test_missing_required_field_fails_loading(tmp_path):
    record = _soft()
    record.pop("risk")
    path = _write(tmp_path, [record])
    with pytest.raises(LessonConfigError) as raised:
        lessons.load(path)
    assert "risk" in str(raised.value)


def test_unknown_stage_fails_loading(tmp_path):
    path = _write(tmp_path, [_soft(applies_to_stages=["not_a_stage"])])
    with pytest.raises(LessonConfigError) as raised:
        lessons.load(path)
    assert "not_a_stage" in str(raised.value)


def test_duplicate_ids_fail_loading(tmp_path):
    path = _write(tmp_path, [_soft(), _soft()])
    with pytest.raises(LessonConfigError) as raised:
        lessons.load(path)
    assert "duplicate" in str(raised.value)


def test_wrong_version_fails_loading(tmp_path):
    path = _write(tmp_path, [], version="agentgate.io/lessons/v0")
    with pytest.raises(LessonConfigError):
        lessons.load(path)


def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(LessonConfigError):
        lessons.load(tmp_path / "nope.yml")


def test_applies_to_returns_true_without_stage_scope():
    lesson = lessons.Lesson(id="x", enforcement="soft", trigger="t", risk="r",
                            fix="f", regression="g", status="pending")
    assert lesson.applies_to("assembly") is True


def test_applies_to_honours_declared_stages():
    lesson = lessons.Lesson(id="x", enforcement="soft", trigger="t", risk="r",
                            fix="f", regression="g", status="pending",
                            applies_to_stages=("assembly",))
    assert lesson.applies_to("assembly") is True
    assert lesson.applies_to("shot_gen") is False