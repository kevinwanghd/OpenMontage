"""Agent-ready error contract shared by API, CLI and doctor (X-2, §10.3).

Every rejection carries a stable machine code plus the human-repair fields the
contract requires: ``what`` (operation + actual value), ``fix`` (safe next
command), ``recovery`` (retry | fix-then-retry | do-X-first | ask-human),
``side_effects`` (none | partial | complete | unknown), ``context`` and an
optional ``trace`` for unexpected failures.

Review validation is NOT re-implemented here: the mapping below classifies the
exception raised by ``lib.review.ReviewStore.decide`` (the single mutation
authority, X-11) using the same constants it validates against.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from lib.review import ACTIONS, REASON_CODES, STAGE_REASON_CODES


class ErrorCode(str, Enum):
    """Stable, machine-checkable error catalog (§10.3)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_ACTION = "INVALID_ACTION"
    MISSING_REASON_CODE = "MISSING_REASON_CODE"
    UNKNOWN_REASON_CODE = "UNKNOWN_REASON_CODE"
    REASON_CODE_NOT_APPLICABLE = "REASON_CODE_NOT_APPLICABLE"
    INVALID_CANDIDATE = "INVALID_CANDIDATE"
    OVERRIDE_EVIDENCE_REQUIRED = "OVERRIDE_EVIDENCE_REQUIRED"
    STALE_REVIEW = "STALE_REVIEW"
    REVIEW_NOT_FOUND = "REVIEW_NOT_FOUND"
    EPISODE_NOT_FOUND = "EPISODE_NOT_FOUND"
    NOTHING_PENDING = "NOTHING_PENDING"
    BATCH_PRECONDITION_FAILED = "BATCH_PRECONDITION_FAILED"
    UNSUPPORTED_LIBRARY_KIND = "UNSUPPORTED_LIBRARY_KIND"
    LIBRARY_ASSET_NOT_FOUND = "LIBRARY_ASSET_NOT_FOUND"
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    MEDIA_PATH_FORBIDDEN = "MEDIA_PATH_FORBIDDEN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


RECOVERY_VALUES = ("retry", "fix-then-retry", "do-X-first", "ask-human")
SIDE_EFFECT_VALUES = ("none", "partial", "complete", "unknown")

# CLI exit codes (§10.2). Everything not listed is a usage error (2).
_EXIT_CODES = {
    ErrorCode.NOTHING_PENDING: 3,
    ErrorCode.STALE_REVIEW: 4,
    ErrorCode.BUDGET_EXCEEDED: 5,
    ErrorCode.INTERNAL_ERROR: 1,
}

# Human-readable catalogue (used by doctor / API docs).
ERROR_CATALOG: dict[str, str] = {
    ErrorCode.VALIDATION_ERROR.value: "Request shape or value failed validation.",
    ErrorCode.INVALID_ACTION.value: "Action is not one of the six review actions.",
    ErrorCode.MISSING_REASON_CODE.value: "reject requires a reason_code.",
    ErrorCode.UNKNOWN_REASON_CODE.value: "reason_code is not in the canonical catalogue.",
    ErrorCode.REASON_CODE_NOT_APPLICABLE.value: "reason_code does not apply to this stage (REV-03).",
    ErrorCode.INVALID_CANDIDATE.value: "pick_variant requires a candidate from candidates[].",
    ErrorCode.OVERRIDE_EVIDENCE_REQUIRED.value: "Overriding UNKNOWN requires recorded evidence.",
    ErrorCode.STALE_REVIEW.value: "Decision targets a superseded candidate version.",
    ErrorCode.REVIEW_NOT_FOUND.value: "review_id does not exist.",
    ErrorCode.EPISODE_NOT_FOUND.value: "Episode manifest does not exist.",
    ErrorCode.NOTHING_PENDING.value: "No review items are pending.",
    ErrorCode.BATCH_PRECONDITION_FAILED.value: "Batch preconditions are not satisfied (REV-07).",
    ErrorCode.UNSUPPORTED_LIBRARY_KIND.value: "Library kind is not supported.",
    ErrorCode.LIBRARY_ASSET_NOT_FOUND.value: "Library asset does not exist.",
    ErrorCode.MEDIA_NOT_FOUND.value: "Media file does not exist.",
    ErrorCode.MEDIA_PATH_FORBIDDEN.value: "Media path escapes its allowed root.",
    ErrorCode.BUDGET_EXCEEDED.value: "Budget gate blocked the operation.",
    ErrorCode.INTERNAL_ERROR.value: "Unexpected server error.",
}


