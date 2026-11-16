"""Hard lessons must be born red: their fixture proves the predicate can FAIL.

A hard lesson without a failing regression fixture is a claim nobody checked
(v2.6 LESSON-07). This test walks every hard lesson in the repository file and
feeds its predicate the recorded incident fixture: it must come out violated.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import lesson_predicates, lessons

ROOT = Path(__file__).resolve().parents[2]


def _hard_lessons():
    return [lesson for lesson in lessons.load() if lesson.enforcement == "hard"]


def test_every_hard_lesson_is_regression_tested():
    checked = 0
    for lesson in _hard_lessons():
        assert lesson.predicate, f"{lesson.id} is hard without a predicate"
        fixture = ROOT / str(lesson.regression_fixture)
        assert fixture.is_file(), f"{lesson.id} fixture is missing: {fixture}"
        context = json.loads(fixture.read_text(encoding="utf-8"))
        verdict = lesson_predicates.evaluate(lesson.predicate, context)
        assert verdict is False, (
            f"{lesson.id} did not FAIL on its incident fixture; a green fixture "
            "proves nothing"
        )
        checked += 1
    assert checked >= 1, "no hard lesson is regression-tested"
