"""DEL-04 content compliance screening (copyright / rating / sensitive / AI).

Overseas short-video platforms gate content on four families of rules. This
module keeps the machine pre-screen and the human confirmation strictly
separate:

- the **machine screen** only judges facts the caller supplied (asset licenses,
  declared rating, a completed sensitive-content scan, AI disclosure text);
- the **human confirmation** records who confirmed the item, when, and on what
  evidence.

A missing fact is ``UNKNOWN`` (needs evidence), never ``PASS``; a human
``rejected`` item or a machine ``FAIL`` blocks the episode. Only machine PASS
on every item plus human confirmation of every item yields ``PASS``.

M0: local validation only. No moderation API, platform API or model call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from lib.delivery import FAIL, PASS, UNKNOWN

HUMAN_UNCONFIRMED = "unconfirmed"
HUMAN_CONFIRMED = "confirmed"
HUMAN_REJECTED = "rejected"
HUMAN_STATUSES = (HUMAN_CONFIRMED, HUMAN_REJECTED)

#: Machine pre-screen items (DEL-04). Human confirmation is tracked per item.
COMPLIANCE_ITEMS: tuple[str, ...] = (
    "copyright",
    "rating",
    "sensitive_content",
    "ai_disclosure",
)

ALLOWED_LICENSES = frozenset(
    {"own", "licensed", "public_domain", "cc0", "royalty_free", "commissioned"}
)
PROHIBITED_LICENSES = frozenset({"unlicensed", "no_rights", "all_rights_reserved_third_party"})
VALID_RATINGS = frozenset({"general", "teen", "mature"})
#: Ratings this pipeline may clear mechanically; anything else needs policy
#: evidence and a human (DEL-09 content_policy covers the rule source).
MACHINE_CLEARABLE_RATINGS = frozenset({"general", "teen"})

#: Overall verdicts.
BLOCKED = "BLOCKED"
NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
PENDING_HUMAN = "PENDING_HUMAN"


class ComplianceError(ValueError):
    """Raised for malformed compliance input (not for a blocked verdict)."""


@dataclass(frozen=True)
class ItemReview:
    name: str
    machine_status: str
    machine_detail: str
    human_status: str = HUMAN_UNCONFIRMED
    human_reviewer: str = ""
    human_reviewed_at: str = ""
    human_evidence: str = ""

    @property
    def confirmed(self) -> bool:
        return self.human_status == HUMAN_CONFIRMED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "machine": {"status": self.machine_status, "detail": self.machine_detail},
            "human": {
                "status": self.human_status,
                "reviewer": self.human_reviewer,
                "reviewed_at": self.human_reviewed_at,
                "evidence": self.human_evidence,
            },
            "confirmed": self.confirmed,
        }


@dataclass
class ComplianceReport:
    status: str
    items: dict[str, ItemReview] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    pending_human: list[str] = field(default_factory=list)

    @property
    def publish_ready(self) -> bool:
        """Only an all-machine-PASS, all-human-confirmed report is ready."""
        return self.status == PASS

    def item(self, name: str) -> ItemReview:
        try:
            return self.items[name]
        except KeyError as exc:
            raise ComplianceError(f"unknown compliance item: {name!r}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "publish_ready": self.publish_ready,
            "blockers": list(self.blockers),
            "unknowns": list(self.unknowns),
            "pending_human": list(self.pending_human),
            "items": [self.items[name].to_dict() for name in COMPLIANCE_ITEMS],
        }


# --- machine pre-screen ------------------------------------------------------


def _screen_copyright(document: Mapping[str, Any]) -> tuple[str, str]:
    assets = document.get("assets")
    if not isinstance(assets, list) or not assets:
        return UNKNOWN, "no asset inventory supplied"
    missing: list[str] = []
    prohibited: list[str] = []
    for index, asset in enumerate(assets):
        if not isinstance(asset, Mapping):
            return UNKNOWN, f"asset #{index} is not a mapping"
        asset_id = str(asset.get("id") or f"#{index}")
        license_name = str(asset.get("license") or "").strip().lower()
        rights_source = str(asset.get("rights_source") or "").strip()
        if license_name in PROHIBITED_LICENSES:
            prohibited.append(asset_id)
            continue
        if license_name not in ALLOWED_LICENSES or not rights_source:
            missing.append(asset_id)
    if prohibited:
        return FAIL, f"prohibited/unlicensed assets: {prohibited}"
    if missing:
        return UNKNOWN, f"assets missing license/rights evidence: {missing}"
    return PASS, f"{len(assets)} asset(s) carry license + rights source"


def _screen_rating(document: Mapping[str, Any]) -> tuple[str, str]:
    rating = document.get("rating")
    if rating is None or (isinstance(rating, str) and not rating.strip()):
        return UNKNOWN, "rating not declared"
    value = str(rating).strip().lower()
    if value not in VALID_RATINGS:
        return UNKNOWN, f"unrecognized rating {rating!r}"
    if value not in MACHINE_CLEARABLE_RATINGS:
        return UNKNOWN, f"rating {value!r} needs platform policy evidence and a human"
    return PASS, f"declared rating {value!r}"


def _screen_sensitive(document: Mapping[str, Any]) -> tuple[str, str]:
    scan = document.get("sensitive_scan")
    if not isinstance(scan, Mapping):
        return UNKNOWN, "no sensitive-content scan supplied"
    if scan.get("scanned") is not True:
        return UNKNOWN, "sensitive-content scan not marked complete"
    flags = scan.get("flags")
    if not isinstance(flags, list):
        return UNKNOWN, "scan flags must be a list"
    hits: list[str] = []
    for flag in flags:
        if not isinstance(flag, Mapping):
            return UNKNOWN, "scan flag is not a mapping"
        if flag.get("match") is True:
            hits.append(str(flag.get("category") or "uncategorized"))
    if hits:
        return FAIL, f"sensitive-content hits: {hits}"
    return PASS, f"scan complete, {len(flags)} category flag(s), no hits"


def _screen_ai_disclosure(document: Mapping[str, Any]) -> tuple[str, str]:
    disclosure = document.get("ai_disclosure")
    if not isinstance(disclosure, Mapping):
        return UNKNOWN, "no AI disclosure record supplied"
    statement = str(disclosure.get("statement") or "").strip()
    if not statement:
        return UNKNOWN, "AI disclosure statement missing"
    if disclosure.get("required", True) is True:
        setting = str(disclosure.get("platform_setting") or "").strip().lower()
        if setting != "enabled":
            return UNKNOWN, "platform AI-disclosure setting not confirmed as enabled"
        return PASS, "disclosure statement present and platform label enabled"
    return PASS, "disclosure statement present (platform label not required)"


_MACHINE_SCREENS = {
    "copyright": _screen_copyright,
    "rating": _screen_rating,
    "sensitive_content": _screen_sensitive,
    "ai_disclosure": _screen_ai_disclosure,
}


# --- human confirmation ------------------------------------------------------


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def confirm_item(
    confirmations: dict[str, dict[str, Any]],
    item: str,
    *,
    reviewer: str,
    reviewed_at: str,
    evidence: str,
    status: str = HUMAN_CONFIRMED,
) -> dict[str, Any]:
    """Record one human confirmation; reviewer, timestamp and evidence required.

    A confirmation without evidence is not a confirmation -- this raises rather
    than letting an empty record masquerade as human approval.
    """
    if item not in COMPLIANCE_ITEMS:
        raise ComplianceError(f"unknown compliance item: {item!r}")
    if status not in HUMAN_STATUSES:
        raise ComplianceError(f"human status must be one of {list(HUMAN_STATUSES)}")
    if not str(reviewer).strip():
        raise ComplianceError("reviewer is required")
    if _parse_timestamp(reviewed_at) is None:
        raise ComplianceError("reviewed_at must be an ISO-8601 timestamp")
    if not str(evidence).strip():
        raise ComplianceError("human evidence is required")
    record = {
        "status": status,
        "reviewer": str(reviewer).strip(),
        "reviewed_at": str(reviewed_at).strip(),
        "evidence": str(evidence).strip(),
    }
    confirmations[item] = record
    return record


def _human_entry(confirmations: Mapping[str, Any], item: str) -> tuple[str, dict[str, Any], list[str]]:
    entry = confirmations.get(item)
    if entry is None:
        return HUMAN_UNCONFIRMED, {}, []
    if not isinstance(entry, Mapping):
        return HUMAN_UNCONFIRMED, {}, [f"{item}: human confirmation is not a mapping"]
    status = str(entry.get("status") or HUMAN_UNCONFIRMED).strip().lower()
    if status not in HUMAN_STATUSES:
        return HUMAN_UNCONFIRMED, dict(entry), [f"{item}: unknown human status {status!r}"]
    problems: list[str] = []
    if status == HUMAN_CONFIRMED:
        if not str(entry.get("reviewer") or "").strip():
            problems.append(f"{item}: human confirmation missing reviewer")
        if _parse_timestamp(entry.get("reviewed_at")) is None:
            problems.append(f"{item}: human confirmation missing/invalid reviewed_at")
        if not str(entry.get("evidence") or "").strip():
            problems.append(f"{item}: human confirmation missing evidence")
    return status, dict(entry), problems


# --- screen ------------------------------------------------------------------


def screen_compliance(
    document: Mapping[str, Any],
    human: Optional[Mapping[str, Any]] = None,
) -> ComplianceReport:
    """Run the DEL-04 machine screen and fold in supplied human confirmations.

    ``document`` keys: ``assets`` (list of ``{id, license, rights_source}``),
    ``rating``, ``sensitive_scan`` (``{scanned, flags}``) and ``ai_disclosure``
    (``{statement, platform_setting}``). ``human`` maps item -> confirmation
    record as produced by :func:`confirm_item`.
    """
    if not isinstance(document, Mapping):
        raise ComplianceError("compliance document must be a mapping")
    confirmations = human or {}
    if not isinstance(confirmations, Mapping):
        raise ComplianceError("human confirmations must be a mapping")

    items: dict[str, ItemReview] = {}
    blockers: list[str] = []
    unknowns: list[str] = []
    pending: list[str] = []
    malformed: list[str] = []

    for name in COMPLIANCE_ITEMS:
        machine_status, detail = _MACHINE_SCREENS[name](document)
        human_status, entry, human_problems = _human_entry(confirmations, name)
        malformed.extend(human_problems)

        items[name] = ItemReview(
            name=name,
            machine_status=machine_status,
            machine_detail=detail,
            human_status=human_status,
            human_reviewer=str(entry.get("reviewer") or ""),
            human_reviewed_at=str(entry.get("reviewed_at") or ""),
            human_evidence=str(entry.get("evidence") or ""),
        )

        if machine_status == FAIL:
            blockers.append(f"{name}: {detail}")
        elif machine_status == UNKNOWN:
            unknowns.append(f"{name}: {detail}")
        if human_status == HUMAN_REJECTED:
            blockers.append(f"{name}: rejected by human reviewer")
        elif human_status == HUMAN_UNCONFIRMED:
            pending.append(name)

    blockers.extend(malformed)

    if blockers:
        status = BLOCKED
    elif unknowns:
        status = NEEDS_EVIDENCE
    elif pending:
        status = PENDING_HUMAN
    else:
        status = PASS

    return ComplianceReport(
        status=status,
        items=items,
        blockers=blockers,
        unknowns=unknowns,
        pending_human=pending,
    )


def require_publish_ready(report: ComplianceReport) -> ComplianceReport:
    """Raise :class:`ComplianceError` unless the report clears DEL-04."""
    if not report.publish_ready:
        raise ComplianceError(
            f"compliance not ready ({report.status}): "
            + "; ".join(report.blockers or report.unknowns or report.pending_human)
        )
    return report


__all__ = [
    "ALLOWED_LICENSES",
    "BLOCKED",
    "COMPLIANCE_ITEMS",
    "ComplianceError",
    "ComplianceReport",
    "HUMAN_CONFIRMED",
    "HUMAN_REJECTED",
    "HUMAN_UNCONFIRMED",
    "ItemReview",
    "NEEDS_EVIDENCE",
    "PENDING_HUMAN",
    "confirm_item",
    "require_publish_ready",
    "screen_compliance",
]