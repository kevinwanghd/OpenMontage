"""AUD-01/AUD-04 provider acceptance scaffolds for TTS and lip-sync.

The requirements (v2.4 AUD-01, AUD-03, AUD-04, MOD-05, R3, R12, R25) agree on
one rule: **a provider is not available until it has been measured**. A tool
being registered, a schema validating, or a mock adapter returning a fixture
proves the *contract* only -- never voice quality, lip-sync quality, multi-person
correctness, commercial authorisation, price or quota.

This module therefore has two deliberately separate modes:

- ``mode="mock"`` (the default) runs a deterministic fake adapter and validates
  the contract shape. It makes zero network calls, spends nothing, and reports
  ``MOCKED``. ``MOCKED`` is not a quality verdict.
- ``mode="live"`` is also offline *here*: it only proves the operator approved
  the spend. It must be enabled with ``approve_live=True`` **and** an approved
  budget record, otherwise it raises :class:`LiveApprovalRequired` with a
  structured, agent-ready explanation. When approved it still performs no real
  provider call (this module is a scaffold), so the result is ``UNKNOWN`` -- it
  can never be ``VERIFIED``/``GREEN``.

The report records everything a human reviewer needs before M1 measurement:
provider, model, license/authorisation evidence, cost estimate, quota, rubric
(quality / lip-sync / multi-face), contract checks and timestamp. Rubric scores
stay ``None`` until a human or a real measured run fills them.

M0: local, deterministic, zero external calls. No file outside this module and
its test is touched.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

MOCK = "mock"
LIVE = "live"
MODES = (MOCK, LIVE)

# Verification vocabulary. This scaffold can only ever emit the first two;
# ``VERIFIED`` exists so callers can compare against the full catalog
# vocabulary (MOD-02) without inventing a private status.
VERIFIED = "VERIFIED"
MOCKED = "MOCKED"
UNKNOWN = "UNKNOWN"
BLOCKED = "BLOCKED"
VERIFICATION_STATUSES = (VERIFIED, MOCKED, UNKNOWN, BLOCKED)

#: "GREEN" is a doctor verdict, not a provider verification status.
FORBIDDEN_STATUSES = ("GREEN", "PASS", "OK")

LICENSE_STATUSES = ("VERIFIED_COMMERCIAL", "UNKNOWN", "RESTRICTED")
AUTHORIZATION_STATES = ("APPROVED", "UNKNOWN", "RESTRICTED")

BENCH_TTS = "tts"
BENCH_LIPSYNC = "lipsync"

_REQUIREMENTS = {
    BENCH_TTS: ("AUD-01", "AUD-03", "R12", "MOD-05"),
    BENCH_LIPSYNC: ("AUD-03", "AUD-04", "R3", "MOD-05"),
}

_RUBRIC_CRITERIA = {
    BENCH_TTS: ("audio_quality",),
    BENCH_LIPSYNC: ("lip_sync", "multi_person"),
}


class AcceptanceBenchError(Exception):
    """Base error for the acceptance scaffolds."""


class UnknownProviderError(AcceptanceBenchError):
    """The provider is not declared in this bench's registry."""


class InvalidModeError(AcceptanceBenchError):
    """``mode`` is neither ``mock`` nor ``live``."""


class LiveApprovalRequired(AcceptanceBenchError):
    """Live mode was requested without an explicit, budgeted approval.

    The error is intentionally agent-ready: ``to_dict()`` exposes the refusal
    reasons and the exact next action, so an automated caller must stop and ask
    the operator instead of retrying or guessing.
    """

    code = "LIVE_BENCH_NOT_APPROVED"
    agent_action = (
        "Stop and ask the user to approve the spend. Then call live mode with "
        "approve_live=True and budget={'approved': True, 'max_usd': <positive "
        "USD cap>, 'approved_by': '<name>'}. Do not retry live mode without "
        "both, and never report a mock result as VERIFIED or GREEN."
    )

    def __init__(self, reasons: list[str], decision: dict[str, Any]) -> None:
        self.reasons = list(reasons)
        self.decision = copy.deepcopy(decision)
        super().__init__(
            "live acceptance bench refused: " + "; ".join(self.reasons)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": str(self),
            "reasons": list(self.reasons),
            "required": {
                "approve_live": True,
                "budget": {"approved": True, "max_usd": "> 0", "approved_by": "name"},
            },
            "decision": copy.deepcopy(self.decision),
            "agent_action": self.agent_action,
        }


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

