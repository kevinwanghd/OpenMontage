"""WP-4 contract tests: CHAR-10 / CHAR-07 offline identity bench.

The bench is a *measurement scaffold*, so the tests assert the measurement
contract, not a model:

- a deterministic fake embedder yields a threshold that separates same-person
  from different-person pairs, and the threshold is calibrated per candidate
  (CHAR-07), not a global constant;
- the report carries every required field and honest provenance;
- an unmeasured run can only be UNKNOWN, a fake-embedder run only MOCKED, and
  GREEN/PASS are refused everywhere;
- no usable tool / no embedder falls back to detection-only + human identity,
  status UNKNOWN, with no fabricated accuracy;
- the run is offline: it completes while outbound sockets are blocked.

M0/fixture only: zero external API calls, no model downloads.
"""
from __future__ import annotations

import copy
import json
import math
import socket
from datetime import datetime

import pytest

from lib.identity_bench import (
    DEFAULT_FIXTURE_PATH,
    FORBIDDEN_STATUSES,
    REQUIRED_REPORT_FIELDS,
    CalibrationError,
    IdentityBenchError,
    InvalidFixtureError,
    build_report,
    calibrate_threshold,
    cosine_similarity,
    fixture_embedder,
    is_same,
    load_fixture,
    main,
    pair_similarity,
    score_pairs,
    pairs_from_fixture,
    threshold_candidates,
    validate_fixture,
    validate_report,
)

DLIB = "dlib_face_recognition_resnet"
LBPH = "opencv_lbph"
INSIGHTFACE = "insightface_arcface_buffalo_l"
SFACE = "opencv_sface"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair(pair_id, sample_a, sample_b, same_person, slices=None):
    row = {
        "id": pair_id,
        "sample_a": sample_a,
        "sample_b": sample_b,
        "same_person": same_person,
    }
    if slices is not None:
        row["slices"] = slices
    return row


def _fixture(pairs, samples=None, notes="synthetic test fixture"):
    return {
        "version": "1.0",
        "notes": notes,
        "pairs": pairs,
        "samples": samples or {},
    }


def _vector_with_cosine(c: float) -> list[float]:
    """A 2-D unit vector whose cosine with ``[1, 0]`` is exactly ``c``."""
    return [c, math.sqrt(max(0.0, 1.0 - c * c))]


def _separated_fixture() -> dict:
    """Same pairs ~0.95-0.99, different pairs ~0.0-0.25: trivially separable."""
    return _fixture(
        pairs=[
            _pair("s1", "alice_a", "alice_b", True, {"pose": "frontal"}),
            _pair("s2", "bob_a", "bob_b", True, {"pose": "profile"}),
            _pair("s3", "carol_a", "carol_b", True, {"pose": "frontal"}),
            _pair("d1", "alice_a", "bob_a", False, {"pose": "frontal"}),
            _pair("d2", "bob_b", "carol_a", False, {"pose": "profile"}),
            _pair("d3", "carol_b", "alice_a", False, {"pose": "frontal"}),
        ],
        samples={
            "alice_a": [1.0, 0.0, 0.0, 0.0],
            "alice_b": [0.98, 0.05, 0.0, 0.0],
            "bob_a": [0.0, 1.0, 0.0, 0.0],
            "bob_b": [0.02, 0.99, 0.0, 0.0],
            "carol_a": [0.0, 0.0, 1.0, 0.0],
            "carol_b": [0.0, 0.03, 0.98, 0.0],
        },
    )


def _overlapping_fixture() -> dict:
    """Same pairs score 0.45/0.9; different pairs score 0.1/0.55.

    No threshold can separate all four: the best achievable accuracy is 0.75.
    This proves the sweep actually maximises instead of assuming separability.
    """
    return _fixture(
        pairs=[
            _pair("s_mid", "base", "same_mid", True),
            _pair("s_high", "base", "same_high", True),
            _pair("d_high", "base", "diff_high", False),
            _pair("d_low", "base", "diff_low", False),
        ],
        samples={
            "base": [1.0, 0.0],
            "same_mid": _vector_with_cosine(0.45),
            "same_high": _vector_with_cosine(0.90),
            "diff_high": _vector_with_cosine(0.55),
            "diff_low": _vector_with_cosine(0.10),
        },
    )


