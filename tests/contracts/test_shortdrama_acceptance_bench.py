"""Contract tests for the AUD-01/AUD-04 provider acceptance scaffolds.

Guards (requirements v2.4 AUD-01, AUD-03, AUD-04, MOD-05, R3, R12, R25):

- The default mock run performs zero external calls and spends nothing.
- A mock run can never report ``VERIFIED``/``GREEN``; it is ``MOCKED``.
- Live mode without explicit approval **and** an approved budget is refused
  with an agent-ready error, not retried silently.
- Live approval still does not verify anything: the offline scaffold reports
  ``UNKNOWN`` because no real provider call happened.
- Reports carry provider/model/licence/authorisation/cost/quota/rubric/
  timestamp fields for the M1 human review.
- The lip-sync contract requires identify-face-first and explicit face
  selection instead of a silent largest-face fallback.

No network: the session-wide socket guard in ``tests/conftest.py`` is on, and
the primary tests also replace ``socket.connect`` to fail loudly.
"""
from __future__ import annotations

import socket
from datetime import datetime

import pytest

import lib.acceptance_bench as ab


REQUIRED_REPORT_KEYS = {
    "bench",
    "provider",
    "model",
    "mode",
    "license_status",
    "authorization",
    "cost_estimate",
    "quota",
    "verification_status",
    "verified",
    "incurred_cost_usd",
    "rubric",
    "contract_checks",
    "budget",
    "requirements",
    "generated_at",
    "notes",
}

APPROVED_BUDGET = {"approved": True, "max_usd": 1.0, "approved_by": "owner"}


@pytest.fixture()
def no_network(monkeypatch):
    """Fail the test if anything opens a socket."""

    def blocked(*args, **kwargs):
        raise AssertionError("acceptance bench attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


# ---------------------------------------------------------------------------
# Mock mode: zero calls, MOCKED, not VERIFIED
# ---------------------------------------------------------------------------


def test_tts_mock_is_zero_api_and_mocked(no_network):
    report = ab.run_tts_bench("doubao")

    assert report["mode"] == "mock"
    assert report["verification_status"] == "MOCKED"
    assert report["verified"] is False
    assert report["incurred_cost_usd"] == 0.0
    assert report["budget"]["required"] is False
    assert all(check["passed"] for check in report["contract_checks"])
    assert "no_network" in {check["name"] for check in report["contract_checks"]}


def test_lipsync_mock_is_zero_api_and_mocked(no_network):
    report = ab.run_lipsync_bench("kling_official")

    assert report["verification_status"] == "MOCKED"
    assert report["incurred_cost_usd"] == 0.0
    names = {check["name"] for check in report["contract_checks"]}
    assert {
        "multi_person_identify_face",
        "explicit_face_selection",
        "no_silent_auto_select",
        "aligned_audio_output_contract",
    }.issubset(names)
    assert all(check["passed"] for check in report["contract_checks"])


def test_mock_never_claims_verified_or_green(no_network):
    for report in (
        ab.run_tts_bench("doubao"),
        ab.run_lipsync_bench("kling_official"),
    ):
        assert report["verification_status"] not in ("VERIFIED", "GREEN", "PASS", "OK")
        assert report["verified"] is False
        for criterion in report["rubric"].values():
            assert criterion["score"] is None
            assert criterion["measured"] is False
            assert criterion["status"] == "UNRATED"
        assert any("Do not report it as VERIFIED or GREEN" in n for n in report["notes"])


def test_mock_contract_failure_is_blocked(monkeypatch):
    failed = [
        ab._check("multi_person_identify_face", False, "fixture rejects silent auto-select"),
    ]
    monkeypatch.setattr(ab, "_mock_lipsync_contract", lambda spec: failed)

    report = ab.run_lipsync_bench("kling_official")

    assert report["verification_status"] == "BLOCKED"
    assert report["verified"] is False


# ---------------------------------------------------------------------------
# Live mode: approval + budget gate
# ---------------------------------------------------------------------------


def test_live_tts_without_approval_is_rejected(no_network):
    with pytest.raises(ab.LiveApprovalRequired) as excinfo:
        ab.run_tts_bench("doubao", mode="live")

    payload = excinfo.value.to_dict()
    assert payload["error"] == "LIVE_BENCH_NOT_APPROVED"
    assert payload["required"]["approve_live"] is True
    assert payload["agent_action"]
    joined = " ".join(payload["reasons"])
    assert "approve_live=True" in joined
    assert "budget" in joined


@pytest.mark.parametrize(
    "budget, expected_fragment",
    [
        (None, "approved budget record is required"),
        ({"approved": False, "max_usd": 1.0}, "budget.approved must be exactly True"),
        ({"approved": True}, "budget.max_usd must be a positive"),
        ({"approved": True, "max_usd": 0}, "budget.max_usd must be a positive"),
        ({"approved": True, "max_usd": -1.0}, "budget.max_usd must be a positive"),
        # doubao's declared estimate is 0.0015 USD; a 0.0001 cap is too small.
        ({"approved": True, "max_usd": 0.0001}, "exceeds the approved cap"),
    ],
)
def test_live_budget_gate_rejects_incomplete_or_over_budget(budget, expected_fragment):
    with pytest.raises(ab.LiveApprovalRequired) as excinfo:
        ab.run_tts_bench("doubao", mode="live", approve_live=True, budget=budget)

    assert any(expected_fragment in reason for reason in excinfo.value.reasons)
    assert excinfo.value.decision["approved"] is False


def test_live_costing_unknown_provider_is_rejected_even_with_budget():
    # Kling's account cost is not snapshotted in-repo, so its estimate is
    # UNKNOWN. An approved cap alone is not enough to buy an unpriced call.
    with pytest.raises(ab.LiveApprovalRequired) as excinfo:
        ab.run_lipsync_bench(
            "kling_official",
            mode="live",
            approve_live=True,
            budget=APPROVED_BUDGET,
        )

    assert any("UNKNOWN" in reason for reason in excinfo.value.reasons)


def test_live_approved_but_no_real_call_is_unknown_not_verified(no_network):
    report = ab.run_tts_bench(
        "doubao", mode="live", approve_live=True, budget=APPROVED_BUDGET
    )

    assert report["mode"] == "live"
    assert report["budget"]["required"] is True
    assert report["budget"]["approved"] is True
    assert report["verification_status"] == "UNKNOWN"
    assert report["verification_status"] != "VERIFIED"
    assert report["verified"] is False
    assert report["incurred_cost_usd"] == 0.0
    assert report["contract_checks"] == []
    assert any("no real provider call was executed" in n for n in report["notes"])


def test_live_lipsync_also_requires_approval():
    with pytest.raises(ab.LiveApprovalRequired):
        ab.run_lipsync_bench("kling_official", mode="live", approve_live=True)


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report",
    [
        ab.run_tts_bench("doubao"),
        ab.run_tts_bench("azure"),
        ab.run_lipsync_bench("kling_official"),
        ab.run_lipsync_bench("lip_sync"),
    ],
)
def test_report_has_all_required_fields(report):
    assert REQUIRED_REPORT_KEYS.issubset(report)
    assert report["provider"]
    assert report["model"]
    assert report["license_status"] in ab.LICENSE_STATUSES
    assert report["authorization"]["commercial_use"] in ab.AUTHORIZATION_STATES
    assert "amount_usd" in report["cost_estimate"]
    assert "status" in report["quota"]
    assert datetime.fromisoformat(report["generated_at"]) is not None
    assert report["notes"]


