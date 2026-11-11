"""DEL-07a publish records and DEL-09 single-platform pre-publish gate.

DEL-07a: hand-entered per-episode publication records. Every record states its
metric source (platform backend vs blind test), and unavailable metrics are
stored as the explicit ``UNKNOWN`` sentinel -- never guessed or silently
omitted. Raw platform metrics and blind-test results live in separate buckets.

DEL-09: evidence-based pre-publish checklist for one target platform. Rights
source, account availability/region, content policy, AI disclosure and A/V
format integrity must be confirmed with evidence; membership-gateway terms and
output rights are an independent item when a paid gateway is used (F21).
Missing evidence or an unconfirmed rule returns BLOCKED.

M0: local data and validation only. No platform API is called.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from lib.delivery import DEGRADED, PASS, DeliveryReport

UNKNOWN = "UNKNOWN"

RECORD_KINDS = ("platform_raw", "blind_test")
RECORD_REQUIRED_FIELDS = (
    "account",
    "episode",
    "version",
    "published_at",
    "collected_at",
    "source",
    "metric_kind",
)
METRIC_FIELDS = ("views", "three_sec_retention", "completion_rate")

PRE_PUBLISH_ITEMS = (
    "rights_source",
    "account_availability_region",
    "content_policy",
    "ai_disclosure",
    "format_av_integrity",
)
GATEWAY_ITEM = "membership_gateway_terms"

#: Items whose "confirmed" status must cite the official rule source and the
#: date it was checked, per DEL-09 (人工记官方规则来源/日期).
RULE_ITEMS = ("account_availability_region", "content_policy", "ai_disclosure")


class PublishRecordError(ValueError):
    """A publish record or pre-publish evidence document is malformed."""


class PrePublishBlocked(RuntimeError):
    """Raised when the DEL-09 gate is BLOCKED and the caller requires a pass."""

    def __init__(self, report: "PrePublishReport") -> None:
        super().__init__("; ".join(report.blockers) or "pre-publish gate blocked")
        self.report = report


# --- DEL-07a ---------------------------------------------------------------


@dataclass(frozen=True)
class RecordValidation:
    ok: bool
    normalized: dict[str, Any]
    issues: tuple[str, ...] = ()

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise PublishRecordError("; ".join(self.issues))


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


def _metric_unknown(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().upper() == UNKNOWN) or value == ""


def _normalize_metric(name: str, value: Any, unit: str, issues: list[str]) -> Any:
    if _metric_unknown(value):
        return UNKNOWN
    if name == "views":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append(f"{name} must be a non-negative integer or {UNKNOWN}")
            return UNKNOWN
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(f"{name} must be numeric or {UNKNOWN}")
        return UNKNOWN
    number = float(value)
    if unit == "percent":
        if not 0 <= number <= 100:
            issues.append(f"{name} percent must be 0..100")
            return UNKNOWN
        return round(number / 100.0, 6)
    if not 0 <= number <= 1:
        issues.append(f"{name} ratio must be 0..1 (or set rate_unit=percent)")
        return UNKNOWN
    return number


def validate_publish_record(record: Mapping[str, Any]) -> RecordValidation:
    """Validate one DEL-07a record and normalize its metrics.

    Missing required fields and missing metric keys are reported as issues:
    an unavailable metric must be stored as ``UNKNOWN`` explicitly so readers
    can tell "not available" apart from "forgotten".
    """
    if not isinstance(record, Mapping):
        raise PublishRecordError("publish record must be a mapping")
    issues: list[str] = []
    normalized: dict[str, Any] = {}

    for field_name in RECORD_REQUIRED_FIELDS:
        value = record.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(f"{field_name} is required")
        else:
            normalized[field_name] = str(value).strip() if isinstance(value, str) else value

    kind = normalized.get("metric_kind")
    if kind is not None and kind not in RECORD_KINDS:
        issues.append(f"metric_kind must be one of {list(RECORD_KINDS)}")

    published = _parse_timestamp(record.get("published_at"))
    collected = _parse_timestamp(record.get("collected_at"))
    if record.get("published_at") and published is None:
        issues.append("published_at must be an ISO-8601 timestamp")
    if record.get("collected_at") and collected is None:
        issues.append("collected_at must be an ISO-8601 timestamp")
    if published and collected and collected < published:
        issues.append("collected_at must not precede published_at")

    unit = str(record.get("rate_unit", "ratio")).strip().lower()
    if unit not in ("ratio", "percent"):
        issues.append("rate_unit must be 'ratio' or 'percent'")
        unit = "ratio"

    for metric in METRIC_FIELDS:
        if metric not in record:
            issues.append(f"{metric} missing; store {UNKNOWN} explicitly when unavailable")
            normalized[metric] = UNKNOWN
        else:
            normalized[metric] = _normalize_metric(metric, record.get(metric), unit, issues)

    for optional in ("platform", "url", "notes", "sample_size"):
        if optional in record:
            normalized[optional] = record[optional]

    return RecordValidation(ok=not issues, normalized=normalized, issues=tuple(issues))


def validate_publish_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate a batch and keep raw-platform vs blind-test records separate."""
    buckets: dict[str, list[dict[str, Any]]] = {kind: [] for kind in RECORD_KINDS}
    issues: list[str] = []
    for index, record in enumerate(records):
        result = validate_publish_record(record)
        issues.extend(f"[{index}] {issue}" for issue in result.issues)
        kind = result.normalized.get("metric_kind")
        if kind in buckets:
            buckets[kind].append(result.normalized)
    return {"ok": not issues, "issues": issues, **buckets}


