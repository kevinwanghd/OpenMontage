"""Word-level subtitle alignment adapter (AUD-05).

This module makes the *source* of cue timings explicit. There are two
legitimate upstream paths for a short-drama dialogue track:

- ``tts_timestamps`` — the TTS provider returns word (or character grouped
  into word) timings for the exact synthesized audio. These are real
  per-word timings, but they are provider timings, not an alignment pass
  over the final mixed/graded track.
- ``forced_alignment`` — an injected forced aligner aligns a transcript to
  the actual dialogue audio. The aligner must be declared as such, either
  by an ``alignment_method = "forced_alignment"`` attribute on the callable
  or by an ``alignment_method`` key in its returned payload.

Plain automatic speech recognition (``transcriber``, Azure/DashScope ASR,
...) produces a *transcription*, not a forced alignment. Fed through this
adapter it is rejected with ``TRANSCRIPTION_NOT_FORCED_ALIGNMENT`` and the
result is BLOCKED/UNKNOWN — it must never be reported as an aligned
subtitle track just because it happens to carry word timestamps.

Validation reuses :mod:`lib.subtitle` (AUD-05 timeline monotonicity/range,
AUD-06 ≤12 words and ≤3 s per cue, minimum display duration). Defects are
reported as structured :class:`~lib.subtitle.SubtitleIssue` values; cues are
never truncated, split differently or silently dropped. The 200 ms
start/end tolerance is the provisional M1 acceptance bound and stays marked
uncalibrated.

Everything here is pure Python over already-produced data: no network, no
ffmpeg, no transcription.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from lib.subtitle import (
    ALIGNMENT_TOLERANCE_MS,
    EMPTY_TEXT,
    MAX_CUE_DURATION_S,
    MAX_WORDS_PER_CUE,
    SubtitleCue,
    SubtitleIssue,
    count_words,
    validate_rules,
    validate_timeline,
)

# --- alignment sources ------------------------------------------------------
METHOD_TTS = "tts_timestamps"
METHOD_FORCED = "forced_alignment"
METHOD_UNKNOWN = "unknown"

# --- result status ----------------------------------------------------------
STATUS_ALIGNED = "ALIGNED"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_BLOCKED = "BLOCKED"
STATUS_UNKNOWN = "UNKNOWN"

# --- calibration ------------------------------------------------------------
CALIBRATION_PENDING = "uncalibrated_m1_pending"
CALIBRATION_VERIFIED = "calibrated"

# --- issue codes (stable identifiers) ---------------------------------------
CODE_BLOCKED_NO_ALIGNER = "ALIGNMENT_BLOCKED_NO_ALIGNER"
CODE_UNKNOWN_METHOD = "UNKNOWN_ALIGNMENT_METHOD"
CODE_TRANSCRIPTION_NOT_FORCED = "TRANSCRIPTION_NOT_FORCED_ALIGNMENT"
CODE_ALIGNER_FAILED = "ALIGNER_FAILED"
CODE_EMPTY_ALIGNMENT = "EMPTY_ALIGNMENT"
CODE_MISSING_WORD_TIMESTAMPS = "MISSING_WORD_TIMESTAMPS"
CODE_NO_WORDS = "NO_WORDS"
CODE_INVALID_WORD_TIMING = "INVALID_WORD_TIMING"

_EPS = 1e-9


@dataclass(frozen=True)
class AlignmentReport:
    """Auditable description of how a cue timeline was obtained."""

    alignment_method: str
    status: str
    calibration_status: str
    tolerance_ms: float
    estimated_error_ms: Optional[float]
    cue_count: int
    audio_duration_s: Optional[float] = None
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "alignment_method": self.alignment_method,
            "status": self.status,
            "calibration_status": self.calibration_status,
            "tolerance_ms": self.tolerance_ms,
            "estimated_error_ms": self.estimated_error_ms,
            "cue_count": self.cue_count,
            "audio_duration_s": self.audio_duration_s,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class AlignmentResult:
    """Cues plus every defect found while building them.

    ``report.status`` is ``ALIGNED`` only when cues exist and no issue was
    raised. ``REVIEW_REQUIRED`` means cues exist but a human must confirm
    one or more flags. ``BLOCKED``/``UNKNOWN`` means no trustworthy cues
    could be produced and the caller must not proceed as if aligned.
    """

    cues: tuple[SubtitleCue, ...]
    issues: tuple[SubtitleIssue, ...]
    report: AlignmentReport

    @property
    def ok(self) -> bool:
        return self.report.status == STATUS_ALIGNED

    @property
    def blocked(self) -> bool:
        return self.report.status in (STATUS_BLOCKED, STATUS_UNKNOWN)

    @property
    def manual_review_items(self) -> tuple[SubtitleIssue, ...]:
        return tuple(i for i in self.issues if i.requires_manual_confirmation)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _issue(code: str, detail: str, cue_index: Optional[int] = None,
           manual: bool = True) -> SubtitleIssue:
    return SubtitleIssue(code, cue_index, detail, manual)


def _check_audio_duration(value: Any, *, required: bool) -> Optional[float]:
    if value is None:
        if required:
            raise ValueError("audio_duration_s is required for TTS-aligned cues")
        return None
    if not _finite(value) or float(value) <= 0:
        raise ValueError(
            f"audio_duration_s must be a positive finite number, got {value!r}")
    return float(value)


def _normalize_words(payload: Any) -> tuple[list[Mapping[str, Any]], list[SubtitleIssue], bool]:
    """Collect word entries from word lists, segments and TTS payloads.

    Returns ``(words, issues, saw_segment_only)``. Segment-only entries carry
    text plus segment bounds but no word timings; they are recorded as a
    defect instead of being promoted into word-level cues.
    """
    issues: list[SubtitleIssue] = []
    words: list[Mapping[str, Any]] = []
    saw_segment_only = False

    items: Any = payload
    if isinstance(payload, Mapping):
        for key in ("words", "word_timestamps"):
            if isinstance(payload.get(key), (list, tuple)):
                items = payload[key]
                break
        else:
            if isinstance(payload.get("segments"), (list, tuple)):
                items = payload["segments"]
            elif "word" in payload or "text" in payload:
                items = [payload]
            else:
                items = []

    if not isinstance(items, (list, tuple)):
        return [], [_issue(
            CODE_INVALID_WORD_TIMING,
            f"unexpected alignment payload type {type(payload).__name__}",
            manual=False)], False

    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            issues.append(_issue(
                CODE_INVALID_WORD_TIMING,
                f"entry #{position} is not a mapping", manual=False))
            continue
        if "word" in item:
            words.append(item)
            continue
        nested = item.get("words")
        if isinstance(nested, (list, tuple)):
            for nested_position, nested_word in enumerate(nested):
                if isinstance(nested_word, Mapping) and "word" in nested_word:
                    words.append(nested_word)
                else:
                    issues.append(_issue(
                        CODE_INVALID_WORD_TIMING,
                        f"segment #{position} word #{nested_position} is not a "
                        "word entry", manual=False))
            continue
        if "text" in item:
            saw_segment_only = True
            issues.append(_issue(
                CODE_MISSING_WORD_TIMESTAMPS,
                f"segment #{position} has text but no word timestamps; "
                "segment-level timing cannot drive word-level cues"))
            continue
        issues.append(_issue(
            CODE_INVALID_WORD_TIMING,
            f"entry #{position} has neither 'word' nor 'text'", manual=False))
    return words, issues, saw_segment_only


def _group_words(
    words: Sequence[Mapping[str, Any]],
    *,
    max_words: int = MAX_WORDS_PER_CUE,
    max_duration_s: float = MAX_CUE_DURATION_S,
) -> tuple[list[SubtitleCue], list[SubtitleIssue]]:
    """Group word timings into cues at word-count/duration boundaries.

    Grouping is proactive, not truncation: all words are kept, cues merely
    start at the threshold. A single entry that is itself over the limit
    (for example a provider phrase chunk) stays whole and is flagged later
    by :func:`lib.subtitle.validate_rules`. Out-of-order words start a new
    cue so the non-monotonic input stays visible to the timeline validator.
    """
    cues: list[SubtitleCue] = []
    issues: list[SubtitleIssue] = []
    current: list[str] = []
    current_words = 0
    start: Optional[float] = None
    end: Optional[float] = None

    def flush() -> None:
        nonlocal current, current_words, start, end
        if current and start is not None and end is not None:
            cues.append(SubtitleCue(
                index=len(cues), start=start, end=end,
                text=" ".join(current)))
        current = []
        current_words = 0
        start = None
        end = None

    for position, raw in enumerate(words):
        text = str(raw.get("word") or raw.get("text") or "").strip()
        word_start = raw.get("start")
        word_end = raw.get("end")
        if not (_finite(word_start) and _finite(word_end)):
            issues.append(_issue(
                CODE_INVALID_WORD_TIMING,
                f"word #{position} has non-finite timing "
                f"start={word_start!r} end={word_end!r}", manual=False))
            continue
        word_start = float(word_start)
        word_end = float(word_end)
        if not text:
            issues.append(_issue(
                EMPTY_TEXT, f"word #{position} has empty text"))
            continue
        if word_end < word_start - _EPS:
            issues.append(_issue(
                CODE_INVALID_WORD_TIMING,
                f"word #{position} {text!r} ends before it starts "
                f"({word_start}s -> {word_end}s)", manual=False))

        tokens = count_words(text)
        if current:
            over_words = current_words + tokens > max_words
            over_duration = (
                word_end >= word_start
                and start is not None
                and word_end - start > max_duration_s + _EPS)
            out_of_order = end is not None and word_start < end - _EPS
            if over_words or over_duration or out_of_order:
                flush()

        if not current:
            start = word_start
        current.append(text)
        current_words += tokens
        end = word_end if end is None else max(end, word_end)

    flush()
    return cues, issues


def _finalize(
    cues: Sequence[SubtitleCue],
    issues: Sequence[SubtitleIssue],
    *,
    method: str,
    audio_duration_s: Optional[float],
    estimated_error_ms: Optional[float],
    calibration_status: str,
    notes: str,
) -> AlignmentResult:
    if not cues:
        status = STATUS_BLOCKED
    elif issues:
        status = STATUS_REVIEW_REQUIRED
    else:
        status = STATUS_ALIGNED
    report = AlignmentReport(
        alignment_method=method,
        status=status,
        calibration_status=calibration_status,
        tolerance_ms=ALIGNMENT_TOLERANCE_MS,
        estimated_error_ms=estimated_error_ms,
        cue_count=len(cues),
        audio_duration_s=audio_duration_s,
        notes=notes,
    )
    return AlignmentResult(cues=tuple(cues), issues=tuple(issues), report=report)


def _blocked(
    *,
    method: str,
    status: str,
    code: str,
    detail: str,
    audio_duration_s: Optional[float] = None,
    issues: Sequence[SubtitleIssue] = (),
    notes: str = "",
) -> AlignmentResult:
    collected = list(issues)
    if not any(issue.code == code for issue in collected):
        collected.append(_issue(code, detail))
    report = AlignmentReport(
        alignment_method=method,
        status=status,
        calibration_status=CALIBRATION_PENDING,
        tolerance_ms=ALIGNMENT_TOLERANCE_MS,
        estimated_error_ms=None,
        cue_count=0,
        audio_duration_s=audio_duration_s,
        notes=notes or detail,
    )
    return AlignmentResult(cues=(), issues=tuple(collected), report=report)


def _estimated_error_ms(payload: Any) -> Optional[float]:
    if not isinstance(payload, Mapping):
        return None
    for key in ("estimated_error_ms", "alignment_error_ms", "mean_error_ms"):
        value = payload.get(key)
        if _finite(value) and float(value) >= 0:
            return float(value)
    return None


def _calibration_status(payload: Any) -> str:
    if isinstance(payload, Mapping):
        value = payload.get("calibration_status")
        if value == CALIBRATION_VERIFIED:
            return CALIBRATION_VERIFIED
    return CALIBRATION_PENDING


def _declared_methods(aligner: Any, payload: Any) -> list[str]:
    values: list[str] = []
    for source in (aligner, payload):
        if source is None:
            continue
        for attribute in ("alignment_method", "method", "kind"):
            if isinstance(source, Mapping):
                value = source.get(attribute)
            else:
                value = getattr(source, attribute, None)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return values


# ---------------------------------------------------------------------------
# TTS timestamps
# ---------------------------------------------------------------------------

def align_from_tts_timestamps(
    segments: Any,
    audio_duration_s: float,
    *,
    estimated_error_ms: Optional[float] = None,
) -> AlignmentResult:
    """Build cues from TTS-provided word timings for the synthesized audio.

    ``segments`` may be the transcriber/TTS ``segments`` list (each entry
    carrying ``words``), a flat word list, or a payload mapping with
    ``words``/``word_timestamps``. Segment-level-only input (text with no
    word timings) is BLOCKED: it is not word-level alignment.

    The resulting report says ``alignment_method="tts_timestamps"``. That is
    deliberately distinct from forced alignment over the final dialogue
    track, and the error estimate stays ``None`` unless the caller supplies
    a measured/declared value.
    """
    duration = _check_audio_duration(audio_duration_s, required=True)
    if estimated_error_ms is not None and (
            not _finite(estimated_error_ms) or float(estimated_error_ms) < 0):
        raise ValueError(
            "estimated_error_ms must be a non-negative finite number, "
            f"got {estimated_error_ms!r}")

    words, normalize_issues, saw_segment_only = _normalize_words(segments)
    if not words:
        if saw_segment_only:
            return _blocked(
                method=METHOD_TTS,
                status=STATUS_BLOCKED,
                code=CODE_MISSING_WORD_TIMESTAMPS,
                detail="segment-level TTS timings cannot produce word-level "
                       "cues; request word/character timestamps from the "
                       "provider",
                audio_duration_s=duration,
                issues=normalize_issues,
            )
        return _blocked(
            method=METHOD_TTS,
            status=STATUS_BLOCKED,
            code=CODE_NO_WORDS,
            detail="no word timings supplied",
            audio_duration_s=duration,
            issues=normalize_issues,
        )

    cues, group_issues = _group_words(words)
    issues = list(normalize_issues) + group_issues
    if cues:
        issues.extend(validate_timeline(cues, duration))
        issues.extend(validate_rules(cues))
    return _finalize(
        cues,
        issues,
        method=METHOD_TTS,
        audio_duration_s=duration,
        estimated_error_ms=(
            float(estimated_error_ms) if estimated_error_ms is not None else None),
        calibration_status=CALIBRATION_PENDING,
        notes="timings come from the TTS provider for the synthesized audio, "
              "not from forced alignment of the final dialogue track; error "
              "is unmeasured unless supplied",
    )


# ---------------------------------------------------------------------------
# forced alignment
# ---------------------------------------------------------------------------

def align_from_forced_alignment(
    audio_path: str,
    transcript: Any,
    aligner: Optional[Callable[..., Any]] = None,
    *,
    audio_duration_s: Optional[float] = None,
) -> AlignmentResult:
    """Align ``transcript`` to the real dialogue audio using an injected aligner.

    ``aligner`` is called as ``aligner(audio_path, transcript)`` and must
    declare itself a forced aligner, either with an
    ``alignment_method = "forced_alignment"`` attribute or an
    ``alignment_method`` key in its returned payload. It may return a word
    list, ``{"words": [...]}``, or segment entries that contain ``words``.

    Missing, failing or undeclared aligners yield an explicit BLOCKED or
    UNKNOWN result. Ordinary transcription output is rejected with
    ``TRANSCRIPTION_NOT_FORCED_ALIGNMENT``; this function never falls back
    to transcription timings.
    """
    duration = _check_audio_duration(audio_duration_s, required=False)

    if aligner is None:
        return _blocked(
            method=METHOD_UNKNOWN,
            status=STATUS_BLOCKED,
            code=CODE_BLOCKED_NO_ALIGNER,
            detail="no forced aligner supplied; plain transcription timings "
                   "are not a forced alignment of the dialogue track",
            audio_duration_s=duration,
        )
    if not callable(aligner):
        return _blocked(
            method=METHOD_UNKNOWN,
            status=STATUS_UNKNOWN,
            code=CODE_UNKNOWN_METHOD,
            detail=f"aligner {type(aligner).__name__} is not callable",
            audio_duration_s=duration,
        )

    try:
        payload = aligner(audio_path, transcript)
    # risk:suppressed-warning reason:"对齐器为外部注入对象，任意异常需转结构化 BLOCKED 而非中断批量对齐" owner:@kevin reviewed:2026-11-12
    except Exception as exc:  # noqa: BLE001 - surface any aligner failure
        return _blocked(
            method=METHOD_UNKNOWN,
            status=STATUS_BLOCKED,
            code=CODE_ALIGNER_FAILED,
            detail=f"forced aligner failed: {type(exc).__name__}: {exc}",
            audio_duration_s=duration,
        )

    markers = _declared_methods(aligner, payload)
    declared_forced = any(marker == METHOD_FORCED for marker in markers)
    foreign_markers = [marker for marker in markers if marker != METHOD_FORCED]
    transcription_shaped = (
        isinstance(payload, Mapping) and "segments" in payload)

    if not declared_forced:
        if foreign_markers or transcription_shaped:
            marker_note = (
                f"aligner declared {foreign_markers!r}"
                if foreign_markers else
                "alignment payload is shaped like a transcription result")
            return _blocked(
                method=METHOD_UNKNOWN,
                status=STATUS_BLOCKED,
                code=CODE_TRANSCRIPTION_NOT_FORCED,
                detail=f"{marker_note}; ordinary transcription is not forced "
                       "alignment and is not accepted as one",
                audio_duration_s=duration,
                issues=[_issue(
                    CODE_TRANSCRIPTION_NOT_FORCED,
                    f"methods={markers!r}")],
            )
        return _blocked(
            method=METHOD_UNKNOWN,
            status=STATUS_UNKNOWN,
            code=CODE_UNKNOWN_METHOD,
            detail="aligner did not declare alignment_method="
                   "forced_alignment; refusing to claim forced alignment",
            audio_duration_s=duration,
        )

    words, normalize_issues, _ = _normalize_words(payload)
    if not words:
        return _blocked(
            method=METHOD_FORCED,
            status=STATUS_BLOCKED,
            code=CODE_EMPTY_ALIGNMENT,
            detail="forced aligner returned no word-level timings",
            audio_duration_s=duration,
            issues=normalize_issues,
        )

    cues, group_issues = _group_words(words)
    issues = list(normalize_issues) + group_issues
    if cues:
        issues.extend(validate_timeline(cues, duration))
        issues.extend(validate_rules(cues))
    return _finalize(
        cues,
        issues,
        method=METHOD_FORCED,
        audio_duration_s=duration,
        estimated_error_ms=_estimated_error_ms(payload),
        calibration_status=_calibration_status(payload),
        notes="word-level timings produced by the injected forced aligner",
    )