"""Deterministic duration -> retention structure scaling (BEAT-01..08).

The beat map is an independent stage artifact (BEAT-02): a retention skeleton
that does NOT reuse the script schema. Everything here is pure, table-driven
and deterministic -- M0 has no external API and no model calls.

Declared scaling function ``beat-scale-v1`` (BEAT-01/05):

- Segment ratios come from a table anchored at 90/120/180s and are linearly
  interpolated for durations in between (clamped outside the anchors).
- Minimum hook candidates = ``max(3, ceil(duration / 25s))``.
- Turn targets = ``max(2, round(duration / 30s))`` evenly spread reversals at
  ``(i+1) * duration / (targets + 1)``; the machine check allows one beat of
  quantization on top of the nominal ``turn_interval_s``.
- The default 120s duration produces exactly 7 segments with continuous
  timestamps.

Machine checks prove timestamps, counts and density. They never prove that a
hook or cliffhanger WORKS: ``metadata.narrative`` and
``metadata.human_confirmation`` carry structural fields that a human must fill
and confirm (BEAT-03/04/07/08). Marking is not content validity.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

import yaml

SCALING_RULE_ID = "beat-scale-v1"
SCHEMA_VERSION = "1.0"

DEFAULT_DURATION_S = 120.0
MIN_DURATION_S = 30.0
MAX_DURATION_S = 600.0

# BEAT-04: first 1s carries conflict, 0-3s is the hard hook window.
HOOK_WINDOW_S = 3.0
HOOK_WINDOW_START_S = 0.0

# BEAT-03: default 120s -> 5 candidates; shorter/longer derive from the same
# declared function.
HOOK_CANDIDATES_PER_S = 1.0 / 25.0
MIN_HOOK_CANDIDATES = 3

# Beat subdivision granularity (creative reference: 3-6s shots, <=10s beats).
TARGET_BEAT_S = 10.0
TURN_TARGET_PERIOD_S = 30.0

SEGMENT_LABELS: tuple[str, ...] = (
    "HOOK",
    "SETUP",
    "RISING",
    "MID_TURN",
    "ESCALATION",
    "CLIMAX",
    "RESOLUTION",
)

# Table anchors (BEAT-01): every profile sums to 1.0 and keeps 7 segments.
_ANCHOR_RATIOS: dict[int, tuple[float, ...]] = {
    90: (0.06, 0.16, 0.20, 0.18, 0.18, 0.14, 0.08),
    120: (0.05, 0.17, 0.21, 0.19, 0.18, 0.13, 0.07),
    180: (0.05, 0.18, 0.22, 0.19, 0.18, 0.12, 0.06),
}

_TOL = 1e-6

TROPE_PATH = Path(__file__).resolve().parents[1] / "data" / "tropes" / "english_tropes.yaml"
MIN_TROPE_COUNT = 5
REQUIRED_TROPE_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "summary",
    "conflict_engine",
    "hook_devices",
    "cultural_notes",
    "sample_premise_en",
)


class BeatMapError(ValueError):
    """Raised when a duration or trope library cannot produce a valid map."""


# ---------------------------------------------------------------------------
# Declared scaling function (BEAT-01/05)
# ---------------------------------------------------------------------------


def _checked_duration(duration_s: float) -> float:
    try:
        duration = float(duration_s)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise BeatMapError(f"duration_s must be numeric, got {duration_s!r}") from exc
    if not math.isfinite(duration):
        raise BeatMapError(f"duration_s must be finite, got {duration_s!r}")
    if duration < MIN_DURATION_S or duration > MAX_DURATION_S:
        raise BeatMapError(
            f"duration_s {duration} outside supported range "
            f"[{MIN_DURATION_S}, {MAX_DURATION_S}]"
        )
    return duration


def segment_ratios(duration_s: float) -> tuple[float, ...]:
    """Return the 7 segment ratios for ``duration_s`` (table + interpolation)."""
    duration = _checked_duration(duration_s)
    anchors = sorted(_ANCHOR_RATIOS)
    if duration <= anchors[0]:
        ratios = _ANCHOR_RATIOS[anchors[0]]
    elif duration >= anchors[-1]:
        ratios = _ANCHOR_RATIOS[anchors[-1]]
    else:
        lower, upper = anchors[0], anchors[-1]
        for lo, hi in zip(anchors, anchors[1:]):
            if lo <= duration <= hi:
                lower, upper = lo, hi
                break
        t = (duration - lower) / (upper - lower)
        ratios = tuple(
            a + (b - a) * t
            for a, b in zip(_ANCHOR_RATIOS[lower], _ANCHOR_RATIOS[upper])
        )
    total = sum(ratios)
    return tuple(round(r / total, 6) for r in ratios)


def min_hook_candidates(duration_s: float) -> int:
    """Minimum hook candidates = max(3, ceil(duration / 25s)) (BEAT-03)."""
    duration = _checked_duration(duration_s)
    return max(MIN_HOOK_CANDIDATES, math.ceil(duration * HOOK_CANDIDATES_PER_S - _TOL))


def turn_target_count(duration_s: float) -> int:
    """Number of information reversals to schedule."""
    duration = _checked_duration(duration_s)
    return max(2, int(round(duration / TURN_TARGET_PERIOD_S)))


def turn_interval_s(duration_s: float) -> float:
    """Nominal maximum gap between reversals.

    Generation snaps reversals onto beat boundaries, so the validator allows
    one beat of quantization on top of this nominal interval.
    """
    duration = _checked_duration(duration_s)
    return duration / turn_target_count(duration)


# ---------------------------------------------------------------------------
# Structure construction
# ---------------------------------------------------------------------------


def build_segments(duration_s: float = DEFAULT_DURATION_S) -> list[dict[str, Any]]:
    """Build the 7 retention segments with continuous, gap-free timestamps."""
    duration = _checked_duration(duration_s)
    ratios = segment_ratios(duration)
    bounds: list[float] = [0.0]
    cumulative = 0.0
    for ratio in ratios[:-1]:
        cumulative += ratio
        bounds.append(round(duration * cumulative, 3))
    bounds.append(round(duration, 3))
    # Keep the timeline strictly increasing after rounding.
    for i in range(1, len(bounds)):
        if bounds[i] <= bounds[i - 1]:
            bounds[i] = round(bounds[i - 1] + 0.001, 3)
    bounds[-1] = round(duration, 3)

    segments = []
    for i, label in enumerate(SEGMENT_LABELS):
        segments.append(
            {
                "segment_id": f"s{i + 1}",
                "label": label,
                "start_s": bounds[i],
                "end_s": bounds[i + 1],
            }
        )
    return segments


def _segment_spans(start: float, end: float, label: str) -> list[tuple[float, float]]:
    """Split a segment into beat spans; HOOK is split at the 3s hard window."""
    spans: list[tuple[float, float]] = []
    cursor = start
    if label == "HOOK":
        first_end = round(min(cursor + HOOK_WINDOW_S, end), 3)
        if first_end <= cursor:  # pragma: no cover - requires <1ms segment
            first_end = end
        spans.append((cursor, first_end))
        cursor = first_end
    while end - cursor > _TOL:
        remaining = end - cursor
        chunks = max(1, math.ceil(remaining / TARGET_BEAT_S))
        nxt = round(cursor + remaining / chunks, 3)
        if nxt >= end - 1e-6 or nxt <= cursor:
            nxt = end
        spans.append((cursor, nxt))
        cursor = nxt
    return spans


def _even_indices(count: int, limit: int) -> list[int]:
    """Pick ``count`` distinct indices from ``range(limit)`` including 0."""
    if limit <= 0:  # pragma: no cover - construction always has beats
        return []
    count = min(count, limit)
    if count == 1:
        return [0]
    picked = sorted({round(i * (limit - 1) / (count - 1)) for i in range(count)})
    filler = 0
    while len(picked) < count and filler < limit:
        if filler not in picked:
            picked.append(filler)
            picked.sort()
        filler += 1
    return picked


def _mark_turns(beats: list[dict[str, Any]], duration: float) -> None:
    targets = turn_target_count(duration)
    used: set[int] = set()
    for i in range(targets):
        target = (i + 1) * duration / (targets + 1)
        index = min(
            range(1, len(beats)),
            key=lambda j: abs(beats[j]["start_s"] - target),
        )
        if index in used:
            forward = [j for j in range(index + 1, len(beats)) if j not in used]
            backward = [j for j in range(index - 1, 0, -1) if j not in used]
            if forward:
                index = forward[0]
            elif backward:
                index = backward[0]
            else:  # pragma: no cover - more targets than beats
                break
        used.add(index)
        beats[index]["turn"] = True


def build_beats(segments: Iterable[dict[str, Any]], duration_s: float) -> list[dict[str, Any]]:
    """Build beats across all segments and mark hooks, turns and cliffhanger."""
    duration = _checked_duration(duration_s)
    spans: list[tuple[float, float]] = []
    for segment in segments:
        spans.extend(
            _segment_spans(segment["start_s"], segment["end_s"], segment["label"])
        )

    beats: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(spans):
        beats.append(
            {
                "beat_id": f"b{i + 1:02d}",
                "start_s": start,
                "end_s": end,
                "hook_candidate": False,
                "turn": False,
                "cliffhanger": False,
            }
        )

    # BEAT-03/04: the first hook candidate always starts at 0 and its beat ends
    # inside the 0-3s hard window. Remaining candidates are spread over the
    # body; the final beat is reserved for the cliffhanger.
    required = min_hook_candidates(duration)
    body_limit = max(1, len(beats) - 1)
    for index in _even_indices(required, body_limit):
        beats[index]["hook_candidate"] = True

    _mark_turns(beats, duration)

    # BEAT-07: the last beat carries the serialized cliffhanger marker.
    beats[-1]["cliffhanger"] = True
    return beats


# ---------------------------------------------------------------------------
# Narrative scaffold (BEAT-08) -- structural fields only, quality is human
# ---------------------------------------------------------------------------


def build_narrative_scaffold(segments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Empty causal fields for a human to fill; presence != story quality."""
    return {
        "character_goal": "",
        "obstacle": "",
        "choice": "",
        "action": "",
        "consequence": "",
        "causal_chain": [
            {
                "segment_id": segment["segment_id"],
                "label": segment["label"],
                "cause": "",
                "choice": "",
                "action": "",
                "consequence": "",
            }
            for segment in segments
        ],
        "episode_result": "",
        "next_episode_hook": "",
        "human_review_required": True,
    }


