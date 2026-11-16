"""Lesson records (v2.6 LESSON-03): schema, loader and startup validation.

A lesson turns a recurring production mistake into a declarative record. P0 only
stores and validates lessons; it never evaluates them (that is LESSON-05/06 in
P1) and never blocks a stage. Validation is fail-closed in the same way DUAL-09
validates agent models: a lesson that names an unknown reason code, an unknown
predicate primitive, or omits a required field makes loading fail loudly instead
of being skipped.

Hard vs soft (v2.6 D24):
- ``hard`` lessons are machine-checkable invariants. They must name a predicate
  and a regression fixture taken from a real incident.
- ``soft`` lessons capture judgement calls (taste, preference). They may omit
  the predicate; they never block a stage.
- ``reason_code`` is optional on both. It is filled in only when the failure
  class also exists in the review vocabulary (``lib/review.py``), so a lesson
  never forces a phantom code into ``REASON_CODES``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from lib.review import REASON_CODES

LESSONS_VERSION = "agentgate.io/lessons/v1"
ENFORCEMENTS = frozenset({"soft", "hard"})
STATUSES = frozenset({"pending", "confirmed", "promoted", "rejected"})

REQUIRED_FIELDS = ("id", "enforcement", "trigger", "risk", "fix", "regression", "status")
# A lesson's identity is its predicate, not a review reason code. `reason_code`
# stays optional for every lesson (v2.6 §2, 2026-11-16): codes are only
# registered for failure classes that actually appear in review decisions.
#
# Migration note (why dropping the requirement is safe): nothing indexes
# ``review_strategies.STRATEGIES`` by ``Lesson.reason_code``. The only consumers
# of a lesson's code are display and aggregation (``om plan``, ``om lesson
# list``), which tolerate ``None``; hard lessons resolve their behaviour through
# ``PRIMITIVES`` by ``predicate.kind``. Lessons that already carry a code keep
# working unchanged, so no data migration is needed.
HARD_REQUIRED_FIELDS = ("predicate", "regression_fixture")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LESSONS_PATH = (
    Path(__file__).resolve().parents[1] / "governance" / "lessons" / "repository.yml"
)
LESSONS_PATH_ENV = "LESSONS_PATH"


class LessonConfigError(ValueError):
    """A lesson record (or the file holding it) is unusable."""


# Predicate primitives (v2.6 LESSON-05). P0 registers none: a hard lesson that
# names a primitive cannot load until its evaluator exists, which is the point —
# a declared-but-unwired predicate must never look green (W1-W6 lesson).
PRIMITIVES: dict[str, str] = {}


def register_primitive(kind: str, description: str = "") -> None:
    """Register a predicate primitive kind. Evaluators arrive in P1."""
    name = (kind or "").strip()
    if not name:
        raise LessonConfigError("predicate primitive kind must not be empty")
    PRIMITIVES[name] = description


def known_primitives() -> frozenset[str]:
    return frozenset(PRIMITIVES)


@dataclass(frozen=True)
class Lesson:
    id: str
    enforcement: str
    trigger: str
    risk: str
    fix: str
    regression: str
    status: str
    reason_code: Optional[str] = None
    applies_to_stages: tuple[str, ...] = ()
    predicate: Optional[dict[str, Any]] = None
    regression_fixture: Optional[str] = None
    occurrence_count: int = 0
    repos_seen: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def applies_to(self, stage: str) -> bool:
        """A lesson with no declared stages applies everywhere."""
        return not self.applies_to_stages or stage in self.applies_to_stages


@lru_cache(maxsize=1)
def known_stages() -> frozenset[str]:
    """Every stage name declared by any pipeline manifest, plus canonical ones."""
    from lib.checkpoint import ALL_KNOWN_STAGES, get_pipeline_stages

    stages = set(ALL_KNOWN_STAGES)
    manifests = Path(__file__).resolve().parents[1] / "pipeline_defs"
    if manifests.is_dir():
        for path in manifests.glob("*.yaml"):
            stages.update(get_pipeline_stages(path.stem))
    return frozenset(stages)


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_lesson(
    record: Mapping[str, Any], *, stages: Optional[Iterable[str]] = None
) -> list[str]:
    """Return human-readable problems with one lesson record ([] when valid).

    The predicate primitives register themselves on import; doing it here (not
    at module scope) keeps ``lib.lessons`` importable on its own and avoids a
    module-level import cycle with ``lib.lesson_predicates``.
    """
    # risk:suppressed-warning reason:"导入只为注册原语副作用，函数级导入用于避免模块级循环；不是未使用导入的缺陷" owner:@kevinwanghd reviewed:2026-11-16
    from lib import lesson_predicates  # noqa: F401 — registration side effect
    problems: list[str] = []
    stage_set = frozenset(stages) if stages is not None else known_stages()

    missing = [name for name in REQUIRED_FIELDS if not _as_text(record.get(name))]
    if missing:
        problems.append("missing required field(s): " + ", ".join(missing))

    enforcement = _as_text(record.get("enforcement"))
    if enforcement and enforcement not in ENFORCEMENTS:
        problems.append(f"enforcement '{enforcement}' is not one of {sorted(ENFORCEMENTS)}")
    status = _as_text(record.get("status"))
    if status and status not in STATUSES:
        problems.append(f"status '{status}' is not one of {sorted(STATUSES)}")

    stages_value = record.get("applies_to_stages") or []
    if not isinstance(stages_value, list) or not all(isinstance(s, str) for s in stages_value):
        problems.append("applies_to_stages must be a list of stage names")
    else:
        unknown = [s for s in stages_value if s not in stage_set]
        if unknown:
            problems.append("unknown stage(s) in applies_to_stages: " + ", ".join(unknown))

    reason_code = _as_text(record.get("reason_code"))
    if reason_code and reason_code not in REASON_CODES:
        problems.append(
            f"reason_code '{reason_code}' is not registered in lib/review.py REASON_CODES"
        )

    predicate = record.get("predicate")
    if predicate is not None:
        if not isinstance(predicate, Mapping):
            problems.append("predicate must be a mapping with a 'kind'")
        else:
            kind = _as_text(predicate.get("kind"))
            if not kind:
                problems.append("predicate.kind is required")
            elif kind not in PRIMITIVES:
                problems.append(
                    f"predicate kind '{kind}' has no registered primitive (v2.6 LESSON-05)"
                )

    if enforcement == "hard":
        for name in HARD_REQUIRED_FIELDS:
            if not record.get(name):
                problems.append(f"hard lesson requires '{name}' (v2.6 D24)")
        fixture = _as_text(record.get("regression_fixture"))
        if fixture and not (REPO_ROOT / fixture).is_file():
            problems.append(
                f"hard lesson regression_fixture does not exist: {fixture} (v2.6 LESSON-07)"
            )
        if reason_code and reason_code not in REASON_CODES:
            pass  # already reported above
    return problems


def _to_lesson(record: Mapping[str, Any]) -> Lesson:
    return Lesson(
        id=str(record.get("id", "")).strip(),
        enforcement=str(record.get("enforcement", "")).strip(),
        trigger=str(record.get("trigger", "")).strip(),
        risk=str(record.get("risk", "")).strip(),
        fix=str(record.get("fix", "")).strip(),
        regression=str(record.get("regression", "")).strip(),
        status=str(record.get("status", "")).strip(),
        reason_code=(_as_text(record.get("reason_code")) or None),
        applies_to_stages=tuple(record.get("applies_to_stages") or ()),
        predicate=dict(record["predicate"]) if isinstance(record.get("predicate"), Mapping) else None,
        regression_fixture=(_as_text(record.get("regression_fixture")) or None),
        occurrence_count=int(record.get("occurrence_count") or 0),
        repos_seen=tuple(record.get("repos_seen") or ()),
        raw=dict(record),
    )


def load(path: Optional[Path | str] = None) -> list[Lesson]:
    """Load and validate the lesson file. Raises LessonConfigError when unusable."""
    target = Path(path) if path else Path(os.environ.get(LESSONS_PATH_ENV, DEFAULT_LESSONS_PATH))
    if not target.is_file():
        raise LessonConfigError(f"lesson file not found: {target}")
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise LessonConfigError(f"lesson file is not valid YAML: {error}") from error
    if not isinstance(data, Mapping):
        raise LessonConfigError(f"lesson file root must be a mapping: {target}")
    if data.get("version") != LESSONS_VERSION:
        raise LessonConfigError(
            f"lesson file version must be {LESSONS_VERSION!r}, got {data.get('version')!r}"
        )
    raw_lessons = data.get("lessons") or []
    if not isinstance(raw_lessons, list):
        raise LessonConfigError("lessons must be a list")

    lessons: list[Lesson] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, record in enumerate(raw_lessons):
        if not isinstance(record, Mapping):
            problems.append(f"lessons[{index}] must be a mapping")
            continue
        issues = validate_lesson(record)
        if issues:
            problems.extend(f"lessons[{index}] ({record.get('id', '?')}): {p}" for p in issues)
            continue
        lesson = _to_lesson(record)
        if lesson.id in seen:
            problems.append(f"duplicate lesson id: {lesson.id}")
            continue
        seen.add(lesson.id)
        lessons.append(lesson)

    if problems:
        raise LessonConfigError("invalid lessons in " + str(target) + ":\n  " + "\n  ".join(problems))
    return lessons


def ensure_valid(lessons: Iterable[Lesson]) -> None:
    """Raise when any loaded lesson would silently do nothing."""
    for lesson in lessons:
        if lesson.enforcement == "hard" and not lesson.predicate:
            raise LessonConfigError(f"hard lesson {lesson.id!r} has no predicate")
