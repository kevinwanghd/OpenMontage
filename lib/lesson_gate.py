"""Lesson gate evaluation (v2.6 LESSON-06).

The driver asks this module one question per stage: do any applicable *hard*
lessons fail? It answers with explicit blocked entries so the pause carries the
lesson id and the original fix text (not a generic "lesson failed").

Fail-closed rules:
- ``False`` and ``None`` both block; only ``True`` passes.
- An evaluator that raises blocks with the exception text.
- ``soft`` lessons are never evaluated here; they belong to human/DIR-04 review.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from lib import lesson_predicates
from lib.lessons import Lesson


def applicable_hard_lessons(stage: str, loaded: Iterable[Lesson]) -> list[Lesson]:
    return [
        lesson
        for lesson in loaded
        if lesson.enforcement == "hard" and lesson.applies_to(stage)
    ]


def pending_hard_lessons(stages: Iterable[str], loaded: Iterable[Lesson]) -> list[dict[str, Any]]:
    """Read-only overview for ``om plan``: hard lessons that will be enforced."""
    stage_list = list(stages)
    out: list[dict[str, Any]] = []
    for lesson in loaded:
        if lesson.enforcement != "hard":
            continue
        touched = [stage for stage in stage_list if lesson.applies_to(stage)]
        if touched:
            out.append({"id": lesson.id, "stages": touched, "fix": lesson.fix})
    return out


def blocked_entries(
    stage: str,
    hard_lessons: Iterable[Lesson],
    context: Mapping[str, Any],
    skipped: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate each hard lesson once. Returns only the blocking ones.

    ``skipped`` (when passed) receives one entry per lesson that had no evidence
    to evaluate. Skipping is never silent: the caller records the reason so an
    unarmed lesson is visible instead of looking green.
    """
    blocked: list[dict[str, Any]] = []
    for lesson in hard_lessons:
        predicate = lesson.predicate or {}
        try:
            verdict = lesson_predicates.evaluate(predicate, context)
        except lesson_predicates.PredicateError as error:
            # The evidence this lesson measures was never collected for this
            # stage (legacy projects, stages without an evidence file). That is
            # "not armed", not "passed": keep the reason so the caller can log
            # and count it instead of silently dropping the lesson.
            if skipped is not None:
                skipped.append({
                    "id": lesson.id,
                    "stage": stage,
                    "kind": predicate.get("kind"),
                    "reason": f"no evidence: {error}",
                })
            continue
        # risk:suppressed-warning reason:"评估器内部的任何异常都必须转成 BLOCKED，不能中断整轮 preflight；这是 fail-closed 的边界" owner:@kevinwanghd reviewed:2026-11-16
        except Exception as error:  # noqa: BLE001 — any other evaluator failure is a block
            blocked.append({
                "id": lesson.id,
                "stage": stage,
                "kind": predicate.get("kind"),
                "verdict": "error",
                "reason": f"{type(error).__name__}: {error}",
                "fix": lesson.fix,
            })
            continue
        if verdict is not True:
            blocked.append({
                "id": lesson.id,
                "stage": stage,
                "kind": predicate.get("kind"),
                "verdict": "fail" if verdict is False else "undecidable",
                "reason": lesson.risk,
                "fix": lesson.fix,
            })
    return blocked