def build_beat_map(duration_s: float = DEFAULT_DURATION_S) -> dict[str, Any]:
    """Build a complete beat map artifact matching beat_map.schema.json."""
    duration = _checked_duration(duration_s)
    segments = build_segments(duration)
    beats = build_beats(segments, duration)
    ratios = segment_ratios(duration)
    return {
        "version": SCHEMA_VERSION,
        "duration_s": round(duration, 3),
        "scaling_rule": SCALING_RULE_ID,
        "segments": segments,
        "beats": beats,
        "metadata": {
            "scaling": {
                "rule": SCALING_RULE_ID,
                "segment_ratios": list(ratios),
                "min_hook_candidates": min_hook_candidates(duration),
                "hook_window_s": HOOK_WINDOW_S,
                "turn_targets": turn_target_count(duration),
                "turn_interval_s": round(turn_interval_s(duration), 3),
            },
            "narrative": build_narrative_scaffold(segments),
            "human_confirmation": {
                "hook_valid": "unconfirmed",
                "cliffhanger_valid": "unconfirmed",
                "note": "Machine markers are not content validity; a human must confirm.",
            },
        },
    }


# ---------------------------------------------------------------------------
# Machine validation (BEAT-01/03/04/07/08)
# ---------------------------------------------------------------------------


