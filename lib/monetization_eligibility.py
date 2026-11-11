"""MON-01 monetization eligibility evidence gate (creator fund + IAA ad share).

MON-01 tracks whether the account is **evidence-ready** for the two monetization
paths the project explicitly wants: the platform creator fund and IAA ad
revenue share (D13 / v2.2; IAP is out of scope). Every required item carries a
status (``UNKNOWN`` / ``UNMET`` / ``MET``) plus an evidence reference, a
reviewer and a review date.

Hard rule: if any required item is not ``MET`` -- including ``UNKNOWN``, and
including ``MET`` without an ``evidence_ref`` -- the whole report is
``BLOCKED``. Only a complete, fully cited checklist returns
``ELIGIBLE_READY``, and even that name deliberately stops short of "eligible":
it means the local evidence file is complete, not that a platform approved
anything.

This module never asserts external facts. Region availability and fan/view
thresholds are recorded as ``UNKNOWN`` in the template and are filled in by a
human with a cited source. No platform API is called and no eligibility is
verified here (M0: local, deterministic, zero external calls). The semantics
follow the DEL-09 pre-publish gate in :mod:`lib.publish_precheck`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

UNKNOWN = "UNKNOWN"
UNMET = "UNMET"
MET = "MET"

ITEM_STATUSES = (UNKNOWN, UNMET, MET)

ELIGIBLE_READY = "ELIGIBLE_READY"
BLOCKED = "BLOCKED"

#: The two monetization paths in scope per D13 / v2.2 (IAP is deferred and is
#: not modeled here).
PROGRAMS = ("creator_fund", "iaa_ad_share")

#: Ordered required evidence items. Descriptions state WHAT must be proven;
#: they intentionally contain no region names, thresholds or amounts -- those
#: are external facts a human records (as ``value``) with a citation.
REQUIRED_ITEMS: tuple[tuple[str, str], ...] = (
    (
        "region_eligibility",
        "Account region is eligible for the program (R16: operator region risk)",
    ),
    (
        "fan_or_view_threshold",
        "Fan/view threshold for the program is met (record the platform value, never guess)",
    ),
    (
        "account_ownership",
        "Account ownership and authorization are documented for the operating entity",
    ),
    (
        "payout_method",
        "A payout method supported by the platform is configured for this account",
    ),
    (
        "tax_information",
        "The tax information required for the account region is filed/accepted",
    ),
    (
        "program_enrollment",
        "The account has joined the creator fund / ad revenue share program",
    ),
    (
        "content_policy_compliance",
        "Account content meets the program's policy and originality requirements",
    ),
)

REQUIRED_ITEM_NAMES = tuple(name for name, _ in REQUIRED_ITEMS)
_REQUIRED_ITEM_SET = frozenset(REQUIRED_ITEM_NAMES)

DISCLAIMER = (
    "ELIGIBLE_READY means the local evidence checklist is complete. It is not a "
    "platform decision, does not verify any external fact, and is not a promise "
    "of approval or income."
)


class MonetizationEligibilityError(ValueError):
    """The evidence document or its configuration is malformed."""


class EligibilityBlocked(RuntimeError):
    """Raised when the MON-01 gate is BLOCKED and the caller requires a pass."""

    def __init__(self, report: "EligibilityReport") -> None:
        super().__init__("; ".join(report.reasons) or "monetization eligibility blocked")
        self.report = report


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Mixed naive/aware input must still compare; treat naive timestamps as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _blank_item() -> dict[str, Any]:
    """One empty evidence item. External facts stay UNKNOWN until a human fills them."""
    return {
        "status": UNKNOWN,
        "evidence_ref": "",
        "reviewer": "",
        "reviewed_at": "",
        "value": UNKNOWN,
        "notes": "",
    }


def build_evidence_template() -> dict[str, dict[str, Any]]:
    """Return a fill-in template with every required item present as UNKNOWN.

    The template is a plain mapping of item name -> record, ready for a human
    reviewer to fill ``status``/``evidence_ref``/``reviewer``/``reviewed_at``
    and, where the item is a threshold or region, ``value`` with a cited
    source. Nothing here guesses a region, threshold or amount.
    """
    return {name: _blank_item() for name in REQUIRED_ITEM_NAMES}


def _check_item(name: str, entry: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalize one required item and return its gaps.

    A gap is any condition that prevents this item from counting as verified
    evidence: missing record, non-MET status, missing evidence ref, missing
    reviewer, or an invalid review date.
    """
    if not isinstance(entry, Mapping):
        item = _blank_item()
        return item, [f"{name}: missing evidence record (treated as {UNKNOWN})"]

    item = _blank_item()
    raw_status = entry.get("status", UNKNOWN)
    status = raw_status.strip().upper() if isinstance(raw_status, str) else ""
    gaps: list[str] = []
    if status not in ITEM_STATUSES:
        gaps.append(f"{name}: invalid status {raw_status!r} (treated as {UNKNOWN})")
        status = UNKNOWN
    item["status"] = status

    for key in ("evidence_ref", "reviewer", "reviewed_at"):
        item[key] = _text(entry.get(key))

    value = entry.get("value", UNKNOWN)
    if value is None or (isinstance(value, str) and not value.strip()):
        value = UNKNOWN
    item["value"] = value
    item["notes"] = _text(entry.get("notes"))

    if status != MET:
        gaps.append(f"{name}: status is {status}, must be {MET} with evidence")
        return item, gaps

    if not item["evidence_ref"]:
        gaps.append(f"{name}: {MET} but no evidence_ref")
    if not item["reviewer"]:
        gaps.append(f"{name}: {MET} but no reviewer")
    if _parse_timestamp(item["reviewed_at"]) is None:
        gaps.append(f"{name}: {MET} but reviewed_at is missing or not ISO-8601")
    return item, gaps


