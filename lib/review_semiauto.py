"""REV-12 semi-automatic release: machine-PASS items auto-accept, must-view and
UNKNOWN never do (P1 automation anchor, D9/F12).

This layer sits on top of :mod:`lib.review` and reuses its single mutation
authority — auto decisions go through ``ReviewStore.decide`` (append-only,
schema-validated) and are compensable with ``ReviewStore.undo``. It never
rewrites the review log, never fabricates close-read / identity / lip-sync
records, and never turns ``UNKNOWN`` into ``PASS``.

Eligibility (all must hold):

- the item has a machine check recorded by :meth:`SemiAutoRelease.record_check`;
- the latest machine result is ``PASS`` (``FAIL`` and ``UNKNOWN`` stay human);
- the item is not a REV-02 must-view item: ``priority == 0`` or any
  ``priority_reasons`` entry in :data:`MUST_VIEW_REASONS`;
- the item is still undecided.

Every auto decision carries ``decided_by = "auto:rev-12"`` and a note pointing
at the machine check. Alongside the review log this module appends an audit
ledger (``review_semiauto.jsonl`` in the episode directory) recording the
machine check read, the batch id, the accepted candidate version and any
rollback. Batch rollback supersedes via ``undo`` events, so history survives.

M0: local files only, zero external API calls, mock-first.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from lib.review import ReviewDecision, ReviewStore

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
MACHINE_RESULTS = frozenset({PASS, FAIL, UNKNOWN})

#: Marker written into ``decision.decided_by`` for every automatic release.
AUTO_DECIDED_BY = "auto:rev-12"

#: Ledger filename, sibling of ``reviews.jsonl`` inside the episode directory.
LEDGER_FILE = "review_semiauto.jsonl"

#: ``ReviewItem.priority`` 0 means "must view" (REV-02); never auto-release it.
MUST_VIEW_PRIORITY = 0

#: Priority reasons that force human review even when ``priority > 0`` (REV-02):
#: hook, climax, manually flagged, identity unknown / low face similarity, and
#: visible lip-sync dialogue.
MUST_VIEW_REASONS = frozenset({
    "hook",
    "climax",
    "manual_flag",
    "manual_suspicion",
    "identity_unknown",
    "low_face_similarity",
    "visible_lip_sync_dialogue",
})

# Skip reasons surfaced by eligibility()/auto_accept().
SKIP_ALREADY_DECIDED = "already_decided"
SKIP_NO_MACHINE_CHECK = "no_machine_check"
SKIP_MACHINE_UNKNOWN = "machine_unknown"
SKIP_MACHINE_FAIL = "machine_fail"
SKIP_MUST_VIEW = "must_view"

_LEDGER_LOCK = threading.Lock()


class SemiAutoError(Exception):
    """Base error for the semi-automatic release layer."""


class NotAutoDecisionError(SemiAutoError):
    """Rollback targets a decision that was not made by this layer."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_reason(value: Any) -> str:
    return str(value).strip().lower()


def is_must_view(item: dict[str, Any]) -> bool:
    """True when a REV-02 must-view signal is present on the item snapshot."""
    try:
        priority = int(item.get("priority", MUST_VIEW_PRIORITY) or 0)
    # risk:swallowed-exception reason:"priority 来自既有审核日志的脏数据，无法解析时按必看处理以免误放行人工必审项，绝不退回自动放行" owner:@kevin reviewed:2026-11-11
    except (TypeError, ValueError):
        return True
    if priority <= MUST_VIEW_PRIORITY:
        return True
    reasons = {_norm_reason(r) for r in item.get("priority_reasons") or []}
    return bool(reasons & MUST_VIEW_REASONS)


def is_auto_decision(decision: Optional[dict[str, Any]]) -> bool:
    """True when ``decision`` was written by this layer."""
    return bool(decision) and decision.get("decided_by") == AUTO_DECIDED_BY


@dataclass(frozen=True)
class MachineCheck:
    """One machine check result for a review item (input to REV-12)."""

    review_id: str
    result: str
    machine: str = ""
    detail: str = ""
    checked_at: str = field(default_factory=_now)


