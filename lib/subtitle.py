"""Deterministic subtitle validation for the short-drama pipeline.

Implements three machine-checkable contracts from the requirements doc:

- **AUD-05** — the cue timeline must be monotonic and lie inside the measured
  audio range; aligned cues are compared against reference annotations with an
  initial start/end tolerance of 200 ms (to be calibrated at M1).
- **AUD-06** — each English cue starts at ≤12 words and ≤3 s; over-long,
  too-short and abnormal-rate cues are *flagged for manual confirmation*.
  Nothing is ever truncated or rewritten here.
- **LOC-02** — English line breaking and the vertical-video safe area. The
  P0 safe area is a provisional TikTok 9:16 preset; because it is not yet
  calibrated (VIS-06) every safe-area check also raises an uncalibrated-preset
  manual item.

All functions take already-produced data (annotations, alignment output, text)
and never call an external service, transcribe audio or touch ffmpeg.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# --- AUD-06 / A5 initial thresholds (calibrated at M1) ---------------------
MAX_WORDS_PER_CUE = 12
MAX_CUE_DURATION_S = 3.0
MIN_CUE_DURATION_S = 0.5
MIN_WORDS_PER_SECOND = 1.0
MAX_WORDS_PER_SECOND = 4.5

# --- AUD-05 initial alignment tolerance ------------------------------------
ALIGNMENT_TOLERANCE_MS = 200.0

# --- LOC-02 English line breaking (overseas short-video habit) -------------
MAX_LINE_CHARS = 42
MAX_LINES = 2

# Issue codes (stable machine identifiers; tests and gates assert on these).
INVALID_TIME = "INVALID_TIME"
NON_POSITIVE_DURATION = "NON_POSITIVE_DURATION"
OUT_OF_RANGE = "OUT_OF_RANGE"
NOT_MONOTONIC = "NOT_MONOTONIC"
OVERLAPPING_CUES = "OVERLAPPING_CUES"
DUPLICATE_INDEX = "DUPLICATE_INDEX"
ALIGNMENT_START_ERROR = "ALIGNMENT_START_ERROR"
ALIGNMENT_END_ERROR = "ALIGNMENT_END_ERROR"
MISSING_ALIGNMENT = "MISSING_ALIGNMENT"
UNKNOWN_ALIGNMENT = "UNKNOWN_ALIGNMENT"
TOO_MANY_WORDS = "TOO_MANY_WORDS"
EMPTY_TEXT = "EMPTY_TEXT"
CUE_TOO_LONG = "CUE_TOO_LONG"
CUE_TOO_SHORT = "CUE_TOO_SHORT"
RATE_TOO_FAST = "RATE_TOO_FAST"
RATE_TOO_SLOW = "RATE_TOO_SLOW"
STYLE_TOO_MANY_LINES = "STYLE_TOO_MANY_LINES"
STYLE_LINE_TOO_LONG = "STYLE_LINE_TOO_LONG"
STYLE_ORPHAN_WORD = "STYLE_ORPHAN_WORD"
SAFE_AREA_INVALID_RECT = "SAFE_AREA_INVALID_RECT"
SAFE_AREA_VIOLATION = "SAFE_AREA_VIOLATION"
SAFE_AREA_PRESET_UNCALIBRATED = "SAFE_AREA_PRESET_UNCALIBRATED"

_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
_EPS = 1e-9


@dataclass(frozen=True)
class SubtitleCue:
    """One subtitle cue with seconds on the actual dialogue audio track."""

    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def word_count(self) -> int:
        return count_words(self.text)


@dataclass(frozen=True)
class SubtitleIssue:
    """A machine-detected defect; manual items are never auto-fixed."""

    code: str
    cue_index: Optional[int]
    detail: str
    requires_manual_confirmation: bool = True


def count_words(text: str) -> int:
    """Count English words; hyphenated/apostrophised forms count once."""
    return len(_WORD.findall(text))


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _issue(code: str, cue_index: Optional[int], detail: str,
           manual: bool = True) -> SubtitleIssue:
    return SubtitleIssue(code, cue_index, detail, manual)


# ---------------------------------------------------------------------------
# AUD-05 — timeline and alignment
# ---------------------------------------------------------------------------

def validate_timeline(
    cues: Sequence[SubtitleCue],
    audio_duration: Optional[float] = None,
) -> list[SubtitleIssue]:
    """Check a cue timeline is monotonic, non-overlapping and in audio range.

    Cues are evaluated in the order supplied. ``audio_duration`` is optional
    so annotation-only input (before the final track exists) can still be
    checked for internal consistency.
    """
    issues: list[SubtitleIssue] = []
    if audio_duration is not None and (not _finite(audio_duration) or audio_duration <= 0):
        raise ValueError(f"audio_duration must be a positive finite number, got {audio_duration!r}")

    previous: Optional[SubtitleCue] = None
    seen: set[int] = set()
    for cue in cues:
        idx = cue.index
        if idx in seen:
            issues.append(_issue(DUPLICATE_INDEX, idx, f"cue index {idx} repeated"))
        seen.add(idx)

        if not (_finite(cue.start) and _finite(cue.end)):
            issues.append(_issue(INVALID_TIME, idx,
                                 f"non-finite time start={cue.start!r} end={cue.end!r}",
                                 manual=False))
            continue
        if cue.end <= cue.start:
            issues.append(_issue(NON_POSITIVE_DURATION, idx,
                                 f"end {cue.end}s must be after start {cue.start}s"))
        if cue.start < 0:
            issues.append(_issue(OUT_OF_RANGE, idx, f"start {cue.start}s is before 0s"))
        if audio_duration is not None and cue.end > audio_duration + _EPS:
            issues.append(_issue(
                OUT_OF_RANGE, idx,
                f"end {cue.end}s exceeds audio duration {audio_duration}s"))

        if previous is not None:
            if cue.start < previous.start - _EPS:
                issues.append(_issue(
                    NOT_MONOTONIC, idx,
                    f"start {cue.start}s is before previous cue start {previous.start}s"))
            if cue.start < previous.end - _EPS:
                issues.append(_issue(
                    OVERLAPPING_CUES, idx,
                    f"start {cue.start}s overlaps previous cue ending {previous.end}s"))
        previous = cue
    return issues


def validate_alignment(
    aligned: Sequence[SubtitleCue],
    reference: Sequence[SubtitleCue],
    *,
    tolerance_ms: float = ALIGNMENT_TOLERANCE_MS,
) -> list[SubtitleIssue]:
    """Compare aligned cue bounds against reference annotations by index.

    Both start and end absolute errors must be ≤ ``tolerance_ms``. Missing and
    extra cues are reported instead of being silently dropped.
    """
    if tolerance_ms < 0 or not _finite(tolerance_ms):
        raise ValueError(f"tolerance_ms must be a non-negative finite number, got {tolerance_ms!r}")

    issues: list[SubtitleIssue] = []
    reference_by_index: dict[int, SubtitleCue] = {}
    for ref in reference:
        if ref.index in reference_by_index:
            issues.append(_issue(DUPLICATE_INDEX, ref.index,
                                 "duplicate index in reference annotations", manual=False))
        reference_by_index[ref.index] = ref

    matched: set[int] = set()
    for cue in aligned:
        ref = reference_by_index.get(cue.index)
        if ref is None:
            issues.append(_issue(UNKNOWN_ALIGNMENT, cue.index,
                                 "aligned cue has no reference annotation"))
            continue
        matched.add(cue.index)
        if not (_finite(cue.start) and _finite(cue.end)
                and _finite(ref.start) and _finite(ref.end)):
            issues.append(_issue(INVALID_TIME, cue.index,
                                 "non-finite time in aligned or reference cue",
                                 manual=False))
            continue
        start_error_ms = abs(cue.start - ref.start) * 1000.0
        end_error_ms = abs(cue.end - ref.end) * 1000.0
        if start_error_ms > tolerance_ms + _EPS:
            issues.append(_issue(
                ALIGNMENT_START_ERROR, cue.index,
                f"start error {start_error_ms:.1f}ms exceeds {tolerance_ms:.0f}ms"))
        if end_error_ms > tolerance_ms + _EPS:
            issues.append(_issue(
                ALIGNMENT_END_ERROR, cue.index,
                f"end error {end_error_ms:.1f}ms exceeds {tolerance_ms:.0f}ms"))

    for index in sorted(set(reference_by_index) - matched):
        issues.append(_issue(MISSING_ALIGNMENT, index,
                             "reference cue has no aligned cue"))
    return issues


# ---------------------------------------------------------------------------
# AUD-06 — word count / duration / speech-rate rules
# ---------------------------------------------------------------------------

def validate_rules(
    cues: Sequence[SubtitleCue],
    *,
    max_words: int = MAX_WORDS_PER_CUE,
    max_duration_s: float = MAX_CUE_DURATION_S,
    min_duration_s: float = MIN_CUE_DURATION_S,
    min_words_per_second: float = MIN_WORDS_PER_SECOND,
    max_words_per_second: float = MAX_WORDS_PER_SECOND,
) -> list[SubtitleIssue]:
    """Flag cues that need manual confirmation, without modifying them.

    The contract is deliberately non-destructive: a 13-word cue yields a
    ``TOO_MANY_WORDS`` item, it is never split or truncated automatically.
    """
    issues: list[SubtitleIssue] = []
    for cue in cues:
        words = cue.word_count
        if words == 0:
            issues.append(_issue(EMPTY_TEXT, cue.index, "cue has no words"))
            continue
        if not _finite(cue.duration):
            issues.append(_issue(INVALID_TIME, cue.index,
                                 "cue has a non-finite duration", manual=False))
            continue

        if words > max_words:
            issues.append(_issue(
                TOO_MANY_WORDS, cue.index,
                f"{words} words exceeds the {max_words}-word per-cue limit"))
        if cue.duration > max_duration_s + _EPS:
            issues.append(_issue(
                CUE_TOO_LONG, cue.index,
                f"{cue.duration:.2f}s exceeds the {max_duration_s:.2f}s limit"))
        if cue.duration < min_duration_s - _EPS:
            issues.append(_issue(
                CUE_TOO_SHORT, cue.index,
                f"{cue.duration:.2f}s is below the {min_duration_s:.2f}s minimum"))
        if cue.duration > 0:
            rate = words / cue.duration
            if rate > max_words_per_second + _EPS:
                issues.append(_issue(
                    RATE_TOO_FAST, cue.index,
                    f"{rate:.2f} words/s exceeds {max_words_per_second:.2f} words/s"))
            if rate < min_words_per_second - _EPS:
                issues.append(_issue(
                    RATE_TOO_SLOW, cue.index,
                    f"{rate:.2f} words/s is below {min_words_per_second:.2f} words/s"))
    return issues


def manual_review_items(issues: Iterable[SubtitleIssue]) -> list[SubtitleIssue]:
    """Return only the items a human must confirm (AUD-06 no-auto-fix rule)."""
    return [issue for issue in issues if issue.requires_manual_confirmation]


# ---------------------------------------------------------------------------
# LOC-02 — English line breaking and safe area
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafeArea:
    """Normalized (0..1) rectangle a burned-in subtitle must stay inside."""

    name: str
    version: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    calibrated: bool = False

    def contains(self, rect: "SubtitleRect") -> bool:
        return (
            rect.x >= self.x_min - _EPS
            and rect.x + rect.width <= self.x_max + _EPS
            and rect.y >= self.y_min - _EPS
            and rect.y + rect.height <= self.y_max + _EPS
        )


# P0 provisional TikTok 9:16 preset. The right rail (interaction buttons) and
# the bottom caption/music band are reserved. Marked uncalibrated on purpose
# because VIS-06 requires the preset to be confirmed before delivery.
TIKTOK_9X16 = SafeArea(
    name="tiktok-9x16",
    version="2026.11-p0-provisional",
    x_min=0.05,
    x_max=0.80,
    y_min=0.10,
    y_max=0.75,
    calibrated=False,
)


@dataclass(frozen=True)
class SubtitleRect:
    """Normalized subtitle burn-in rectangle (x, y = top-left)."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class SubtitleStyle:
    """English subtitle layout spec checked by :func:`validate_style`."""

    max_words_per_cue: int = MAX_WORDS_PER_CUE
    max_duration_s: float = MAX_CUE_DURATION_S
    max_chars_per_line: int = MAX_LINE_CHARS
    max_lines: int = MAX_LINES
    safe_area: SafeArea = TIKTOK_9X16