@dataclass
class EligibilityReport:
    """Deterministic MON-01 report: per-item status, gaps and blocked reasons."""

    status: str
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    gap_items: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    platform: str = UNKNOWN
    programs: tuple[str, ...] = PROGRAMS

    @property
    def ok(self) -> bool:
        return self.status == ELIGIBLE_READY

    @property
    def eligible_ready(self) -> bool:
        return self.ok

    def query(self, item: Optional[str] = None) -> dict[str, Any]:
        """Return the full report, or one item's status record."""
        if item is None:
            return self.to_dict()
        if item not in self.items:
            raise KeyError(f"unknown monetization item {item!r}")
        return dict(self.items[item])

    def items_with_status(self, status: str) -> list[str]:
        wanted = status.strip().upper()
        return [name for name in REQUIRED_ITEM_NAMES if self.items[name]["status"] == wanted]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "platform": self.platform,
            "programs": list(self.programs),
            "required_items": list(REQUIRED_ITEM_NAMES),
            "items": {name: dict(record) for name, record in self.items.items()},
            "gap_items": list(self.gap_items),
            "gaps": list(self.gaps),
            "reasons": list(self.reasons),
            "disclaimer": DISCLAIMER,
        }


def evaluate_eligibility(
    evidence: Mapping[str, Any],
    *,
    platform: str = UNKNOWN,
    programs: Iterable[str] = PROGRAMS,
    require_ready: bool = False,
) -> EligibilityReport:
    """Evaluate the MON-01 monetization eligibility evidence.

    Every required item must supply ``{"status": "MET", "evidence_ref": ...,
    "reviewer": ..., "reviewed_at": ...}``. Any other state -- including
    ``UNKNOWN`` and ``MET`` without an evidence reference -- makes the overall
    status ``BLOCKED``. External facts (region, thresholds) are never invented;
    an absent ``value`` is reported as ``UNKNOWN``.
    """
    if not isinstance(evidence, Mapping):
        raise MonetizationEligibilityError(
            "evidence must be a mapping of required item names to records"
        )

    program_list = tuple(programs)
    invalid = [program for program in program_list if program not in PROGRAMS]
    if invalid:
        raise MonetizationEligibilityError(
            f"unknown program(s) {invalid}; allowed: {list(PROGRAMS)}"
        )

    items: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for name in REQUIRED_ITEM_NAMES:
        item, item_gaps = _check_item(name, evidence.get(name))
        items[name] = item
        gaps.extend(item_gaps)

    gap_items = [name for name in REQUIRED_ITEM_NAMES if any(g.startswith(f"{name}:") for g in gaps)]

    reasons: list[str] = []
    if gaps:
        not_met = [name for name in REQUIRED_ITEM_NAMES if items[name]["status"] != MET]
        uncited = [
            name
            for name in REQUIRED_ITEM_NAMES
            if items[name]["status"] == MET and not items[name]["evidence_ref"]
        ]
        reasons.append(
            "cannot declare monetization eligibility: "
            f"{len(gap_items)} of {len(REQUIRED_ITEM_NAMES)} required items are not "
            f"MET with complete evidence ({len(gaps)} gap(s))"
        )
        if not_met:
            reasons.append("not MET: " + ", ".join(not_met))
        if uncited:
            reasons.append("MET without evidence_ref: " + ", ".join(uncited))

    report = EligibilityReport(
        status=BLOCKED if gaps else ELIGIBLE_READY,
        items=items,
        gaps=gaps,
        gap_items=gap_items,
        reasons=reasons,
        platform=_text(platform) or UNKNOWN,
        programs=program_list,
    )
    if require_ready and not report.ok:
        raise EligibilityBlocked(report)
    return report