def test_rubric_covers_tts_quality_and_lipsync_multiface():
    tts = ab.run_tts_bench("doubao")
    lipsync = ab.run_lipsync_bench("kling_official")

    assert set(tts["rubric"]) == {"audio_quality"}
    assert set(lipsync["rubric"]) == {"lip_sync", "multi_person"}


def test_custom_provider_declaration_is_validated_and_recorded():
    spec = {
        "id": "fixture_tts",
        "model": "fixture-model",
        "license_status": "UNKNOWN",
        "authorization": {
            "commercial_use": "UNKNOWN",
            "evidence": "",
            "source": "test fixture",
        },
        "cost_estimate": {
            "amount_usd": 0.0,
            "currency": "USD",
            "basis": "fixture",
            "status": "ESTIMATE",
            "source": "test",
        },
        "quota": {"status": "UNKNOWN", "limit": None, "unit": "characters"},
    }

    report = ab.run_tts_bench(spec)

    assert report["provider"] == "fixture_tts"
    assert report["model"] == "fixture-model"
    assert report["verification_status"] == "MOCKED"


def test_invalid_custom_provider_is_rejected():
    with pytest.raises(ab.AcceptanceBenchError):
        ab.run_tts_bench(
            {
                "id": "bad",
                "model": "bad",
                "license_status": "GREEN",
                "authorization": {},
                "cost_estimate": {},
                "quota": {},
            }
        )


# ---------------------------------------------------------------------------
# Input errors
# ---------------------------------------------------------------------------


def test_unknown_provider_is_rejected_with_declared_list():
    with pytest.raises(ab.UnknownProviderError) as excinfo:
        ab.run_tts_bench("not-a-provider")

    assert "doubao" in str(excinfo.value)
    assert "elevenlabs" in str(excinfo.value)


def test_invalid_mode_is_rejected():
    with pytest.raises(ab.InvalidModeError):
        ab.run_tts_bench("doubao", mode="dry-run")


def test_unknown_lipsync_provider_is_rejected():
    with pytest.raises(ab.UnknownProviderError):
        ab.run_lipsync_bench("heygen_video")