class SemiAutoRelease:
    """Append-only machine-check ledger + auto-accept / rollback authority."""

    def __init__(self, store: ReviewStore):
        self.store = store
        self.path: Path = store.dir / LEDGER_FILE

    # --- ledger (append-only) ---

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        record = {"version": "1.0", "project": self.store.project,
                  "episode": self.store.episode, **event}
        self.store.dir.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out

    # --- machine checks ---

    def record_check(self, review_id: str, result: str, *, machine: str = "",
                     detail: str = "", checked_at: Optional[str] = None) -> dict[str, Any]:
        """Append a machine check; the latest event per review wins."""
        if result not in MACHINE_RESULTS:
            raise ValueError(
                f"invalid machine result '{result}' (expected one of {sorted(MACHINE_RESULTS)})")
        self.store.get(review_id)  # fail loud on an unknown review id
        return self._append({
            "event_type": "machine_check",
            "review_id": review_id,
            "result": result,
            "machine": machine,
            "detail": detail,
            "checked_at": checked_at or _now(),
        })

    def checks(self) -> dict[str, dict[str, Any]]:
        """Latest machine check per review_id, folded from the ledger."""
        out: dict[str, dict[str, Any]] = {}
        for rec in self._events():
            if rec.get("event_type") == "machine_check":
                out[rec["review_id"]] = rec
        return out

    def check_for(self, review_id: str) -> Optional[dict[str, Any]]:
        return self.checks().get(review_id)

    # --- eligibility ---

    def eligibility(self, review_id: str) -> dict[str, Any]:
        """Why an item is (or is not) eligible for automatic acceptance."""
        eff = self.store.get(review_id)
        item = eff["item"]
        decision = eff["decision"]
        check = self.check_for(review_id)
        result = check.get("result") if check else None

        reason: Optional[str] = None
        if decision is not None:
            reason = SKIP_ALREADY_DECIDED
        elif check is None:
            reason = SKIP_NO_MACHINE_CHECK
        elif result == UNKNOWN:
            reason = SKIP_MACHINE_UNKNOWN
        elif result != PASS:
            reason = SKIP_MACHINE_FAIL
        elif is_must_view(item):
            reason = SKIP_MUST_VIEW

        return {
            "review_id": review_id,
            "eligible": reason is None,
            "reason": reason,
            "machine_result": result,
            "must_view": is_must_view(item),
            "candidate_version": item.get("candidate_version"),
        }

    # --- auto accept ---

    def auto_accept(self, *, batch_id: Optional[str] = None,
                    review_ids: Optional[Iterable[str]] = None) -> dict[str, Any]:
        """Accept every eligible undecided item; append audit + review records.

        Only ``PASS`` + non-must-view + undecided items are accepted. Returns
        ``{batch_id, accepted, skipped}`` where ``skipped`` carries a reason.
        """
        batch_id = batch_id or f"semiauto-{uuid.uuid4().hex[:12]}"
        if review_ids is None:
            ids = [i["review_id"] for i in sorted(
                self.store.items(),
                key=lambda x: (int(x.get("priority", 0) or 0),
                               x.get("slot", ""), x["review_id"]))]
        else:
            ids = list(review_ids)

        accepted: list[str] = []
        skipped: list[dict[str, Any]] = []
        for rid in ids:
            el = self.eligibility(rid)
            if not el["eligible"]:
                skipped.append({"review_id": rid, "reason": el["reason"],
                                "machine_result": el["machine_result"]})
                self._append({
                    "event_type": "auto_skip",
                    "review_id": rid,
                    "batch_id": batch_id,
                    "reason": el["reason"],
                    "machine_result": el["machine_result"],
                })
                continue
            check = self.check_for(rid) or {}
            note = (
                f"REV-12 semi-auto: machine check {PASS} "
                f"({check.get('machine') or 'unspecified'} @ {check.get('checked_at')}); "
                f"non-must-view item auto-accepted")
            rec = self.store.decide(
                rid, ReviewDecision(action="accept", note=note,
                                    decided_by=AUTO_DECIDED_BY))
            self._append({
                "event_type": "auto_accept",
                "review_id": rid,
                "batch_id": batch_id,
                "machine_result": check.get("result"),
                "machine": check.get("machine", ""),
                "checked_at": check.get("checked_at"),
                "candidate_version": rec.get("candidate_version"),
                "review_version": rec.get("review_version"),
                "decided_at": rec["decision"]["decided_at"],
                "decision_created_at": rec.get("created_at"),
            })
            accepted.append(rid)
        return {"batch_id": batch_id, "accepted": accepted, "skipped": skipped}

    # --- auto decisions & rollback ---

    def _batch_map(self) -> dict[str, str]:
        """review_id -> latest batch id that auto-accepted it."""
        out: dict[str, str] = {}
        for rec in self._events():
            if rec.get("event_type") == "auto_accept":
                out[rec["review_id"]] = rec.get("batch_id", "")
        return out

    def auto_decisions(self, batch_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Currently effective auto-accepted decisions (optionally one batch)."""
        batches = self._batch_map()
        out = []
        for item in self.store.items():
            rid = item["review_id"]
            decision = self.store.get(rid)["decision"]
            if not is_auto_decision(decision):
                continue
            batch = batches.get(rid, "")
            if batch_id is not None and batch != batch_id:
                continue
            out.append({"review_id": rid, "batch_id": batch,
                        "decision": decision})
        return sorted(out, key=lambda x: x["review_id"])

    def rollback(self, review_id: str, reason: str = "") -> dict[str, Any]:
        """Undo one auto-accept via an append-only ``undo`` compensation event."""
        eff = self.store.get(review_id)
        if not is_auto_decision(eff["decision"]):
            raise NotAutoDecisionError(
                f"review '{review_id}' has no effective auto decision to roll back")
        batch = self._batch_map().get(review_id, "")
        reason = reason or "REV-12 auto-accept rolled back"
        self.store.undo(review_id, reason=reason)
        self._append({
            "event_type": "auto_rollback",
            "review_id": review_id,
            "batch_id": batch,
            "reason": reason,
        })
        return {"review_id": review_id, "batch_id": batch, "reason": reason}

    def rollback_batch(self, batch_id: str, reason: str = "") -> list[dict[str, Any]]:
        """Undo every still-effective auto decision in ``batch_id``."""
        return [self.rollback(d["review_id"], reason)
                for d in self.auto_decisions(batch_id)]

    # --- audit trail ---

    def audit(self, batch_id: Optional[str] = None) -> dict[str, Any]:
        """Join machine checks, auto decisions and rollbacks for traceability.

        Every entry states what the machine said and what decision is currently
        effective, so an automatic acceptance can always be reconstructed.
        """
        checks = self.checks()
        accepted: dict[str, dict[str, Any]] = {}
        rolled: dict[str, dict[str, Any]] = {}
        skipped: dict[str, dict[str, Any]] = {}
        for rec in self._events():
            if rec.get("event_type") == "auto_accept":
                accepted[rec["review_id"]] = rec
            elif rec.get("event_type") == "auto_rollback":
                rolled[rec["review_id"]] = rec
            elif rec.get("event_type") == "auto_skip":
                skipped[rec["review_id"]] = rec

        if batch_id is not None:
            ids = {rid for rid, rec in accepted.items() if rec.get("batch_id") == batch_id}
            ids |= {rid for rid, rec in rolled.items() if rec.get("batch_id") == batch_id}
            ids |= {rid for rid, rec in skipped.items() if rec.get("batch_id") == batch_id}
        else:
            ids = set(checks) | set(accepted) | set(skipped)

        entries = []
        for rid in sorted(ids):
            eff = self.store.get(rid)
            decision = eff["decision"]
            check = checks.get(rid)
            accept_rec = accepted.get(rid, {})
            skip_rec = skipped.get(rid, {})
            entries.append({
                "review_id": rid,
                "batch_id": (accept_rec.get("batch_id") or rolled.get(rid, {}).get("batch_id")
                             or skip_rec.get("batch_id")),
                "machine_check": ({"result": check.get("result"),
                                   "machine": check.get("machine", ""),
                                   "detail": check.get("detail", ""),
                                   "checked_at": check.get("checked_at")} if check else None),
                "decision": decision,
                "auto_accepted": is_auto_decision(decision),
                "skipped_reason": skip_rec.get("reason"),
                "rolled_back": rid in rolled,
                "rolled_back_reason": rolled.get(rid, {}).get("reason"),
            })
        return {"batch_id": batch_id, "entries": entries}