def _report(fixture_doc, candidate=DLIB, embedder_kind="mock", metric="cosine"):
    return build_report(
        candidate_id=candidate,
        fixture=fixture_doc,
        embedder=fixture_embedder(fixture_doc),
        embedder_kind=embedder_kind,
        metric=metric,
        generated_at="2026-11-12T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Fixture contract
# ---------------------------------------------------------------------------


def test_shipped_fixture_is_valid_and_mock_only():
    doc = load_fixture(DEFAULT_FIXTURE_PATH)
    assert doc["version"] == "1.0"
    assert doc["kind"] == "synthetic_identity_bench_fixture"
    assert len(doc["pairs"]) >= 4
    assert "synthetic" in doc["notes"].lower()
    # Both classes are present, which calibration requires.
    labels = {pair["same_person"] for pair in doc["pairs"]}
    assert labels == {True, False}


def test_invalid_fixture_missing_label_is_rejected():
    broken = _fixture([_pair("p1", "a", "b", True)], {"a": [1.0], "b": [1.0]})
    broken["pairs"][0].pop("same_person")
    with pytest.raises(InvalidFixtureError, match="same_person"):
        validate_fixture(broken)


def test_duplicate_pair_ids_are_rejected():
    broken = _fixture(
        [_pair("dup", "a", "b", True), _pair("dup", "a", "b", False)],
        {"a": [1.0], "b": [1.0]},
    )
    with pytest.raises(InvalidFixtureError, match="duplicate"):
        validate_fixture(broken)


def test_missing_fixture_file_raises(tmp_path):
    with pytest.raises(IdentityBenchError, match="not found"):
        load_fixture(tmp_path / "missing.yaml")


def test_fixture_embedder_rejects_unknown_sample():
    doc = _fixture([_pair("p1", "a", "b", True)], {"a": [1.0]})
    embed = fixture_embedder(doc)
    with pytest.raises(IdentityBenchError, match="no vector"):
        embed("b")


# ---------------------------------------------------------------------------
# Similarity and decision rule
# ---------------------------------------------------------------------------


def test_is_same_uses_a_single_inclusive_threshold_rule():
    assert is_same(0.75, 0.75) is True
    assert is_same(0.7499, 0.75) is False


def test_pair_similarity_agrees_for_identical_vectors():
    vector = [0.3, -0.4, 0.5]
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)
    assert pair_similarity(vector, vector, "cosine") == pytest.approx(1.0)
    # Euclidean is mapped to 1/(1+d) so identical vectors also score 1.0.
    assert pair_similarity(vector, vector, "euclidean") == pytest.approx(1.0)


def test_zero_magnitude_embedding_is_rejected():
    with pytest.raises(IdentityBenchError, match="zero-magnitude"):
        cosine_similarity([0.0, 0.0], [1.0, 0.0])


def test_dimension_mismatch_is_rejected():
    with pytest.raises(IdentityBenchError, match="dimensions differ"):
        cosine_similarity([1.0], [1.0, 2.0])


# ---------------------------------------------------------------------------
# Calibration, sweep, accuracy (CHAR-07)
# ---------------------------------------------------------------------------


def test_separable_fixture_calibrates_to_full_accuracy():
    report = _report(_separated_fixture())
    assert report["accuracy"] == pytest.approx(1.0)
    # The threshold must sit in the gap between same and different scores.
    assert 0.26 < report["threshold"] < 0.94
    assert report["verification_status"] == "MOCKED"


def test_accuracy_reflects_inseparable_pairs():
    report = _report(_overlapping_fixture())
    assert report["accuracy"] == pytest.approx(0.75)
    # The best split passes both same pairs and the 0.55 different pair, and
    # rejects the 0.10 different pair: 3 of 4 predictions are correct.
    assert report["calibration"]["confusion"]["true_positive"] == 2
    assert report["calibration"]["confusion"]["false_negative"] == 0
    assert report["calibration"]["confusion"]["false_positive"] == 1
    assert report["calibration"]["confusion"]["true_negative"] == 1


def test_sweep_has_a_point_between_every_observed_similarity():
    doc = _overlapping_fixture()
    scores = score_pairs(pairs_from_fixture(doc), fixture_embedder(doc))
    candidates = threshold_candidates(scores)
    # Four distinct similarities -> four gaps -> five boundaries.
    assert len(candidates) == 5
    assert candidates == sorted(candidates)
    best, sweep = calibrate_threshold(scores)
    assert len(sweep) == len(candidates)
    assert best["accuracy"] == pytest.approx(0.75)


def test_threshold_is_calibrated_per_model_not_global():
    # Two candidate models see different embedding geometry, so each gets its
    # own threshold (CHAR-07) and the reports must not share one constant.
    first = _report(_separated_fixture(), candidate=DLIB)
    shifted = _fixture(
        pairs=[
            _pair("s1", "base", "same", True),
            _pair("d1", "base", "diff", False),
        ],
        samples={
            "base": [1.0, 0.0],
            "same": _vector_with_cosine(0.95),
            "diff": _vector_with_cosine(0.30),
        },
    )
    second = _report(shifted, candidate=LBPH)
    assert first["candidate"] != second["candidate"]
    assert first["accuracy"] == pytest.approx(1.0)
    assert second["accuracy"] == pytest.approx(1.0)
    assert first["threshold"] != second["threshold"]
    assert abs(first["threshold"] - second["threshold"]) > 0.05
    # Each report names the model it is calibrated for.
    assert first["embedder_model"] != second["embedder_model"]


def test_calibration_requires_both_classes():
    doc = _fixture(
        [_pair("s1", "a", "b", True)],
        {"a": [1.0, 0.0], "b": [0.9, 0.1]},
    )
    with pytest.raises(CalibrationError, match="both"):
        calibrate_threshold(score_pairs(pairs_from_fixture(doc), fixture_embedder(doc)))


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