# Cost figures are *estimates and sources*, never measured spend:
# - doubao: tools/audio/doubao_tts.py estimate_cost() bills per character.
# - kling: tools/_kling/account.py can read costs but no snapshot is recorded
#   here, so the estimate is UNKNOWN rather than a guessed number.
# Licences are UNKNOWN until a human attaches evidence (R25, AUD-01).
TTS_PROVIDERS: dict[str, dict[str, Any]] = {
    "doubao": {
        "id": "doubao",
        "model": "doubao-seed-tts-2.0",
        "license_status": "UNKNOWN",
        "authorization": {
            "commercial_use": "UNKNOWN",
            "evidence": "",
            "source": "AUD-01 / R12: confirm Volcengine commercial terms before use",
        },
        "cost_estimate": {
            "amount_usd": 0.0015,
            "currency": "USD",
            "basis": "100 characters @ 0.000015 USD/character",
            "status": "ESTIMATE",
            "source": "tools/audio/doubao_tts.py estimate_cost()",
        },
        "quota": {
            "status": "UNKNOWN",
            "limit": None,
            "unit": "characters",
            "notes": "Volcengine concurrency/quota must be measured live",
        },
    },
    "azure": {
        "id": "azure",
        "model": "azure-neural-tts",
        "license_status": "UNKNOWN",
        "authorization": {
            "commercial_use": "UNKNOWN",
            "evidence": "",
            "source": "AUD-01 fallback candidate; terms not yet recorded",
        },
        "cost_estimate": {
            "amount_usd": None,
            "currency": "USD",
            "basis": "pricing not captured in-repo",
            "status": "UNKNOWN",
            "source": "AUD-01 fallback candidate",
        },
        "quota": {
            "status": "UNKNOWN",
            "limit": None,
            "unit": "characters",
            "notes": "Speech resource quota must be measured live",
        },
    },
    "elevenlabs": {
        "id": "elevenlabs",
        "model": "elevenlabs-tts",
        "license_status": "UNKNOWN",
        "authorization": {
            "commercial_use": "UNKNOWN",
            "evidence": "",
            "source": "AUD-01 fallback candidate; terms not yet recorded",
        },
        "cost_estimate": {
            "amount_usd": None,
            "currency": "USD",
            "basis": "pricing not captured in-repo",
            "status": "UNKNOWN",
            "source": "AUD-01 fallback candidate",
        },
        "quota": {
            "status": "UNKNOWN",
            "limit": None,
            "unit": "characters",
            "notes": "plan quota must be measured live",
        },
    },
}