def _is_continuous(items: list[dict[str, Any]], duration: float) -> list[str]:
    problems: list[str] = []
    if not items:
        return ["timeline is empty"]
    if abs(items[0]["start_s"]) > 1e-3:
        problems.append(f"timeline must start at 0, got {items[0]['start_s']}")
    for prev, nxt in zip(items, items[1:]):
        if abs(prev["end_s"] - nxt["start_s"]) > 1e-3:
            problems.append(
                f"gap/overlap between {prev.get('beat_id') or prev.get('segment_id')} "
                f"and {nxt.get('beat_id') or nxt.get('segment_id')}: "
                f"{prev['end_s']} -> {nxt['start_s']}"
            )
        if nxt["end_s"] < nxt["start_s"]:
            problems.append(f"negative duration at {nxt.get('beat_id') or nxt.get('segment_id')}")
    if abs(items[-1]["end_s"] - duration) > 1e-3:
        problems.append(f"timeline must end at {duration}, got {items[-1]['end_s']}")
    return problems


def check_hook_density(beat_map: dict[str, Any]) -> list[str]:
    """Return problems with the hook window, candidate count and turn spacing."""
    problems: list[str] = []
    duration = float(beat_map.get("duration_s", 0) or 0)
    beats = beat_map.get("beats") or []
    if not beats:
        return ["beat map has no beats"]

    hooks = [b for b in beats if b.get("hook_candidate")]
    if not hooks:
        return ["no hook_candidate beat (BEAT-03)"]

    opening = [
        b
        for b in hooks
        if b["start_s"] <= HOOK_WINDOW_START_S + 1e-3 and b["end_s"] <= HOOK_WINDOW_S + 1e-3
    ]
    if not opening:
        problems.append(
            "no hook_candidate inside the 0-3s hard window (BEAT-04): "
            f"first candidate is {hooks[0]['start_s']}-{hooks[0]['end_s']}"
        )

    required = min_hook_candidates(duration)
    if len(hooks) < required:
        problems.append(
            f"hook density too low: {len(hooks)} candidates < required {required} "
            f"for {duration}s (BEAT-03)"
        )

    turns = sorted(b["start_s"] for b in beats if b.get("turn"))
    if len(turns) < 2:
        problems.append("fewer than 2 turn beats")
    bound = turn_interval_s(duration) + TARGET_BEAT_S + 1e-3
    for prev, nxt in zip(turns, turns[1:]):
        if nxt - prev > bound:
            problems.append(
                f"turn gap {round(nxt - prev, 3)}s exceeds {round(bound, 3)}s "
                f"for {duration}s (BEAT-01/03)"
            )
    return problems


