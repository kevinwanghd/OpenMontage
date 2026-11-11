"""Durable paid-submission ledger (ORC-03, NFR-01/03, F6/F16).

Append-only record of every paid generation attempt: intent, input hash,
provider task id, billing state and reservation lifecycle. Enables crash
recovery WITHOUT duplicate paid submission:

- crash before submit -> intent still ``pending``, safe to (re)submit;
- crash after submit / before task id -> ``unknown``, MUST reconcile with the
  provider before any re-submission (never auto re-pay);
- reservation held until billing resolved; unresolved reservations escalate
  past max-age, never silently released (F16).

M0: local files only, zero external API calls. Single-writer discipline (ORC-10).
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from lib.checkpoint import PROJECTS_DIR

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "artifacts" / "submission_ledger.schema.json"
_VALIDATOR = Draft202012Validator(json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))

UNKNOWN = "unknown"
HELD = "held"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerError(Exception):
    pass


class DuplicateSubmissionError(LedgerError):
    """An unresolved attempt for the same inputs exists; do not auto re-pay."""


@dataclass
class LedgerEntry:
    intent: str
    provider: str
    input_hash: str
    episode_id: str = ""
    shot_id: Optional[str] = None
    model: Optional[str] = None
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: Optional[str] = None
    billing_state: str = "pending"
    currency: str = "CNY"
    rate: Optional[float] = None
    reservation: Optional[dict] = None
    output_verified: bool = False
    created_at: str = field(default_factory=_now)
    resolved_at: Optional[str] = None


class SubmissionLedger:
    _lock = threading.Lock()

    def __init__(self, project: str, episode: str, projects_dir: Path = PROJECTS_DIR):
        self.project = project
        self.episode = episode
        self.dir = Path(projects_dir) / project / episode
        self.path = self.dir / "ledger.jsonl"

    def _append(self, entry: LedgerEntry) -> dict[str, Any]:
        rec = {"version": "1.0", **asdict(entry)}
        _VALIDATOR.validate(rec)
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _latest(self) -> dict[str, dict[str, Any]]:
        """Latest state per entry_id (append-only updates fold by entry_id)."""
        out: dict[str, dict] = {}
        for rec in self._records():
            out[rec["entry_id"]] = rec
        return out

    def get(self, entry_id: str) -> dict[str, Any]:
        rec = self._latest().get(entry_id)
        if not rec:
            raise LedgerError(f"entry '{entry_id}' not found")
        return rec

    def find_by_input_hash(self, input_hash: str) -> list[dict[str, Any]]:
        return [r for r in self._latest().values() if r.get("input_hash") == input_hash]

    def record_intent(self, intent: str, provider: str, input_hash: str, *,
                      reserve_amount: float = 0.0, currency: str = "CNY",
                      max_age_s: float = 86400, **kw) -> dict[str, Any]:
        """Record a paid intent. Refuses if an unresolved attempt for the same
        inputs already exists (idempotency / no duplicate paid submission)."""
        for prior in self.find_by_input_hash(input_hash):
            if prior["billing_state"] in ("pending", "submitted", UNKNOWN, "reserved"):
                raise DuplicateSubmissionError(
                    f"unresolved attempt {prior['entry_id']} for same inputs; reconcile first")
        reservation = {"amount": reserve_amount, "max_age_s": max_age_s, "state": HELD} if reserve_amount else None
        entry = LedgerEntry(intent=intent, provider=provider, input_hash=input_hash,
                            currency=currency, reservation=reservation,
                            billing_state="reserved" if reservation else "pending", **kw)
        return self._append(entry)

    def _update(self, entry_id: str, **changes) -> dict[str, Any]:
        rec = dict(self.get(entry_id))
        rec.update(changes)
        entry = LedgerEntry(**{k: rec[k] for k in LedgerEntry.__dataclass_fields__ if k in rec})
        return self._append(entry)

    def mark_submitted(self, entry_id: str, task_id: Optional[str]) -> dict[str, Any]:
        return self._update(entry_id, task_id=task_id,
                            billing_state="submitted" if task_id else UNKNOWN)

    def mark_result(self, entry_id: str, billing_state: str) -> dict[str, Any]:
        if billing_state not in ("billed", "free", UNKNOWN):
            raise LedgerError(f"invalid result state '{billing_state}'")
        changes: dict[str, Any] = {"billing_state": billing_state}
        if billing_state in ("billed", "free"):
            changes["resolved_at"] = _now()
            changes["reservation"] = self._released_reservation(entry_id)
        return self._update(entry_id, **changes)

    def _released_reservation(self, entry_id: str) -> Optional[dict]:
        res = self.get(entry_id).get("reservation")
        if not res:
            return None
        return {**res, "state": "released"}

    def release_reservation(self, entry_id: str) -> dict[str, Any]:
        return self._update(entry_id, reservation=self._released_reservation(entry_id))

    def escalate_reservation(self, entry_id: str) -> dict[str, Any]:
        res = self.get(entry_id).get("reservation") or {}
        return self._update(entry_id, reservation={**res, "state": "escalated"})

    def unknown_entries(self) -> list[dict[str, Any]]:
        return [r for r in self._latest().values() if r["billing_state"] == UNKNOWN]

    def reserved_total(self, currency: str = "CNY") -> float:
        total = 0.0
        for r in self._latest().values():
            res = r.get("reservation") or {}
            if res.get("state") == HELD and r.get("currency") == currency:
                total += res.get("amount", 0.0)
        return total