LIPSYNC_PROVIDERS: dict[str, dict[str, Any]] = {
    "kling_official": {
        "id": "kling_official",
        "model": "kling-advanced-lip-sync",
        "license_status": "UNKNOWN",
        "authorization": {
            "commercial_use": "UNKNOWN",
            "evidence": "",
            "source": "AUD-04 / R3: Kling cloud terms and multi-person use not yet recorded",
        },
        "cost_estimate": {
            "amount_usd": None,
            "currency": "USD",
            "basis": "account cost not snapshotted; read via tools/_kling/account.py",
            "status": "UNKNOWN",
            "source": "tools/_kling/account.py get_account_costs()",
        },
        "quota": {
            "status": "UNKNOWN",
            "limit": None,
            "unit": "tasks",
            "notes": "Kling resource-pack quota must be read live",
        },
    },
    "lip_sync": {
        "id": "lip_sync",
        "model": "wav2lip",
        "license_status": "UNKNOWN",
        "authorization": {
            "commercial_use": "UNKNOWN",
            "evidence": "",
            "source": "AUD-04 / R3: model weights licence not verified",
        },
        "cost_estimate": {
            "amount_usd": 0.0,
            "currency": "USD",
            "basis": "local GPU inference (no per-call fee)",
            "status": "ESTIMATE",
            "source": "tools/avatar/lip_sync.py",
        },
        "quota": {
            "status": "UNKNOWN",
            "limit": None,
            "unit": "local_gpu",
            "notes": "CUDA unavailable on this machine (F8); contract only",
        },
    },
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceBenchError(f"{where} must be a non-empty string")
    return value.strip()


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceBenchError(f"{where} must be an object")
    return dict(value)


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _normalize_spec(raw: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Validate one provider declaration and return a deep copy."""
    data = _mapping(raw, where)
    spec = copy.deepcopy(data)
    spec["id"] = _text(data.get("id"), f"{where}.id")
    spec["model"] = _text(data.get("model"), f"{where}.model")

    license_status = data.get("license_status")
    if license_status not in LICENSE_STATUSES:
        raise AcceptanceBenchError(
            f"{where}.license_status must be one of {LICENSE_STATUSES}"
        )
    spec["license_status"] = license_status

    authorization = _mapping(data.get("authorization"), f"{where}.authorization")
    commercial_use = authorization.get("commercial_use")
    if commercial_use not in AUTHORIZATION_STATES:
        raise AcceptanceBenchError(
            f"{where}.authorization.commercial_use must be one of "
            f"{AUTHORIZATION_STATES}"
        )
    authorization["evidence"] = str(authorization.get("evidence") or "")
    authorization["source"] = str(authorization.get("source") or "")
    spec["authorization"] = authorization

    cost = _mapping(data.get("cost_estimate"), f"{where}.cost_estimate")
    amount = cost.get("amount_usd")
    if amount is not None:
        normalized_amount = _finite_number(amount)
        if normalized_amount is None or normalized_amount < 0:
            raise AcceptanceBenchError(
                f"{where}.cost_estimate.amount_usd must be a non-negative number "
                "or null"
            )
        amount = normalized_amount
    if cost.get("status") not in ("ESTIMATE", "MEASURED", "UNKNOWN"):
        raise AcceptanceBenchError(
            f"{where}.cost_estimate.status must be ESTIMATE, MEASURED or UNKNOWN"
        )
    if amount is None and cost.get("status") != "UNKNOWN":
        raise AcceptanceBenchError(
            f"{where}.cost_estimate.amount_usd is null but status is not UNKNOWN"
        )
    cost["amount_usd"] = amount
    cost["currency"] = str(cost.get("currency") or "USD")
    cost["basis"] = str(cost.get("basis") or "")
    cost["source"] = str(cost.get("source") or "")
    spec["cost_estimate"] = cost

    quota = _mapping(data.get("quota"), f"{where}.quota")
    if quota.get("status") not in ("UNKNOWN", "KNOWN", "EXHAUSTED"):
        raise AcceptanceBenchError(
            f"{where}.quota.status must be UNKNOWN, KNOWN or EXHAUSTED"
        )
    limit = quota.get("limit")
    if limit is not None and _finite_number(limit) is None:
        raise AcceptanceBenchError(f"{where}.quota.limit must be a number or null")
    quota["unit"] = str(quota.get("unit") or "")
    quota["notes"] = str(quota.get("notes") or "")
    spec["quota"] = quota

    return spec


def _resolve_spec(bench: str, provider: Any) -> dict[str, Any]:
    if isinstance(provider, Mapping):
        return _normalize_spec(provider, "provider")

    key = _text(provider, "provider").strip().lower()
    registry = TTS_PROVIDERS if bench == BENCH_TTS else LIPSYNC_PROVIDERS
    if key not in registry:
        raise UnknownProviderError(
            f"unknown {bench} provider {key!r}; declared providers: "
            f"{sorted(registry)}. Add a declaration with licence, cost and quota "
            "fields; do not silently invent one."
        )
    return _normalize_spec(registry[key], f"providers.{key}")


# ---------------------------------------------------------------------------
# Mock contract adapters
# ---------------------------------------------------------------------------

#: Marker carried by every mock artefact. Nothing here touches a real system.
MOCK_URI_PREFIX = "mock://"


def _check(name: str, passed: bool, detail: str, evidence: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "detail": detail,
        "evidence": evidence or f"{MOCK_URI_PREFIX}{name}",
    }


def _mock_tts_contract(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Simulate a TTS adapter response and validate its shape.

    The fixture is deterministic and in-memory. It proves the *contract* the
    real adapter must satisfy (audio artefact, positive duration, monotonic
    sentence timestamps aligned to the audio) and nothing about quality.
    """
    fixture = {
        "audio_path": f"{MOCK_URI_PREFIX}tts/sample.mp3",
        "format": "mp3",
        "duration_seconds": 1.5,
        "sample_rate": 24000,
        "sentences": [
            {"start": 0.0, "end": 0.75, "text": "Mock line one."},
            {"start": 0.75, "end": 1.5, "text": "Mock line two."},
        ],
        "usage": {"characters": 27},
    }

    required = {"audio_path", "format", "duration_seconds", "sentences", "usage"}
    output_ok = required.issubset(fixture) and fixture["duration_seconds"] > 0
    sentences = fixture["sentences"]
    monotonic = bool(sentences) and sentences[0]["start"] == 0.0
    previous_end = 0.0
    for sentence in sentences:
        start, end = sentence.get("start"), sentence.get("end")
        if not (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and start < end
            and start >= previous_end
            and end <= fixture["duration_seconds"]
        ):
            monotonic = False
            break
        previous_end = end

    return [
        _check(
            "adapter_output_contract",
            output_ok,
            "fake adapter returns an audio path, format, positive duration, "
            "sentence timestamps and usage",
        ),
        _check(
            "timestamp_monotonic_alignment",
            monotonic,
            "sentence timestamps are monotonic and inside the audio duration",
        ),
        _check(
            "authorization_recorded",
            spec["authorization"]["commercial_use"] in AUTHORIZATION_STATES,
            f"commercial_use={spec['authorization']['commercial_use']} "
            "(recorded, not verified)",
        ),
        _check(
            "zero_cost",
            True,
            "mock mode spends nothing; incurred_cost_usd stays 0.0",
        ),
        _check(
            "no_network",
            True,
            "fake adapter is in-memory; no socket or provider SDK is touched",
        ),
    ]


def _mock_lipsync_contract(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Simulate a two-person lip-sync flow and validate the parameter contract.

    AUD-04/F8: a multi-person short-drama frame may not be silently downsized to
    the largest face. The flow must identify faces first and then select faces
    explicitly; ``auto_select_face`` must be opt-in, never a default.
    """
    fixture = {
        "operation": "advanced_lip_sync",
        "faces_detected": 2,
        "identified_face_ids": ["face-a", "face-b"],
        "face_choose": [
            {
                "face_id": "face-a",
                "sound_start_time": 0,
                "sound_end_time": 1000,
            }
        ],
        "auto_select_face": False,
        "aligned_audio_path": f"{MOCK_URI_PREFIX}lipsync/aligned.wav",
        "duration_seconds": 2.0,
    }

    identified = fixture["identified_face_ids"]
    chosen = fixture["face_choose"]
    identified_ok = (
        len(identified) >= 2
        and bool(chosen)
        and all(entry.get("face_id") in identified for entry in chosen)
    )
    explicit_ok = bool(chosen) and all(
        isinstance(entry.get("face_id"), str) and entry["face_id"]
        for entry in chosen
    )
    selection_times_ok = all(
        isinstance(entry.get("sound_start_time"), int)
        and isinstance(entry.get("sound_end_time"), int)
        and entry["sound_start_time"] < entry["sound_end_time"]
        for entry in chosen
    )
    audio_contract_ok = bool(fixture["aligned_audio_path"]) and (
        fixture["duration_seconds"] > 0
    )

    return [
        _check(
            "multi_person_identify_face",
            identified_ok,
            "two faces are identified before advanced_lip_sync and every "
            "chosen face comes from the identified set",
        ),
        _check(
            "explicit_face_selection",
            explicit_ok and selection_times_ok,
            "face_choose names a face_id with a valid sound_start_time/"
            "sound_end_time window; no implicit first-face fallback",
        ),
        _check(
            "no_silent_auto_select",
            fixture["auto_select_face"] is False,
            "auto_select_face is False; largest-face selection requires "
            "explicit opt-in",
        ),
        _check(
            "aligned_audio_output_contract",
            audio_contract_ok,
            "fake adapter returns an aligned audio artefact and duration for "
            "AUD-03/AUD-05 subtitle alignment",
        ),
        _check(
            "authorization_recorded",
            spec["authorization"]["commercial_use"] in AUTHORIZATION_STATES,
            f"commercial_use={spec['authorization']['commercial_use']} "
            "(recorded, not verified)",
        ),
        _check(
            "zero_cost",
            True,
            "mock mode spends nothing; incurred_cost_usd stays 0.0",
        ),
        _check(
            "no_network",
            True,
            "fake adapter is in-memory; no socket or provider SDK is touched",
        ),
    ]


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------


def _budget_cap(budget: Mapping[str, Any]) -> Optional[float]:
    cap = _finite_number(budget.get("max_usd"))
    return cap if cap is not None and cap > 0 else None


def _evaluate_live_gate(
    approve_live: bool,
    budget: Any,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Return a decision record; ``approved`` is True only when every gate passes."""
    reasons: list[str] = []
    if approve_live is not True:
        reasons.append(
            "approve_live=True is required: live provider calls spend money and "
            "must be approved explicitly"
        )

    cap: Optional[float] = None
    approved_flag: Any = None
    approved_by = ""
    if not isinstance(budget, Mapping):
        reasons.append(
            "an approved budget record is required: budget={'approved': True, "
            "'max_usd': <positive USD cap>, 'approved_by': '<name>'}"
        )
    else:
        approved_flag = budget.get("approved")
        approved_by = str(budget.get("approved_by") or "")
        if approved_flag is not True:
            reasons.append("budget.approved must be exactly True")
        cap = _budget_cap(budget)
        if cap is None:
            reasons.append("budget.max_usd must be a positive, finite USD cap")

    amount = spec["cost_estimate"]["amount_usd"]
    if amount is None:
        reasons.append(
            "cost_estimate.amount_usd is UNKNOWN; obtain a priced quote before "
            "approving live spend"
        )
    elif cap is not None and amount > cap:
        reasons.append(
            f"estimated cost {amount:.6f} USD exceeds the approved cap "
            f"{cap:.6f} USD"
        )

    decision = {
        "required": True,
        "approved": not reasons,
        "approve_live": approve_live is True,
        "budget_approved": approved_flag,
        "approved_by": approved_by,
        "max_usd": cap,
        "estimated_usd": amount,
        "reasons": reasons,
    }
    return decision


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _rubric(bench: str, mode: str) -> dict[str, dict[str, Any]]:
    method = "mock_contract_only" if mode == MOCK else "not_run"
    return {
        criterion: {
            "score": None,
            "scale": "0-5",
            "measured": False,
            "status": "UNRATED",
            "method": method,
            "evidence": "",
            "human_required": True,
        }
        for criterion in _RUBRIC_CRITERIA[bench]
    }


def _finalize_status(status: str, mode: str) -> str:
    """Enforce the core rule: an unmeasured run is never VERIFIED/GREEN."""
    if status in FORBIDDEN_STATUSES:
        raise AcceptanceBenchError(f"forbidden verification status {status!r}")
    if status not in VERIFICATION_STATUSES:
        raise AcceptanceBenchError(
            f"verification status {status!r} is not in {VERIFICATION_STATUSES}"
        )
    if mode == MOCK and status not in (MOCKED, BLOCKED):
        raise AcceptanceBenchError(
            f"mock mode may only report {MOCKED} or {BLOCKED}, got {status!r}"
        )
    if mode == LIVE and status != UNKNOWN:
        raise AcceptanceBenchError(
            "this scaffold performs no real provider call in live mode; "
            f"it may only report {UNKNOWN}, got {status!r}"
        )
    if status == VERIFIED:
        raise AcceptanceBenchError(
            "VERIFIED requires a measured live run and is not produced by this scaffold"
        )
    return status


def _run_bench(
    bench: str,
    spec: dict[str, Any],
    *,
    mode: str,
    approve_live: bool,
    budget: Any,
) -> dict[str, Any]:
    if mode not in MODES:
        raise InvalidModeError(f"mode must be one of {MODES}, got {mode!r}")

    if mode == MOCK:
        adapter = _mock_tts_contract if bench == BENCH_TTS else _mock_lipsync_contract
        checks = adapter(spec)
        passed = all(check["passed"] for check in checks)
        status = _finalize_status(MOCKED if passed else BLOCKED, mode)
        budget_decision = {
            "required": False,
            "approved": None,
            "approve_live": approve_live is True,
            "budget_approved": None,
            "approved_by": "",
            "max_usd": None,
            "estimated_usd": spec["cost_estimate"]["amount_usd"],
            "reasons": [],
            "note": "mock mode is zero-cost; a live budget is not required",
        }
        incurred_cost_usd = 0.0
        notes = [
            f"{bench.upper()} contract check only: the fake adapter made no "
            "provider call and no network connection.",
            "MOCKED is not a quality, licence, price or quota verdict. Do not "
            "report it as VERIFIED or GREEN.",
            "M1 must run the real provider and a human must fill the rubric "
            "scores before this provider can be called AVAILABLE.",
        ]
    else:
        budget_decision = _evaluate_live_gate(approve_live, budget, spec)
        if not budget_decision["approved"]:
            raise LiveApprovalRequired(budget_decision["reasons"], budget_decision)
        checks = []
        status = _finalize_status(UNKNOWN, mode)
        incurred_cost_usd = 0.0
        notes = [
            "Live spend was approved, but this scaffold is offline by design: "
            "no real provider call was executed.",
            "UNKNOWN is the correct status until a measured run produces audio/"
            "video artefacts and a human fills the rubric.",
            "Do not promote UNKNOWN to VERIFIED merely because approval passed.",
        ]

    return {
        "bench": bench,
        "provider": spec["id"],
        "model": spec["model"],
        "mode": mode,
        "license_status": spec["license_status"],
        "authorization": copy.deepcopy(spec["authorization"]),
        "cost_estimate": copy.deepcopy(spec["cost_estimate"]),
        "quota": copy.deepcopy(spec["quota"]),
        "verification_status": status,
        "verified": False,
        "incurred_cost_usd": incurred_cost_usd,
        "rubric": _rubric(bench, mode),
        "contract_checks": checks,
        "budget": budget_decision,
        "requirements": list(_REQUIREMENTS[bench]),
        "generated_at": _now(),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_tts_bench(
    provider: Any,
    *,
    mode: str = MOCK,
    approve_live: bool = False,
    budget: Any = None,
) -> dict[str, Any]:
    """Run the AUD-01 TTS acceptance scaffold.

    Default ``mode="mock"``: deterministic contract check, zero API, zero cost,
    status ``MOCKED``. ``mode="live"`` requires ``approve_live=True`` and an
    approved ``budget`` record, then still performs no real call and reports
    ``UNKNOWN``.
    """
    spec = _resolve_spec(BENCH_TTS, provider)
    return _run_bench(
        BENCH_TTS, spec, mode=mode, approve_live=approve_live, budget=budget
    )


def run_lipsync_bench(
    provider: Any,
    *,
    mode: str = MOCK,
    approve_live: bool = False,
    budget: Any = None,
) -> dict[str, Any]:
    """Run the AUD-03/AUD-04 lip-sync acceptance scaffold.

    Validates the multi-person parameter contract (identify faces first, then
    select explicitly; no silent largest-face fallback), records licence/cost
    and refuses to claim verification. Same mock/live gate as
    :func:`run_tts_bench`.
    """
    spec = _resolve_spec(BENCH_LIPSYNC, provider)
    return _run_bench(
        BENCH_LIPSYNC, spec, mode=mode, approve_live=approve_live, budget=budget
    )