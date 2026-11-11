"""CHAR-10 M0: identity-comparison candidate registry and its validator.

The requirement (CHAR-10, P1 per D14) splits cleanly:

- **M0 (this module)** declares at least two license-clean CPU face-identity
  candidates, recording the *model* license and the *library* license
  independently, and declares the fallback when no tool is usable.
- **M1** measures accuracy, bias, authorisation and device compatibility and
  fills calibrated thresholds.

M0 is a declaration layer only: zero external API calls, zero model downloads.
It therefore cannot verify anything, so the validator refuses every status
other than ``UNVERIFIED``/``BLOCKED`` and refuses ``GREEN`` outright. The
status vocabulary is the capability-catalog one
(``schemas/artifacts/capability_catalog.schema.json``), which keeps this file
semantically consistent with MOD-02: unknown is never support.

The load-bearing rule is **model license != library license**. A candidate whose
library is MIT but whose weights are non-commercial is not license-clean; a
candidate with an ``UNKNOWN`` license can never be declared clean. When nothing
is clean, usable *and* measured, the fallback is detection-only plus a human
identity decision, with identity ``UNKNOWN`` (CHAR-06 semantics).

No files outside this module and its data file are touched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES_PATH = _REPO_ROOT / "data" / "identity" / "char10_candidates.yaml"

UNKNOWN = "UNKNOWN"

#: Full capability-catalog vocabulary, in the order the schema declares it.
VERIFICATION_STATUSES = ("VERIFIED", "MOCKED", "UNVERIFIED", "BLOCKED")
#: M0 measured nothing, so only these two are legal here.
M0_ALLOWED_STATUSES = ("UNVERIFIED", "BLOCKED")
#: Explicitly banned words: "GREEN" is a doctor verdict, not a catalog status.
FORBIDDEN_STATUSES = ("GREEN", "PASS", "OK")

#: ``license_clean_for_commercial`` is tri-state: a license we cannot read is
#: not a license we can rely on.
CLEAN_VALUES = (True, False, UNKNOWN)


class IdentityCandidatesError(Exception):
    """Base error for the CHAR-10 candidate registry."""


class InvalidCandidatesError(IdentityCandidatesError):
    """The candidates document violates the M0 declaration contract."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_candidates(path: Optional[Path] = None) -> dict:
    """Load the YAML document from ``path`` (default: repo data file)."""
    resolved = Path(path) if path is not None else DEFAULT_CANDIDATES_PATH
    if not resolved.is_file():
        raise IdentityCandidatesError(f"candidate file not found: {resolved}")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InvalidCandidatesError(f"{resolved} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidCandidatesError(f"{resolved} must contain a mapping")
    return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidCandidatesError(f"{where} must be an object")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCandidatesError(f"{where} must be a non-empty string")
    return value.strip()


def _validate_semantics(data: dict) -> None:
    semantics = _mapping(data.get("verification_semantics"), "verification_semantics")
    if semantics.get("green_forbidden") is not True:
        raise InvalidCandidatesError(
            "verification_semantics.green_forbidden must be true: M0 measured nothing")
    declared = semantics.get("m0_allowed")
    if not isinstance(declared, list) or not declared:
        raise InvalidCandidatesError(
            "verification_semantics.m0_allowed must be a non-empty list")
    illegal = [status for status in declared if status not in M0_ALLOWED_STATUSES]
    if illegal:
        raise InvalidCandidatesError(
            f"verification_semantics.m0_allowed contains non-M0 statuses: {illegal}")


def _validate_model_and_library(candidate: dict, where: str) -> tuple[str, str]:
    model = _mapping(candidate.get("model"), f"{where}.model")
    library = _mapping(candidate.get("library"), f"{where}.library")
    model_license = _text(model.get("license"), f"{where}.model.license")
    library_license = _text(library.get("license"), f"{where}.library.license")
    _text(model.get("license_source"), f"{where}.model.license_source")
    _text(library.get("license_source"), f"{where}.library.license_source")
    return model_license, library_license


def _validate_candidate(candidate: Any, index: int) -> dict:
    where = f"candidates[{index}]"
    _mapping(candidate, where)
    _text(candidate.get("id"), f"{where}.id")
    _text(candidate.get("name"), f"{where}.name")

    model_license, library_license = _validate_model_and_library(candidate, where)

    cpu = _mapping(candidate.get("cpu"), f"{where}.cpu")
    available = cpu.get("available")
    if available not in (True, False, UNKNOWN):
        raise InvalidCandidatesError(
            f"{where}.cpu.available must be true, false or {UNKNOWN!r}")
    if available is False:
        raise InvalidCandidatesError(
            f"{where} is not CPU-capable; CHAR-10 M0 only enumerates CPU candidates")

    _text(candidate.get("input"), f"{where}.input")
    _text(candidate.get("output"), f"{where}.output")

    status = candidate.get("verification_status")
    if status in FORBIDDEN_STATUSES:
        raise InvalidCandidatesError(
            f"{where}.verification_status={status!r} is forbidden in M0")
    if status not in VERIFICATION_STATUSES:
        raise InvalidCandidatesError(
            f"{where}.verification_status must be one of {VERIFICATION_STATUSES}")
    if status not in M0_ALLOWED_STATUSES:
        raise InvalidCandidatesError(
            f"{where}.verification_status={status!r} is not allowed in M0; "
            f"allowed: {M0_ALLOWED_STATUSES} (nothing was measured)")

    clean = candidate.get("license_clean_for_commercial")
    if clean not in CLEAN_VALUES:
        raise InvalidCandidatesError(
            f"{where}.license_clean_for_commercial must be true, false or {UNKNOWN!r}")
    if clean is True and UNKNOWN in (model_license, library_license):
        raise InvalidCandidatesError(
            f"{where} declares an UNKNOWN license and cannot be license-clean")
    if clean is False:
        if status != "BLOCKED":
            raise InvalidCandidatesError(
                f"{where} is not license-clean; verification_status must be BLOCKED")
        _text(candidate.get("blocked_reason"), f"{where}.blocked_reason")
    if clean is UNKNOWN and status == "BLOCKED":
        _text(candidate.get("blocked_reason"), f"{where}.blocked_reason")
    return candidate


def _validate_fallback(data: dict) -> dict:
    fallback = _mapping(data.get("fallback"), "fallback")
    _text(fallback.get("id"), "fallback.id")
    _text(fallback.get("applies_when"), "fallback.applies_when")

    detection = _mapping(fallback.get("detection"), "fallback.detection")
    _text(detection.get("tool"), "fallback.detection.tool")
    role = _text(detection.get("role"), "fallback.detection.role")
    if "detect" not in role.lower():
        raise InvalidCandidatesError(
            "fallback.detection.role must state that detection is the only machine step")

    decision = _text(fallback.get("identity_decision"), "fallback.identity_decision")
    if decision.upper() != "MANUAL":
        raise InvalidCandidatesError(
            f"fallback.identity_decision must be MANUAL, got {decision!r}")
    if fallback.get("identity_status") != UNKNOWN:
        raise InvalidCandidatesError(
            f"fallback.identity_status must be {UNKNOWN} (CHAR-06), "
            f"got {fallback.get('identity_status')!r}")
    _text(fallback.get("semantics"), "fallback.semantics")
    return fallback


def validate_candidates(data: dict) -> dict:
    """Validate the M0 declaration contract; return ``data`` unchanged.

    Raises :class:`InvalidCandidatesError` on the first violated rule. The
    document is the schema (no schema file is added by this work package).
    """
    data = _mapping(data, "candidates document")
    _text(data.get("version"), "version")
    if data.get("requirement") != "CHAR-10":
        raise InvalidCandidatesError("requirement must be 'CHAR-10'")
    if data.get("scope") != "M0":
        raise InvalidCandidatesError("scope must be 'M0'")
    _validate_semantics(data)

    entries = data.get("candidates")
    if not isinstance(entries, list) or not entries:
        raise InvalidCandidatesError("candidates must be a non-empty list")
    seen: set[str] = set()
    validated: list[dict] = []
    for index, candidate in enumerate(entries):
        validated.append(_validate_candidate(candidate, index))
        cid = str(candidate["id"])
        if cid in seen:
            raise InvalidCandidatesError(f"duplicate candidate id {cid!r}")
        seen.add(cid)

    _validate_fallback(data)

    clean = [row for row in validated if row.get("license_clean_for_commercial") is True]
    if len(clean) < 2:
        raise InvalidCandidatesError(
            f"CHAR-10 M0 needs >= 2 license-clean CPU candidates, found {len(clean)}")
    return data


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def candidates(data: dict) -> list[dict]:
    """Return the candidate list after validation."""
    return list(validate_candidates(data)["candidates"])


def license_clean_candidates(data: dict) -> list[dict]:
    """Candidates whose model *and* library licenses are declared clean."""
    return [row for row in candidates(data)
            if row.get("license_clean_for_commercial") is True]


def usable_candidates(data: dict) -> list[dict]:
    """Clean, CPU-available and not blocked.

    ``UNVERIFIED`` candidates are usable *for M1 measurement*, not for
    commercial delivery: they still need M1 accuracy/rights evidence.
    """
    return [row for row in license_clean_candidates(data)
            if row.get("verification_status") == "UNVERIFIED"
            and (row.get("cpu") or {}).get("available") is True]


def fallback(data: dict) -> dict:
    """Return the detection-only + human-identity fallback declaration."""
    validate_candidates(data)
    return dict(data["fallback"])


def summary(data: dict) -> dict:
    """Compact, machine-readable status for doctor/CLI output."""
    validate_candidates(data)
    rows = list(data["candidates"])
    return {
        "requirement": data["requirement"],
        "scope": data["scope"],
        "version": data["version"],
        "candidate_count": len(rows),
        "license_clean_count": len(license_clean_candidates(data)),
        "usable_count": len(usable_candidates(data)),
        "blocked": [
            {"id": row["id"], "reason": row.get("blocked_reason")}
            for row in rows if row.get("verification_status") == "BLOCKED"
        ],
        "unknown_license": [
            row["id"] for row in rows
            if row.get("license_clean_for_commercial") == UNKNOWN
        ],
        "fallback_id": data["fallback"]["id"],
        "identity_status": data["fallback"]["identity_status"],
        "green_forbidden": True,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the CHAR-10 M0 identity-candidate declaration.")
    parser.add_argument("--path", default=str(DEFAULT_CANDIDATES_PATH),
                        help="candidates YAML (default: repo data file)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    try:
        data = load_candidates(Path(args.path))
        report = summary(data)
    except IdentityCandidatesError as exc:
        print(f"INVALID: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"OK: {report['requirement']} {report['scope']} "
              f"({report['candidate_count']} candidates, "
              f"{report['license_clean_count']} license-clean, "
              f"{report['usable_count']} usable for M1)")
        print(f"fallback: {report['fallback_id']} -> "
              f"identity_status={report['identity_status']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via CLI only
    raise SystemExit(main())