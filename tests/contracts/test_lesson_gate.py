"""The driver's lesson gate blocks on hard lessons and never on soft ones."""
from __future__ import annotations

from lib import lesson_gate, lesson_predicates, lessons
from lib.lessons import Lesson


def _lesson(enforcement="hard", predicate=None, stages=("assembly",), fixture="tests/fixtures/lessons/m1_subtitle_early.json"):
    return Lesson(
        id="test.lesson",
        enforcement=enforcement,
        trigger="t",
        risk="会出错",
        fix="按 fix 修正",
        regression="r",
        status="confirmed",
        applies_to_stages=stages,
        predicate=predicate,
        regression_fixture=fixture,
    )


SUBTITLE = {"kind": "subtitle_timing_matches_speech", "tolerance_s": 0.35, "gap_s": 0.15}


def test_failing_fixture_blocks_with_lesson_and_fix():
    blocked = lesson_gate.blocked_entries("assembly", [_lesson(predicate=SUBTITLE)], {
        "subtitles": [{"start": 16.32, "end": 18.55}],
        "speech": [{"start": 17.68, "end": 19.30}],
    })
    assert len(blocked) == 1
    assert blocked[0]["id"] == "test.lesson"
    assert blocked[0]["verdict"] == "fail"
    assert blocked[0]["fix"] == "按 fix 修正"


def test_fixed_evidence_passes():
    blocked = lesson_gate.blocked_entries("assembly", [_lesson(predicate=SUBTITLE)], {
        "subtitles": [{"start": 17.63, "end": 19.30}],
        "speech": [{"start": 17.68, "end": 19.30}],
    })
    assert blocked == []


def test_undetectable_speech_blocks_instead_of_passing():
    blocked = lesson_gate.blocked_entries("assembly", [_lesson(predicate=SUBTITLE)], {
        "subtitles": [{"start": 1.0, "end": 2.0}],
        "speech": [],
    })
    assert blocked and blocked[0]["verdict"] == "undecidable"


def test_evaluator_exception_blocks_fail_closed():
    def boom(predicate, context):
        raise RuntimeError("measurement exploded")

    lesson_predicates.EVALUATORS["boom_predicate"] = boom
    try:
        blocked = lesson_gate.blocked_entries(
            "assembly", [_lesson(predicate={"kind": "boom_predicate"})], {}
        )
    finally:
        lesson_predicates.EVALUATORS.pop("boom_predicate", None)
    assert blocked and blocked[0]["verdict"] == "error"
    assert "measurement exploded" in blocked[0]["reason"]


def test_soft_lessons_are_never_gated():
    soft = _lesson(enforcement="soft", predicate=SUBTITLE)
    assert lesson_gate.applicable_hard_lessons("assembly", [soft]) == []


def test_stage_filtering_and_plan_overview():
    assembly = _lesson(predicate=SUBTITLE, stages=("assembly",))
    import dataclasses
    delivery = dataclasses.replace(
        _lesson(predicate=SUBTITLE, stages=("delivery",)), id="test.delivery"
    )
    assert lesson_gate.applicable_hard_lessons("delivery", [assembly]) == []
    pending = lesson_gate.pending_hard_lessons(["assembly", "delivery"], [assembly, delivery])
    assert {row["id"] for row in pending} == {"test.lesson", "test.delivery"}

def test_missing_evidence_is_recorded_not_silently_skipped():
    skipped = []
    blocked = lesson_gate.blocked_entries(
        "assembly", [_lesson(predicate=SUBTITLE)], {}, skipped
    )
    assert blocked == []
    assert skipped and skipped[0]["id"] == "test.lesson"
    assert skipped[0]["reason"].startswith("no evidence:")


def test_unknown_predicate_kind_blocks_instead_of_skipping():
    skipped = []
    blocked = lesson_gate.blocked_entries(
        "assembly", [_lesson(predicate={"kind": "not_registered"})], {}, skipped
    )
    assert skipped == []
    assert blocked and blocked[0]["verdict"] == "error"
    assert "not_registered" in blocked[0]["reason"]
    assert blocked[0]["fix"] == "按 fix 修正"