def save_publish_records(path: Path | str, records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Persist validated records with raw and blind-test metrics separated."""
    document = validate_publish_records(records)
    if not document["ok"]:
        raise PublishRecordError("; ".join(document["issues"]))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "platform_raw": document["platform_raw"],
        "blind_test": document["blind_test"],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def load_publish_records(path: Path | str) -> dict[str, Any]:
    """Load the separated DEL-07a buckets from a local JSON file."""
    target = Path(path)
    if not target.is_file():
        raise PublishRecordError(f"publish record file not found: {target}")
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise PublishRecordError("publish record file root must be an object")
    return {
        "platform_raw": list(document.get("platform_raw", [])),
        "blind_test": list(document.get("blind_test", [])),
    }


# --- DEL-09 ----------------------------------------------------------------


@dataclass
class PrePublishReport:
    status: str
    blockers: list[str] = field(default_factory=list)
    checked: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "blockers": list(self.blockers),
            "checked": dict(self.checked),
        }


def _check_item(name: str, evidence: Any) -> list[str]:
    """Return blockers for one checklist item."""
    if not isinstance(evidence, Mapping):
        return [f"{name}: missing evidence (no record supplied)"]
    status = str(evidence.get("status", "missing")).strip().lower()
    if status in ("missing", "", "none"):
        return [f"{name}: missing evidence"]
    if status == "unconfirmed":
        return [f"{name}: rule not confirmed"]
    if status != "confirmed":
        return [f"{name}: unknown status {status!r}"]

    blockers: list[str] = []
    if not str(evidence.get("evidence", "")).strip():
        blockers.append(f"{name}: confirmed but no evidence text")
    if name in RULE_ITEMS:
        if not str(evidence.get("source", "")).strip():
            blockers.append(f"{name}: official rule source missing")
        checked = evidence.get("checked_at")
        if _parse_timestamp(checked) is None:
            blockers.append(f"{name}: rule check date missing/invalid")
    if name == "account_availability_region" and not str(evidence.get("region", "")).strip():
        blockers.append(f"{name}: account region missing")
    return blockers


def format_av_evidence(delivery_report: DeliveryReport | Mapping[str, Any]) -> dict[str, Any]:
    """Build the DEL-09 A/V integrity evidence item from a DEL-01 report."""
    document = (
        delivery_report.to_dict()
        if isinstance(delivery_report, DeliveryReport)
        else dict(delivery_report)
    )
    status = document.get("status")
    if status in (PASS, DEGRADED):
        suffix = " (DEGRADED upscale disclosed)" if status == DEGRADED else ""
        return {
            "status": "confirmed",
            "evidence": f"ffprobe metadata checks {status}{suffix}",
        }
    return {
        "status": "missing",
        "evidence": f"ffprobe metadata checks {status}",
    }


def evaluate_pre_publish(
    evidence: Mapping[str, Any],
    *,
    membership_gateway: bool = False,
    require_pass: bool = False,
) -> PrePublishReport:
    """Evaluate the DEL-09 single-platform pre-publish checklist.

    Every item must supply ``{"status": "confirmed", "evidence": ...}``.
    Rule items also need ``source`` and ``checked_at``; the account item needs
    ``region``. Any missing evidence or unconfirmed rule yields BLOCKED.
    """
    if not isinstance(evidence, Mapping):
        raise PublishRecordError("evidence must be a mapping of checklist items")

    items = list(PRE_PUBLISH_ITEMS)
    if membership_gateway:
        items.append(GATEWAY_ITEM)

    blockers: list[str] = []
    checked: dict[str, str] = {}
    for item in items:
        entry = evidence.get(item)
        blockers.extend(_check_item(item, entry))
        checked[item] = (
            str(entry.get("status", "missing")).strip().lower()
            if isinstance(entry, Mapping)
            else "missing"
        )

    report = PrePublishReport(
        status=PASS if not blockers else "BLOCKED",
        blockers=blockers,
        checked=checked,
    )
    if require_pass and not report.ok:
        raise PrePublishBlocked(report)
    return report