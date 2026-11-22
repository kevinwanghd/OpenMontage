"""Job-level idempotency for paid generation calls (GATE-03).

Why this exists: during the 2026-11-17 investigation the agent submitted the
same kind of generation request several times while probing APIs, and a timeout
had no defined meaning. A stage-level checkpoint cannot answer "did this exact
shot already get submitted?", so the answer was usually "submit again".

The rule this module enforces is the one every provider's docs imply but none
enforce for you:

* A request is identified by its *content* (provider + model + prompt + params),
  not by when it ran.
* **`unknown` is not `failed`.** A timeout, a dropped connection, or a killed
  process means we do not know whether the provider started work. Resubmitting
  in that state is how you pay twice. The only legal move is to reconcile with
  the provider first (``recover``), and only a confirmed absence allows a new
  submission.
* Every retry records its parent and the one variable that changed, so a
  "try again with a different seed" is distinguishable from an accidental
  duplicate.

Storage is an append-only JSON array. It is intentionally boring: the value is
in the state machine, not in the persistence format.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

JOBS_FILE = Path("artifacts") / "generation_jobs.json"

#: States from which a *new* submission of the same content is a duplicate.
#: `recovered` counts as submitted: the provider turned out to be working on it.
SUBMITTED_STATES = frozenset({"submitted", "processing", "recovered",
                              "completed", "reviewed", "accepted"})
#: States that mean "we do not know what the provider did".
UNKNOWN_STATES = frozenset({"unknown"})
#: States that legitimately allow another attempt.
RETRYABLE_STATES = frozenset({"planned", "failed", "rejected", "repair", "cancelled"})
ALL_STATES = frozenset({
    "planned", "submitted", "processing", "completed", "reviewed",
    "accepted", "repair", "rejected", "cancel_requested", "cancelled",
    "unknown", "recovered", "failed",
})


class JobLedgerError(RuntimeError):
    """Base class for ledger violations."""


class DuplicateSubmission(JobLedgerError):
    """The same request is already in flight or finished."""


class UnresolvedUnknown(JobLedgerError):
    """A previous attempt is in `unknown`; reconcile with the provider first."""


@dataclass
class JobRecord:
    job_id: str
    request_fingerprint: str
    idempotency_key: str
    provider: str
    state: str = "planned"
    provider_task_id: Optional[str] = None
    authorization_scope_id: Optional[str] = None
    parent_job_id: Optional[str] = None
    changed_variable: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "request_fingerprint": self.request_fingerprint,
            "idempotency_key": self.idempotency_key,
            "provider": self.provider,
            "state": self.state,
            "provider_task_id": self.provider_task_id,
            "authorization_scope_id": self.authorization_scope_id,
            "parent_job_id": self.parent_job_id,
            "changed_variable": self.changed_variable,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        return cls(
            job_id=str(data["job_id"]),
            request_fingerprint=str(data["request_fingerprint"]),
            idempotency_key=str(data.get("idempotency_key") or ""),
            provider=str(data.get("provider") or ""),
            state=str(data.get("state") or "planned"),
            provider_task_id=data.get("provider_task_id"),
            authorization_scope_id=data.get("authorization_scope_id"),
            parent_job_id=data.get("parent_job_id"),
            changed_variable=data.get("changed_variable"),
            metadata=dict(data.get("metadata") or {}),
        )


def request_fingerprint(provider: str, model: str, prompt: str, params: Any) -> str:
    """Content hash: same inputs must produce the same fingerprint."""
    payload = json.dumps(
        {"provider": provider, "model": model, "prompt": prompt, "params": params},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key(fingerprint: str, attempt: int = 0) -> str:
    """Stable per (content, attempt). Attempt 0 is the first submission."""
    return hashlib.sha256(f"{fingerprint}:{int(attempt)}".encode("utf-8")).hexdigest()[:32]


class JobLedger:
    """Append-only record of generation attempts for one project."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    # -- persistence -----------------------------------------------------
    def _read(self) -> list[JobRecord]:
        if not self.path.is_file():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise JobLedgerError(f"job ledger root must be a list: {self.path}")
        return [JobRecord.from_dict(item) for item in raw if isinstance(item, dict)]

    def _write(self, records: Iterable[JobRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [record.to_dict() for record in records]
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- queries ---------------------------------------------------------
    def records(self) -> list[JobRecord]:
        return self._read()

    def by_fingerprint(self, fingerprint: str) -> list[JobRecord]:
        return [r for r in self._read() if r.request_fingerprint == fingerprint]

    def latest(self, fingerprint: str) -> Optional[JobRecord]:
        matches = self.by_fingerprint(fingerprint)
        return matches[-1] if matches else None

    def resubmission_allowed(self, fingerprint: str) -> bool:
        """True only when every prior attempt is in a settled, failed state."""
        # One rule, shared with submit(): only an unresolved `unknown` or a
        # live attempt blocks. Deriving this from a separate allow-list let the
        # query and the enforcement disagree (e.g. `cancelled`).
        prior = self.by_fingerprint(fingerprint)
        return not any(
            r.state in UNKNOWN_STATES or r.state in SUBMITTED_STATES for r in prior
        )

    # -- transitions -----------------------------------------------------
    def submit(self, record: JobRecord) -> JobRecord:
        """Record a new submission, refusing duplicates and unresolved unknowns."""
        existing = self.by_fingerprint(record.request_fingerprint)
        unknown = [r for r in existing if r.state in UNKNOWN_STATES]
        if unknown:
            raise UnresolvedUnknown(
                f"previous attempt {unknown[-1].job_id} for this request is in "
                f"'unknown' state; reconcile with the provider (record.recover) "
                f"before submitting again"
            )
        in_flight = [r for r in existing if r.state in SUBMITTED_STATES]
        if in_flight:
            raise DuplicateSubmission(
                f"request already {in_flight[-1].state} as {in_flight[-1].job_id}; "
                f"refusing a duplicate paid submission"
            )
        if record.state == "planned":
            record.state = "submitted"
        self._validate_state(record.state)
        records = self._read()
        records.append(record)
        self._write(records)
        return record

    def mark_unknown(self, job_id: str, reason: str = "") -> JobRecord:
        """A timeout/drop is *unknown*, never *failed*."""
        # A dropped call can only happen while work is in flight; a finished
        # job whose result was lost is a different problem (reconcile, do not
        # re-submit), so only the live states may become unknown.
        return self._transition(job_id, frozenset({"submitted", "processing", "recovered"}),
                                "unknown",
                                metadata={"unknown_reason": reason} if reason else None)

    def recover(self, job_id: str, *, found: bool, provider_task_id: str = "",
                metadata: Optional[dict[str, Any]] = None) -> JobRecord:
        """Resolve an `unknown` after checking the provider.

        ``found=True`` means the provider did start the work, so the job moves
        to `recovered` and no resubmission is needed. ``found=False`` means the
        work never started, so the job settles as `failed` and a retry is legal.
        """
        # `found=True` resolves to the dedicated `recovered` state (PRD GATE-03:
        # `unknown→recovered|failed`), which is treated as submitted so the same
        # request still cannot be paid for twice.
        target = "recovered" if found else "failed"
        payload: dict[str, Any] = {"recovered": found}
        if provider_task_id:
            payload["provider_task_id"] = provider_task_id
        payload.update(metadata or {})
        return self._transition(job_id, UNKNOWN_STATES, target,
                                provider_task_id=provider_task_id or None,
                                metadata=payload)

    def transition(self, job_id: str, target_state: str,
                   *, metadata: Optional[dict[str, Any]] = None) -> JobRecord:
        self._validate_state(target_state)
        return self._transition(job_id, None, target_state, metadata=metadata)

    # -- internals -------------------------------------------------------
    @staticmethod
    def _validate_state(state: str) -> None:
        if state not in ALL_STATES:
            raise JobLedgerError(f"unknown job state {state!r}; allowed: {sorted(ALL_STATES)}")

    def _transition(self, job_id: str, allowed_from: Optional[frozenset], target: str,
                    *, provider_task_id: Optional[str] = None,
                    metadata: Optional[dict[str, Any]] = None) -> JobRecord:
        records = self._read()
        for record in records:
            if record.job_id != job_id:
                continue
            if allowed_from is not None and record.state not in allowed_from:
                raise JobLedgerError(
                    f"job {job_id} is in state {record.state!r}; "
                    f"only {sorted(allowed_from)} can move to {target!r}"
                )
            record.state = target
            if provider_task_id:
                record.provider_task_id = provider_task_id
            if metadata:
                record.metadata.update(metadata)
            self._write(records)
            return record
        raise JobLedgerError(f"unknown job id {job_id!r}")

# -- provider wiring -------------------------------------------------------

def ledger_for_artifact(artifact_path: Path | str) -> Optional[JobLedger]:
    """Find the project that owns ``artifact_path`` and open its ledger.

    Provider tools only know the file they are about to write. Walking up to
    the nearest directory with an ``artifacts/`` child is what lets the ledger
    be attached without asking every caller to pass a project id.
    """
    path = Path(artifact_path)
    try:
        path = path.resolve()
    except OSError:
        path = Path(path.absolute())
    for candidate in (path.parent, *path.parent.parents):
        if (candidate / "artifacts").is_dir():
            return JobLedger(candidate / JOBS_FILE)
    return None


def begin_submission(ledger: JobLedger, *, provider: str, model: str,
                     prompt: str, params: Any,
                     job_id: Optional[str] = None) -> JobRecord:
    """Record an attempt *before* the paid call is made.

    Raises :class:`DuplicateSubmission` / :class:`UnresolvedUnknown` when the
    same request must not be paid for again; the caller turns that into a
    refused run.
    """
    fingerprint = request_fingerprint(provider, model, prompt, params)
    record = JobRecord(
        job_id=job_id or uuid.uuid4().hex,
        request_fingerprint=fingerprint,
        idempotency_key=idempotency_key(fingerprint),
        provider=provider,
        metadata={"model": model},
    )
    return ledger.submit(record)