DEFAULT_STYLE = SubtitleStyle()


def wrap_english_text(
    text: str,
    *,
    max_chars_per_line: int = MAX_LINE_CHARS,
    max_lines: int = MAX_LINES,
) -> list[str]:
    """Break English text at word boundaries, never mid-word.

    Greedy word wrapping is deterministic and matches the overseas
    short-video habit of up to two balanced lines. A word longer than
    ``max_chars_per_line`` is kept whole on its own line so the caller can
    flag it instead of splitting it silently.
    """
    if max_chars_per_line < 1:
        raise ValueError("max_chars_per_line must be >= 1")
    if max_lines < 1:
        raise ValueError("max_lines must be >= 1")
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars_per_line:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def validate_style(
    text: str,
    style: SubtitleStyle = DEFAULT_STYLE,
) -> list[SubtitleIssue]:
    """Validate one cue's text against LOC-02 line-breaking rules."""
    if not text.strip():
        return [_issue(EMPTY_TEXT, None, "cue has no words")]
    lines = wrap_english_text(
        text,
        max_chars_per_line=style.max_chars_per_line,
        max_lines=style.max_lines,
    )
    issues: list[SubtitleIssue] = []
    if len(lines) > style.max_lines:
        issues.append(_issue(
            STYLE_TOO_MANY_LINES, None,
            f"{len(lines)} lines exceed the {style.max_lines}-line limit"))
    for number, line in enumerate(lines, start=1):
        if len(line) > style.max_chars_per_line:
            issues.append(_issue(
                STYLE_LINE_TOO_LONG, None,
                f"line {number} is {len(line)} characters, "
                f"limit {style.max_chars_per_line}"))
    if len(lines) > 1 and len(lines[-1].split()) == 1:
        issues.append(_issue(
            STYLE_ORPHAN_WORD, None,
            f"final line {lines[-1]!r} is a single orphan word"))
    return issues


