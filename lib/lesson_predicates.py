"""Lesson predicate primitives (v2.6 LESSON-05).

A hard lesson names a ``predicate.kind`` plus its thresholds; this module maps
each kind to a deterministic evaluator. Evaluation is pure: the caller passes
the artifacts it already read, so a predicate never fetches or runs anything.

Verdicts are three-valued on purpose:

- ``True``  — the invariant holds;
- ``False`` — it is violated, the stage must be BLOCKED with the lesson's fix;
- ``None``  — it cannot be decided from the supplied evidence (missing speech
  intervals, silent scene, no measurement). The driver treats ``None`` as
  BLOCKED and asks a human: a predicate must never default to pass.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from lib.lessons import register_primitive

# A measurement has to be *found* before it can be compared. When the evidence
# collection handed to the predicate lacks the key, the verdict is undecidable.
Verdict = Optional[bool]


class PredicateError(ValueError):
    """A predicate or its arguments are unusable (missing key, wrong shape)."""


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PredicateError(f"{name} must be a number")
    return float(value)


def _intervals(items: Any, name: str) -> list[tuple[float, float]]:
    if not isinstance(items, (list, tuple)):
        raise PredicateError(f"{name} must be a list of {{start, end}}")
    out: list[tuple[float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise PredicateError(f"{name}[{index}] must be a mapping")
        start = _number(item.get("start"), f"{name}[{index}].start")
        end = _number(item.get("end"), f"{name}[{index}].end")
        if end < start:
            raise PredicateError(f"{name}[{index}] ends before it starts")
        out.append((start, end))
    return out


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def subtitle_timing_matches_speech(
    predicate: Mapping[str, Any], context: Mapping[str, Any]
) -> Verdict:
    """Every subtitle line must sit inside the speech it transcribes.

    Per line: ``lead = sub.start - speech.start`` (negative = text early),
    ``lag = sub.end - speech.end``, ``gap`` = seconds of that line's speech not
    covered by the subtitle. PASS iff ``|lead| <= T`` and ``|lag| <= T`` and
    ``gap <= G`` for *all* lines. T=0.35s, G=0.15s per PRD §4.1.

    Missing speech intervals (BGM louder than speech, pure silence) are not a
    pass: a line with no detectable speech makes the whole evaluation ``None``.
    Silent hook lines declared ``silent: true`` are skipped as n/a.
    """
    tolerance = float(predicate.get("tolerance_s", 0.35))
    gap_limit = float(predicate.get("gap_s", 0.15))
    subtitles = _intervals(context.get("subtitles"), "subtitles")
    speech = _intervals(context.get("speech"), "speech")
    silent_lines = {int(i) for i in context.get("silent_lines", [])}

    if not subtitles:
        return None
    undecidable = False
    for index, line in enumerate(subtitles):
        if index in silent_lines:
            continue
        overlapping = [segment for segment in speech if _overlap(line, segment) > 0]
        if not overlapping:
            undecidable = True
            continue
        start = min(segment[0] for segment in overlapping)
        end = max(segment[1] for segment in overlapping)
        lead = line[0] - start
        lag = line[1] - end
        covered = sum(_overlap(line, segment) for segment in overlapping)
        gap = max(0.0, (end - start) - covered)
        if abs(lead) > tolerance or abs(lag) > tolerance or gap > gap_limit:
            return False
    return None if undecidable else True


def music_audition_confirmed(
    predicate: Mapping[str, Any], context: Mapping[str, Any]
) -> Verdict:
    """A licensed track still needs a recorded audition before assembly uses it.

    Reads ``edit_decisions.audio.music`` (preferred) or legacy
    ``edit_decisions.music``; every referenced entry must carry
    ``audition_confirmed: true``. No music at all is ``None`` (decide nothing),
    not a pass.
    """
    edit_decisions = context.get("edit_decisions")
    if not isinstance(edit_decisions, Mapping):
        raise PredicateError("edit_decisions must be a mapping")
    audio = edit_decisions.get("audio")
    entries = [audio.get("music")] if isinstance(audio, Mapping) else []
    entries.append(edit_decisions.get("music"))
    tracks = [entry for entry in entries if isinstance(entry, Mapping)]
    if not tracks:
        return None
    return all(entry.get("audition_confirmed") is True for entry in tracks)


def approval_basis_recorded(
    predicate: Mapping[str, Any], context: Mapping[str, Any]
) -> Verdict:
    """A human checkpoint must record *why* it was approved (not just a flag)."""
    checkpoint = context.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise PredicateError("checkpoint must be a mapping")
    basis = checkpoint.get("approval_basis")
    if not isinstance(basis, str):
        return False
    return bool(basis.strip())


def loudness_in_range(
    predicate: Mapping[str, Any], context: Mapping[str, Any]
) -> Verdict:
    """Final mix must stay inside the platform loudness window (AUD-07).

    Defaults mirror ``lib/loudness.py``: target -14 LUFS ± 1 LU. No measurement
    is undecidable, never a pass.
    """
    measured = context.get("loudness_lufs")
    if measured is None:
        return None
    value = _number(measured, "loudness_lufs")
    low = float(predicate.get("min_lufs", -15.0))
    high = float(predicate.get("max_lufs", -13.0))
    return low <= value <= high


def version_binding_current(
    predicate: Mapping[str, Any], context: Mapping[str, Any]
) -> Verdict:
    """A rendered artifact must be bound to the version currently in force."""
    binding = context.get("version_binding")
    current = context.get("current_version")
    if not isinstance(binding, str) or not isinstance(current, str):
        return None
    return binding.strip() == current.strip()


EVALUATORS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Verdict]] = {
    "subtitle_timing_matches_speech": subtitle_timing_matches_speech,
    "music_audition_confirmed": music_audition_confirmed,
    "approval_basis_recorded": approval_basis_recorded,
    "loudness_in_range": loudness_in_range,
    "version_binding_current": version_binding_current,
}

DESCRIPTIONS = {
    "subtitle_timing_matches_speech": "字幕行与实测语音区间的 lead/lag/gap 都在阈值内",
    "music_audition_confirmed": "选用的曲目已记录试听确认",
    "approval_basis_recorded": "人工 checkpoint 记录了审批依据",
    "loudness_in_range": "成片整体响度在平台窗口内",
    "version_binding_current": "产物绑定的是当前生效版本",
}


def evaluate(
    predicate: Mapping[str, Any], context: Mapping[str, Any]
) -> Verdict:
    """Run one predicate. Unknown kinds fail loudly instead of passing."""
    kind = str(predicate.get("kind") or "").strip()
    evaluator = EVALUATORS.get(kind)
    if evaluator is None:
        raise PredicateError(f"unknown predicate kind: {kind!r}")
    return evaluator(predicate, context)


for _kind, _description in DESCRIPTIONS.items():
    register_primitive(_kind, _description)