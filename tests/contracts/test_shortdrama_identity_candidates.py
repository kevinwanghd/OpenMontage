"""WP-AD contract tests: CHAR-10 M0 identity candidates + fallback.

The task is a *declaration* task, so the tests assert the declaration's intent
rather than that a function runs:

- at least two license-clean CPU candidates exist, each recording BOTH the
  model license and the library license;
- M0 never claims anything was verified (no VERIFIED / MOCKED / GREEN);
- a permissive library does not launder a non-commercial model license
  (InsightFace is the regression fixture);
- UNKNOWN license facts never become "clean";
- the fallback is detection-only + a MANUAL identity decision with identity
  UNKNOWN, matching CHAR-06.

M0/fixture only: zero external API calls, no model downloads.
"""
from __future__ import annotations

import copy

import pytest

from lib.identity_candidates import (
    DEFAULT_CANDIDATES_PATH,
    M0_ALLOWED_STATUSES,
    UNKNOWN,
    IdentityCandidatesError,
    InvalidCandidatesError,
    candidates,
    fallback,
    license_clean_candidates,
    load_candidates,
    summary,
    usable_candidates,
    validate_candidates,
)


@pytest.fixture
def doc():
    return load_candidates(DEFAULT_CANDIDATES_PATH)


def _by_id(document, candidate_id):
    return next(row for row in document["candidates"] if row["id"] == candidate_id)


def _clone(document):
    return copy.deepcopy(document)


# ---------------------------------------------------------------------------
# The declared file
# ---------------------------------------------------------------------------


def test_default_file_is_a_versioned_char10_m0_document(doc):
    assert DEFAULT_CANDIDATES_PATH.is_file()
    assert doc["version"] == "1.0"
    assert doc["requirement"] == "CHAR-10"
    assert doc["scope"] == "M0"
    validate_candidates(doc)  # the document is its own schema


def test_at_least_two_license_clean_cpu_candidates(doc):
    clean = license_clean_candidates(doc)
    assert len(clean) >= 2, "CHAR-10 requires >= 2 license-clean CPU candidates"
    for row in clean:
        assert row["cpu"]["available"] is True
        assert row["input"].strip()
        assert row["output"].strip()


def test_every_candidate_records_model_and_library_license(doc):
    for row in candidates(doc):
        assert row["model"]["license"].strip()
        assert row["model"]["license_source"].strip()
        assert row["library"]["license"].strip()
        assert row["library"]["license_source"].strip()
        # The two licenses are declared independently, never collapsed.
        assert set(row["model"]) != set(row["library"])


def test_m0_never_claims_a_verified_or_green_result(doc):
    for row in candidates(doc):
        assert row["verification_status"] in M0_ALLOWED_STATUSES
        assert row["verification_status"] not in ("VERIFIED", "MOCKED", "GREEN")
    assert doc["verification_semantics"]["green_forbidden"] is True
    assert "VERIFIED" not in doc["verification_semantics"]["m0_allowed"]


def test_insightface_is_blocked_by_the_model_license_not_the_library(doc):
    row = _by_id(doc, "insightface_arcface_buffalo_l")
    assert "MIT" in row["library"]["license"]
    assert "non-commercial" in row["model"]["license"].lower()
    assert row["license_clean_for_commercial"] is False
    assert row["verification_status"] == "BLOCKED"
    assert row["blocked_reason"].strip()


def test_unknown_model_license_is_not_promoted_to_clean(doc):
    row = _by_id(doc, "opencv_sface")
    assert row["model"]["license"] == UNKNOWN
    assert row["license_clean_for_commercial"] == UNKNOWN
    assert row not in usable_candidates(doc)


def test_usable_candidates_are_clean_cpu_and_unverified(doc):
    usable = usable_candidates(doc)
    assert usable, "at least one clean candidate must be available for M1 measurement"
    for row in usable:
        assert row["license_clean_for_commercial"] is True
        assert row["verification_status"] == "UNVERIFIED"
        assert row["cpu"]["available"] is True


# ---------------------------------------------------------------------------
# Fallback: detection-only + human identity (CHAR-06)
# ---------------------------------------------------------------------------