class ApiError(Exception):
    """Rejection that serializes to the shared §10.3 envelope."""

    def __init__(
        self,
        code: ErrorCode | str,
        what: str,
        fix: str = "",
        *,
        recovery: str = "fix-then-retry",
        side_effects: str = "none",
        context: Optional[dict[str, Any]] = None,
        http_status: int = 400,
        trace: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> None:
        self.code = ErrorCode(code)
        self.what = what
        self.fix = fix
        if recovery not in RECOVERY_VALUES:
            raise ValueError(f"invalid recovery '{recovery}'")
        if side_effects not in SIDE_EFFECT_VALUES:
            raise ValueError(f"invalid side_effects '{side_effects}'")
        self.recovery = recovery
        self.side_effects = side_effects
        self.context = context or {}
        self.http_status = http_status
        self.trace = trace
        self.exit_code = exit_code if exit_code is not None else _EXIT_CODES.get(self.code, 2)
        super().__init__(f"{self.code.value}: {what}")

    def to_envelope(self, *, include_trace: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code.value,
            "what": self.what,
            "fix": self.fix,
            "recovery": self.recovery,
            "side_effects": self.side_effects,
            "context": self.context,
        }
        if include_trace and self.trace:
            body["trace"] = self.trace
        return body


def classify_review_rejection(decision: Any, item: dict[str, Any]) -> tuple[ErrorCode, str, str]:
    """Map a rejected ReviewDecision to (code, what, fix).

    Mirrors the exact checks in ``lib.review.ReviewStore.decide`` using its own
    constants, so the code is derived from the authority's rules rather than
    from parsing exception text.
    """
    action = getattr(decision, "action", "")
    stage = item.get("stage", "")

    if action not in ACTIONS:
        return (
            ErrorCode.INVALID_ACTION,
            f"action '{action}' is not a valid review action",
            f"use one of: {', '.join(sorted(ACTIONS))}",
        )
    if action == "reject":
        reason = getattr(decision, "reason_code", None)
        if not reason:
            return (
                ErrorCode.MISSING_REASON_CODE,
                "reject was submitted without a reason_code",
                "retry with --reason CODE (API: reason_code)",
            )
        if reason not in REASON_CODES:
            return (
                ErrorCode.UNKNOWN_REASON_CODE,
                f"reason_code '{reason}' is not in the canonical catalogue",
                "use a code from the published reason-code catalogue",
            )
        allowed = STAGE_REASON_CODES.get(stage, REASON_CODES)
        if reason not in allowed:
            return (
                ErrorCode.REASON_CODE_NOT_APPLICABLE,
                f"reason_code '{reason}' is not applicable to stage '{stage}'",
                f"use one of: {', '.join(sorted(allowed))}",
            )
    if action == "pick_variant":
        chosen = getattr(decision, "chosen_candidate", None)
        candidates = item.get("candidates", [])
        if not chosen or chosen not in candidates:
            return (
                ErrorCode.INVALID_CANDIDATE,
                f"chosen_candidate '{chosen}' is not among candidates",
                f"choose one of: {', '.join(candidates) or '(none)'}",
            )
    if getattr(decision, "override", False) and not getattr(decision, "evidence", ""):
        return (
            ErrorCode.OVERRIDE_EVIDENCE_REQUIRED,
            "override=true was submitted without evidence",
            "record who/when/what was visibly verified, then retry",
        )
    return (
        ErrorCode.VALIDATION_ERROR,
        f"decision '{action}' rejected for stage '{stage}'",
        "inspect the review item and retry with a valid decision",
    )