def validate_safe_area(
    rect: SubtitleRect,
    safe_area: SafeArea = TIKTOK_9X16,
) -> list[SubtitleIssue]:
    """Check a normalized subtitle rectangle against a versioned safe area.

    The provisional P0 preset always adds a manual-calibration item: VIS-06
    says an unconfirmed preset blocks release, so fitting alone is not
    enough.
    """
    issues: list[SubtitleIssue] = []
    values = (rect.x, rect.y, rect.width, rect.height)
    if not all(_finite(v) for v in values) or rect.width <= 0 or rect.height <= 0:
        return [_issue(SAFE_AREA_INVALID_RECT, None,
                       f"rectangle must be finite and positive, got {rect!r}",
                       manual=False)]
    if (rect.x < 0 or rect.y < 0
            or rect.x + rect.width > 1 + _EPS
            or rect.y + rect.height > 1 + _EPS):
        issues.append(_issue(SAFE_AREA_INVALID_RECT, None,
                             f"rectangle {rect!r} is outside normalized bounds"))
        return issues
    if not safe_area.contains(rect):
        issues.append(_issue(
            SAFE_AREA_VIOLATION, None,
            f"rectangle {rect!r} escapes safe area {safe_area.name}@"
            f"{safe_area.version}"))
    if not safe_area.calibrated:
        issues.append(_issue(
            SAFE_AREA_PRESET_UNCALIBRATED, None,
            f"safe area {safe_area.name}@{safe_area.version} is provisional "
            "and must be calibrated against the target app"))
    return issues