def test_report_has_every_required_field():
    report = _report(_separated_fixture())
    for field in REQUIRED_REPORT_FIELDS:
        assert field in report, f"missing report field {field!r}"
    assert report["candidate"] == DLIB
    assert report["device"] == "CPU"
    assert report["license"]["model"]
    assert report["license"]["library"]
    assert report["license"]["clean_for_commercial"] is True
    assert report["embedder_model"]
    assert isinstance(report["accuracy"], float)
    assert isinstance(report["threshold"], float)
    assert isinstance(report["bias_slices"], dict)
    assert isinstance(report["notes"], str) and report["notes"].strip()
    # generated_at is a parseable ISO timestamp; injected here for determinism.
    assert datetime.fromisoformat(report["generated_at"]).year == 2026
    validate_report(report)


def test_bias_slices_are_reported_per_declared_value():
    report = _report(_separated_fixture())
    assert set(report["bias_slices"]["pose"]) == {"frontal", "profile"}
    for value in report["bias_slices"]["pose"].values():
        assert value["count"] >= 1
        assert value["accuracy"] == pytest.approx(1.0)


def test_real_embedder_kind_may_be_verified_but_never_green():
    report = _report(_separated_fixture(), embedder_kind="real")
    assert report["verification_status"] == "VERIFIED"
    assert report["verification_status"] not in FORBIDDEN_STATUSES


def test_mock_embedder_is_mocked_never_green():
    report = _report(_separated_fixture(), embedder_kind="mock")
    assert report["verification_status"] == "MOCKED"
    assert report["verification_status"] not in FORBIDDEN_STATUSES
    assert "never GREEN" in report["notes"]


def test_forbidden_status_is_refused():
    for status in FORBIDDEN_STATUSES:
        report = _report(_separated_fixture())
        report["verification_status"] = status
        with pytest.raises(IdentityBenchError, match="forbidden"):
            validate_report(report)


def test_unknown_report_cannot_carry_a_fabricated_score():
    report = _report(_separated_fixture())
    report["verification_status"] = "UNKNOWN"
    with pytest.raises(IdentityBenchError, match="fabricated"):
        validate_report(report)


# ---------------------------------------------------------------------------
# Fallback: detection-only + human identity (CHAR-06)
# ---------------------------------------------------------------------------


def test_no_embedder_falls_back_to_unknown_detection_only():
    report = build_report(
        candidate_id=DLIB,
        fixture=_separated_fixture(),
        embedder=None,
        generated_at="2026-11-12T00:00:00+00:00",
    )
    assert report["verification_status"] == "UNKNOWN"
    assert report["accuracy"] is None
    assert report["threshold"] is None
    assert report["bias_slices"] == {}
    assert report["fallback"]["id"] == "detection_only_plus_human_identity"
    assert report["fallback"]["identity_decision"] == "MANUAL"
    assert report["fallback"]["identity_status"] == "UNKNOWN"
    assert "face_tracker.py" in report["notes"]
    assert "human identity" in report["notes"]


def test_blocked_candidate_falls_back_to_unknown():
    # InsightFace: permissive library, non-commercial weights -> BLOCKED.
    report = build_report(
        candidate_id=INSIGHTFACE,
        fixture=_separated_fixture(),
        embedder=fixture_embedder(_separated_fixture()),
        embedder_kind="mock",
    )
    assert report["verification_status"] == "UNKNOWN"
    assert report["accuracy"] is None
    assert report["threshold"] is None
    assert report["fallback"]["identity_status"] == "UNKNOWN"


def test_unknown_license_candidate_falls_back_to_unknown():
    # SFace: model license UNKNOWN is never promoted to usable.
    report = build_report(
        candidate_id=SFACE,
        fixture=_separated_fixture(),
        embedder=fixture_embedder(_separated_fixture()),
    )
    assert report["verification_status"] == "UNKNOWN"
    assert report["accuracy"] is None


def test_undeclared_candidate_falls_back_instead_of_guessing():
    report = build_report(
        candidate_id="not_a_model",
        fixture=_separated_fixture(),
        embedder=fixture_embedder(_separated_fixture()),
    )
    assert report["verification_status"] == "UNKNOWN"
    assert report["candidate"] == "not_a_model"
    assert report["accuracy"] is None


# ---------------------------------------------------------------------------
# Offline guarantee + CLI
# ---------------------------------------------------------------------------


def test_bench_runs_with_outbound_sockets_blocked(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the identity bench must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    report = _report(_separated_fixture())
    assert report["accuracy"] == pytest.approx(1.0)


def test_cli_prints_a_report_as_json(capsys):
    exit_code = main(["--json", "--candidate", DLIB])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate"] == DLIB
    assert payload["verification_status"] == "MOCKED"
    for field in REQUIRED_REPORT_FIELDS:
        assert field in payload


def test_cli_defaults_to_first_usable_candidate(capsys):
    exit_code = main(["--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    # The first declared usable candidate is the dlib ResNet.
    assert payload["candidate"] == DLIB
    assert payload["fallback"] is None