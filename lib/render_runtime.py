"""DEL-08 render_runtime governance for the short-drama delivery stage.

Proposal-stage rules (AGENT_GUIDE "Present Both Composition Runtimes"):

- every known runtime must appear in the alternatives -- Remotion AND
  HyperFrames when both are installed, plus ``ffmpeg`` when it applies;
- unavailable options are recorded as rejected instead of being dropped, so
  the audit trail shows what was offered and why it was not used;
- the user's pick is locked in a ``render_runtime_selection`` decision with
  ``metadata.user_approved: true``;
- delivery must match the last approved runtime. Silent switching is blocked:
  a missing approval or a mismatch returns BLOCKED.

Records are appended through the canonical :class:`lib.decision_log.DecisionLog`
(ORC-04, append-only JSONL, schema-validated on every write). Read helpers also
accept a cumulative ``decision_log.json`` document for projects whose checkpoint
log predates the append-only ledger.
M0: local files only, no renderer invocation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from lib.decision_log import DecisionEntry, DecisionLog

PASS = "PASS"
BLOCKED = "BLOCKED"

DECISION_CATEGORY = "render_runtime_selection"
TRACKED_RUNTIMES = ("remotion", "hyperframes", "ffmpeg")

DecisionSource = Union[DecisionLog, Path, str, Mapping[str, Any]]


class RuntimeGovernanceError(ValueError):
    """The proposal provenance is not auditable (e.g. a runtime was dropped)."""


class RuntimeBlockedError(RuntimeError):
    """Delivery runtime cannot be verified against an approved decision."""

    def __init__(self, check: "RuntimeCheck") -> None:
        super().__init__(check.reason)
        self.check = check


@dataclass(frozen=True)
class RuntimeOption:
    option_id: str
    label: str
    available: bool = True
    score: float = 0.5
    reason: str = ""


@dataclass(frozen=True)
class RuntimeProposal:
    project: str
    stage: str
    selected: str
    reason: str
    user_approved: bool
    options: tuple[RuntimeOption, ...]

    def to_decision_entry(
        self,
        *,
        operator: str = "owner",
        episode_id: Optional[str] = None,
        shot_id: Optional[str] = None,
    ) -> DecisionEntry:
        alternatives = []
        for option in self.options:
            if option.reason:
                reason = option.reason
            elif not option.available:
                reason = "runtime not available on this machine"
            elif option.option_id == self.selected:
                reason = "selected by the user"
            else:
                reason = "considered and not selected"
            alternatives.append(
                {
                    "option_id": option.option_id,
                    "label": option.label,
                    "reason": reason,
                }
            )
        metadata: dict[str, Any] = {
            "stage": self.stage,
            "user_approved": self.user_approved,
            "availability": {o.option_id: o.available for o in self.options},
            "scores": {o.option_id: o.score for o in self.options},
            "surface": "short-drama-delivery",
        }
        return DecisionEntry(
            category=DECISION_CATEGORY,
            subject=f"render runtime for {self.project}",
            decision=self.selected,
            reason=self.reason,
            alternatives=alternatives,
            episode_id=episode_id,
            shot_id=shot_id,
            operator=operator,
            metadata=metadata,
        )


@dataclass(frozen=True)
class RuntimeCheck:
    status: str
    approved: Optional[str]
    actual: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.status == PASS


def _norm(value: Any) -> str:
    return str(value).strip().lower().replace(" ", "-")


def _coerce_option(raw: Any) -> RuntimeOption:
    if isinstance(raw, RuntimeOption):
        return raw
    if isinstance(raw, str):
        return RuntimeOption(option_id=_norm(raw), label=raw)
    if isinstance(raw, Mapping):
        label = str(raw.get("label") or raw.get("option_id") or "")
        option_id = _norm(raw.get("option_id") or raw.get("id") or label)
        score = raw.get("score", 0.5)
        try:
            score = float(score)
        except (TypeError, ValueError) as exc:
            raise RuntimeGovernanceError(f"option score must be numeric: {raw!r}") from exc
        return RuntimeOption(
            option_id=option_id,
            label=label or option_id,
            available=bool(raw.get("available", True)),
            score=score,
            reason=str(raw.get("reason", "")),
        )
    raise RuntimeGovernanceError(f"unsupported runtime option: {raw!r}")


def build_runtime_proposal(
    project: str,
    options: Iterable[Any],
    selected: str,
    reason: str,
    *,
    stage: str = "idea",
    user_approved: bool = False,
    availability: Optional[Mapping[str, bool]] = None,
) -> RuntimeProposal:
    """Build a proposal-stage record, enforcing option coverage.

    ``availability`` is the machine's runtime inventory. Every runtime whose
    availability is known must appear in ``options`` -- available runtimes so
    the user sees the real choice, unavailable ones so the audit trail says
    why they were not used.
    """
    parsed = [_coerce_option(option) for option in options]
    if not parsed:
        raise RuntimeGovernanceError("at least one runtime option must be listed")
    if not str(reason).strip():
        raise RuntimeGovernanceError("reason is required for a runtime decision")

    ids = [option.option_id for option in parsed]
    if len(ids) != len(set(ids)):
        raise RuntimeGovernanceError(f"duplicate runtime options: {ids}")

    if availability:
        for runtime in TRACKED_RUNTIMES:
            if runtime in availability and runtime not in ids:
                state = "available" if availability[runtime] else "unavailable"
                raise RuntimeGovernanceError(
                    f"runtime {runtime!r} is {state} on this machine but was not "
                    "included in alternatives; record every known runtime"
                )

    selected_id = _norm(selected)
    by_id = {option.option_id: option for option in parsed}
    if selected_id not in by_id:
        raise RuntimeGovernanceError(
            f"selected runtime {selected!r} is not one of {ids}"
        )
    if not by_id[selected_id].available:
        raise RuntimeGovernanceError(
            f"selected runtime {selected!r} is marked unavailable"
        )

    return RuntimeProposal(
        project=project,
        stage=stage,
        selected=selected_id,
        reason=reason,
        user_approved=bool(user_approved),
        options=tuple(parsed),
    )


def record_runtime_selection(
    log: DecisionLog,
    options: Iterable[Any],
    selected: str,
    reason: str,
    *,
    stage: str = "idea",
    user_approved: bool = True,
    availability: Optional[Mapping[str, bool]] = None,
    operator: str = "owner",
    episode_id: Optional[str] = None,
    shot_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append the proposal-stage runtime choice to the append-only log."""
    proposal = build_runtime_proposal(
        log.project,
        options,
        selected,
        reason,
        stage=stage,
        user_approved=user_approved,
        availability=availability,
    )
    entry = proposal.to_decision_entry(
        operator=operator, episode_id=episode_id, shot_id=shot_id
    )
    return log.append(entry)


