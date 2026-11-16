"""Four director gates and the delivery full_watch (v2.5 DIR-03/DIR-05).

D17 lands the director at four gates: brief (idea), script+shot_gen, assembly
and delivery. A gate counts as closed only when an effective creative-gate
approval for that stage is recorded in reviews.jsonl (``gate_type="creative"``,
action accept or pick_variant) - the director's intent must be auditable, not
implied by the file existing.

The delivery gate additionally requires a ``full_watch`` decision that names
the exact render version (DIR-05 / REV-11). A re-render produces a new version
hash, so the old watch no longer satisfies the gate.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional, Sequence

from lib.review import ReviewDecision, ReviewStore

# Gate name -> the pipeline stages it covers (D17; script and shot_gen share
# one gate, "写作是天花板" H7).
DIRECTOR_GATE_STAGES: dict[str, tuple[str, ...]] = {
    "brief": ("idea",),
    "script_shot": ("script", "shot_gen"),
    "assembly": ("assembly",),
    "delivery": ("delivery",),
}

FULL_WATCH_KIND = "full_watch"
JUDGMENT_FIELDS = ("hook", "emotion", "pacing")


def _effective_decision(
    records: Iterable[Mapping[str, Any]], review_id: str
) -> Optional[Mapping[str, Any]]:
    current: Optional[Mapping[str, Any]] = None
    for rec in records:
        if rec.get("review_id") != review_id:
            continue
        if rec.get("event_type") == "decision":
            current = rec
        elif rec.get("event_type") == "undo":
            current = None
    return current


def _is_creative_pass(rec: Optional[Mapping[str, Any]]) -> bool:
    if not rec:
        return False
    decision = rec.get("decision") or {}
    return (
        decision.get("gate_type") == "creative"
        and decision.get("action") in {"accept", "pick_variant"}
    )


def stage_has_director_pass(records: Iterable[Mapping[str, Any]], stages: Sequence[str]) -> bool:
    """True when any effective creative approval exists in one of ``stages``."""
    records = list(records)
    seen: set[str] = set()
    for rec in records:
        rid = rec.get("review_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        effective = _effective_decision(records, rid)
        if effective and effective.get("stage") in stages and _is_creative_pass(effective):
            return True
    return False


def missing_director_gates(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """Gate names with no director creative approval yet (DIR-03)."""
    records = list(records)
    return [
        gate
        for gate, stages in DIRECTOR_GATE_STAGES.items()
        if not stage_has_director_pass(records, stages)
    ]


def require_director_gates(records: Iterable[Mapping[str, Any]]) -> None:
    """Raise when any of the four gates has no director decision."""
    missing = missing_director_gates(records)
    if missing:
        raise ValueError(
            "director gate missing for: " + ", ".join(missing) + " (DIR-03)"
        )


def record_full_watch(
    store: ReviewStore,
    review_id: str,
    version_sha: str,
    *,
    hook: str,
    emotion: str,
    pacing: str,
    decided_by: str = "director",
) -> dict[str, Any]:
    """Record the delivery full-watch with the three creative judgments."""
    if not str(version_sha).strip():
        raise ValueError("full_watch needs the render version hash")
    judgments = {"hook": hook, "emotion": emotion, "pacing": pacing}
    empty = [name for name, value in judgments.items() if not str(value).strip()]
    if empty:
        raise ValueError(
            "full_watch judgment missing: " + ", ".join(empty) + " (DIR-05)"
        )
    note = json.dumps(
        {"kind": FULL_WATCH_KIND, "version": str(version_sha), **judgments},
        ensure_ascii=False,
    )
    return store.decide(
        review_id,
        ReviewDecision(
            action="accept",
            gate_type="creative",
            evidence=f"full_watch version={version_sha}",
            note=note,
            decided_by=decided_by,
        ),
    )


def full_watch_ok(
    records: Iterable[Mapping[str, Any]], review_id: str, current_version: str
) -> bool:
    """True when the effective full_watch names this exact render version."""
    effective = _effective_decision(records, review_id)
    if not _is_creative_pass(effective) or not effective:
        return False
    decision = effective.get("decision") or {}
    try:
        note = json.loads(decision.get("note") or "{}")
    except json.JSONDecodeError:
        return False
    return (
        note.get("kind") == FULL_WATCH_KIND
        and str(note.get("version")) == str(current_version)
        and all(str(note.get(field, "")).strip() for field in JUDGMENT_FIELDS)
    )