def validate_beat_map(beat_map: dict[str, Any]) -> list[str]:
    """Return a list of machine-check problems; empty means structurally valid."""
    problems: list[str] = []
    if not isinstance(beat_map, dict):
        return ["beat map must be an object"]
    for key in ("version", "duration_s", "segments", "beats"):
        if key not in beat_map:
            problems.append(f"missing required field '{key}'")
    if problems:
        return problems

    try:
        duration = _checked_duration(beat_map["duration_s"])
    except BeatMapError as exc:
        return [str(exc)]

    segments = beat_map.get("segments") or []
    beats = beat_map.get("beats") or []
    problems.extend(f"segments: {p}" for p in _is_continuous(segments, duration))
    problems.extend(f"beats: {p}" for p in _is_continuous(beats, duration))

    labels = [s.get("label") for s in segments]
    if abs(duration - DEFAULT_DURATION_S) <= 1e-6 and len(segments) != len(SEGMENT_LABELS):
        problems.append(
            f"default {DEFAULT_DURATION_S:g}s must produce {len(SEGMENT_LABELS)} "
            f"segments, got {len(segments)}"
        )
    unknown = [label for label in labels if label not in SEGMENT_LABELS]
    if unknown:
        problems.append(f"unknown segment labels: {unknown}")
    for prev, nxt in zip(labels, labels[1:]):
        if prev in SEGMENT_LABELS and nxt in SEGMENT_LABELS:
            if SEGMENT_LABELS.index(nxt) <= SEGMENT_LABELS.index(prev):
                problems.append(f"segment order violated: {prev} -> {nxt}")

    problems.extend(check_hook_density(beat_map))

    if not beats[-1].get("cliffhanger"):
        problems.append("last beat must be marked cliffhanger (BEAT-07)")

    narrative = (beat_map.get("metadata") or {}).get("narrative")
    if not isinstance(narrative, dict):
        problems.append("metadata.narrative missing (BEAT-08)")
    else:
        for field in ("character_goal", "obstacle", "choice", "action", "consequence"):
            if field not in narrative:
                problems.append(f"metadata.narrative missing '{field}' (BEAT-08)")
        if narrative.get("human_review_required") is not True:
            problems.append("metadata.narrative.human_review_required must be true")
        chain = narrative.get("causal_chain")
        if not isinstance(chain, list) or len(chain) < len(segments):
            problems.append("metadata.narrative.causal_chain must cover every segment")
    return problems


def validate_schema(
    beat_map: dict[str, Any],
    schema_path: Path | None = None,
) -> None:
    """Raise jsonschema.ValidationError if the artifact violates the schema."""
    import json

    from jsonschema import Draft202012Validator

    path = schema_path or (
        Path(__file__).resolve().parents[1] / "schemas" / "artifacts" / "beat_map.schema.json"
    )
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(beat_map)


# ---------------------------------------------------------------------------
# English trope library (BEAT-06)
# ---------------------------------------------------------------------------


def load_tropes(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load and validate the culture-adapted English trope library."""
    trope_path = Path(path) if path is not None else TROPE_PATH
    if not trope_path.exists():
        raise BeatMapError(f"trope library not found: {trope_path}")
    data = yaml.safe_load(trope_path.read_text(encoding="utf-8")) or {}
    tropes = data.get("tropes")
    if not isinstance(tropes, list):
        raise BeatMapError("trope library must contain a 'tropes' list")
    if len(tropes) < MIN_TROPE_COUNT:
        raise BeatMapError(
            f"trope library needs >= {MIN_TROPE_COUNT} tropes, got {len(tropes)}"
        )

    seen: set[str] = set()
    for index, trope in enumerate(tropes):
        if not isinstance(trope, dict):
            raise BeatMapError(f"trope #{index} must be a mapping")
        missing = [f for f in REQUIRED_TROPE_FIELDS if not trope.get(f)]
        if missing:
            raise BeatMapError(f"trope #{index} missing fields: {missing}")
        if trope["id"] in seen:
            raise BeatMapError(f"duplicate trope id: {trope['id']}")
        seen.add(trope["id"])
        if not isinstance(trope["hook_devices"], list) or not trope["hook_devices"]:
            raise BeatMapError(f"trope {trope['id']} needs hook_devices list")
    return tropes