def record_runtime_change(
    log: DecisionLog,
    options: Iterable[Any],
    selected: str,
    reason: str,
    *,
    operator: str = "owner",
    episode_id: Optional[str] = None,
    shot_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record an approved runtime switch, requiring a prior approval and a reason."""
    previous = approved_runtime(log)
    if previous is None:
        raise RuntimeGovernanceError(
            "no approved runtime to change from; record a selection first"
        )
    if _norm(selected) == previous:
        raise RuntimeGovernanceError(
            f"{previous!r} is already the approved runtime"
        )
    if not str(reason).strip():
        raise RuntimeGovernanceError("a runtime change needs an explicit reason")
    proposal = build_runtime_proposal(
        log.project,
        options,
        selected,
        reason,
        stage="delivery",
        user_approved=True,
    )
    entry = proposal.to_decision_entry(
        operator=operator, episode_id=episode_id, shot_id=shot_id
    )
    entry.metadata["changed_from"] = previous
    return log.append(entry)


# --- read-back -------------------------------------------------------------


def _source_entries(source: DecisionSource) -> list[dict[str, Any]]:
    if isinstance(source, DecisionLog):
        return source.entries()
    if isinstance(source, Mapping):
        decisions = source.get("decisions")
        if isinstance(decisions, list):
            return [dict(item) for item in decisions if isinstance(item, Mapping)]
        return [dict(source)] if "category" in source else []
    path = Path(source)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [
            json.loads(line)
            for line in text.splitlines()
            if line.strip()
        ]
    document = json.loads(text)
    if isinstance(document, list):
        return [item for item in document if isinstance(item, Mapping)]
    if isinstance(document, Mapping):
        return _source_entries(document)
    return []


def _approved_from_entry(record: Mapping[str, Any]) -> Optional[str]:
    if record.get("category") != DECISION_CATEGORY:
        return None
    metadata = record.get("metadata") or {}
    approved = metadata.get("user_approved", record.get("user_approved"))
    selected = record.get("decision") or record.get("selected")
    if not approved or not selected:
        return None
    return _norm(selected)


def approved_runtime(source: DecisionSource) -> Optional[str]:
    """Return the last user-approved runtime selection, if any."""
    approved: Optional[str] = None
    for record in _source_entries(source):
        selected = _approved_from_entry(record)
        if selected:
            approved = selected
    return approved


def runtime_changes(source: DecisionSource) -> list[dict[str, str]]:
    """Return approved runtime transitions: ``[{from, to, decision_id}]``."""
    changes: list[dict[str, str]] = []
    previous: Optional[str] = None
    for record in _source_entries(source):
        selected = _approved_from_entry(record)
        if not selected:
            continue
        if previous is not None and selected != previous:
            changes.append(
                {
                    "from": previous,
                    "to": selected,
                    "decision_id": str(record.get("decision_id", "")),
                }
            )
        previous = selected
    return changes


def verify_delivery_runtime(
    actual: str,
    *,
    approved: Optional[str] = None,
    decision_log: Optional[DecisionSource] = None,
) -> RuntimeCheck:
    """Block delivery unless ``actual`` equals the last approved runtime."""
    if approved is None and decision_log is not None:
        approved = approved_runtime(decision_log)
    actual_id = _norm(actual)
    if not approved:
        return RuntimeCheck(
            status=BLOCKED,
            approved=None,
            actual=actual_id,
            reason="no user-approved render_runtime_selection in decision_log",
        )
    approved_id = _norm(approved)
    if approved_id != actual_id:
        return RuntimeCheck(
            status=BLOCKED,
            approved=approved_id,
            actual=actual_id,
            reason=(
                f"delivery used {actual_id!r} but the approved runtime is "
                f"{approved_id!r}; log a render_runtime_selection and get "
                "user approval before switching"
            ),
        )
    return RuntimeCheck(
        status=PASS,
        approved=approved_id,
        actual=actual_id,
        reason="delivery matches the approved render runtime",
    )


def require_approved_runtime(
    actual: str,
    *,
    approved: Optional[str] = None,
    decision_log: Optional[DecisionSource] = None,
) -> RuntimeCheck:
    """Like :func:`verify_delivery_runtime`, raising on BLOCKED."""
    check = verify_delivery_runtime(
        actual, approved=approved, decision_log=decision_log
    )
    if not check.ok:
        raise RuntimeBlockedError(check)
    return check