def test_fallback_is_detection_only_plus_manual_identity(doc):
    declaration = fallback(doc)
    assert declaration["detection"]["tool"] == "tools/analysis/face_tracker.py"
    assert "detect" in declaration["detection"]["role"].lower()
    assert declaration["identity_decision"] == "MANUAL"
    assert declaration["identity_status"] == UNKNOWN
    assert declaration["review_route"] == "human_review"
    assert "UNKNOWN" in declaration["semantics"]


def test_fallback_identity_status_must_stay_unknown(doc):
    broken = _clone(doc)
    broken["fallback"]["identity_status"] = "MATCH"
    with pytest.raises(InvalidCandidatesError, match="identity_status"):
        validate_candidates(broken)


def test_fallback_identity_decision_must_be_manual(doc):
    broken = _clone(doc)
    broken["fallback"]["identity_decision"] = "AUTO"
    with pytest.raises(InvalidCandidatesError, match="MANUAL"):
        validate_candidates(broken)


# ---------------------------------------------------------------------------
# Validator is adversarial: bad declarations are rejected
# ---------------------------------------------------------------------------


def test_verified_status_is_rejected_in_m0(doc):
    broken = _clone(doc)
    _by_id(broken, "dlib_face_recognition_resnet")["verification_status"] = "VERIFIED"
    with pytest.raises(InvalidCandidatesError, match="M0"):
        validate_candidates(broken)


def test_green_status_is_rejected(doc):
    broken = _clone(doc)
    _by_id(broken, "dlib_face_recognition_resnet")["verification_status"] = "GREEN"
    with pytest.raises(InvalidCandidatesError, match="forbidden"):
        validate_candidates(broken)


def test_missing_library_license_is_rejected(doc):
    broken = _clone(doc)
    _by_id(broken, "dlib_face_recognition_resnet")["library"]["license"] = ""
    with pytest.raises(InvalidCandidatesError, match="library.license"):
        validate_candidates(broken)


def test_unknown_license_cannot_be_declared_clean(doc):
    broken = _clone(doc)
    sface = _by_id(broken, "opencv_sface")
    sface["license_clean_for_commercial"] = True
    with pytest.raises(InvalidCandidatesError, match="UNKNOWN license"):
        validate_candidates(broken)


def test_non_clean_candidate_must_be_blocked(doc):
    broken = _clone(doc)
    insight = _by_id(broken, "insightface_arcface_buffalo_l")
    insight["verification_status"] = "UNVERIFIED"
    with pytest.raises(InvalidCandidatesError, match="must be BLOCKED"):
        validate_candidates(broken)


def test_blocked_candidate_needs_a_reason(doc):
    broken = _clone(doc)
    _by_id(broken, "insightface_arcface_buffalo_l")["blocked_reason"] = ""
    with pytest.raises(InvalidCandidatesError, match="blocked_reason"):
        validate_candidates(broken)


def test_non_cpu_candidate_is_rejected(doc):
    broken = _clone(doc)
    _by_id(broken, "dlib_face_recognition_resnet")["cpu"]["available"] = False
    with pytest.raises(InvalidCandidatesError, match="CPU"):
        validate_candidates(broken)


def test_duplicate_candidate_ids_are_rejected(doc):
    broken = _clone(doc)
    broken["candidates"].append(copy.deepcopy(broken["candidates"][0]))
    with pytest.raises(InvalidCandidatesError, match="duplicate"):
        validate_candidates(broken)


def test_fewer_than_two_clean_candidates_is_rejected(doc):
    broken = _clone(doc)
    broken["candidates"] = [_by_id(broken, "dlib_face_recognition_resnet")]
    with pytest.raises(InvalidCandidatesError, match=">= 2"):
        validate_candidates(broken)


def test_missing_file_raises(tmp_path):
    with pytest.raises(IdentityCandidatesError, match="not found"):
        load_candidates(tmp_path / "missing.yaml")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summary_exposes_blocked_and_unknown_license(doc):
    report = summary(doc)
    assert report["requirement"] == "CHAR-10"
    assert report["candidate_count"] == len(doc["candidates"])
    assert report["license_clean_count"] >= 2
    assert report["green_forbidden"] is True
    assert report["fallback_id"] == "detection_only_plus_human_identity"
    assert report["identity_status"] == UNKNOWN
    assert "insightface_arcface_buffalo_l" in [row["id"] for row in report["blocked"]]
    assert "opencv_sface" in report["unknown_license"]