"""Capability slots, bindings and doctor for the short-drama pipeline.

Three layers, deliberately split (MOD-01, requirements v2.4 §2.3):

- **L1 semantics** -- pipeline manifests say ``slot:video_primary``, never a
  tool name.
- **L2 slots** -- this module resolves a slot name through the active profile
  to a concrete tool.
- **L3 bindings** -- ``bindings/`` holds profiles, the capability catalog and
  local account state.

Switching a profile therefore touches one line (``active_profile`` in
``bindings/model_bindings.yaml``) or one argument; pipeline files stay byte
identical.

Contract (frozen for M0, parallel worktree plan §1.2)::

    resolve(slot, profile, *, operation=None) -> str        # tool name
    supports(slot, profile) -> dict                          # normalized caps
    plan_degradation(slots, profile, requirements) -> list[dict]
    doctor(profile) -> {status: GREEN|DEGRADED|BLOCKED, reasons, degradation}

Invariants:
- Unknown is never support: an absent capability reads as ``"unknown"`` and
  fails a boolean requirement instead of passing it (MOD-02).
- Every degradation path returned by :func:`plan_degradation` comes from a
  plan predeclared in the profile's ``degradation:`` block. Code never invents
  a sub-stage (MOD-06 / F14); when no plan exists the slot reports BLOCKED.
- Account state separates "no credential" (BLOCKED) from "credits exhausted"
  (UNAVAILABLE, MOD-03) and from "session token present but unverified"
  (DEGRADED with SESSION_UNVERIFIED, F15).
- M0 makes zero external API calls. Tool availability is read from the local
  registry's env-backed status; account state comes from a local YAML file.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BINDINGS_DIR = _REPO_ROOT / "bindings"

#: Override for tests and for `om --bindings-dir`. Read on every call so a
#: monkeypatched env var needs no module reload.
BINDINGS_DIR_ENV = "SHORTDRAMA_BINDINGS_DIR"

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: capability fields normalized out of the catalog and legacy ``supports``.
CAPABILITY_FIELDS = (
    "native_audio",
    "lipsync_from_dialogue",
    "reference_images_max",
    "reference_video",
    "reference_audio",
    "multi_shot",
    "cross_shot_identity",
    "aspect_ratios",
    "min_duration_s",
    "max_duration_s",
    "output_resolutions",
    "seed_control",
    "first_frame_conditioning",
)

#: legacy ``BaseTool.supports`` keys -> canonical capability names (F13).
#: Only straight renames are mapped; boolean flags such as ``resolution: true``
#: carry no concrete value and are left out on purpose (unknown != supported).
_LEGACY_ALIASES = {
    "max_reference_images": "reference_images_max",
    "reference_images": "reference_images_max",
    "audio": "native_audio",
    "with_audio": "native_audio",
    "lipsync": "lipsync_from_dialogue",
    "first_frame": "first_frame_conditioning",
    "aspect_ratio": "aspect_ratios",
    "resolution": "output_resolutions",
}

_VERIFIED = "VERIFIED"
_UNVERIFIED = "UNVERIFIED"


class CapabilitySlotsError(Exception):
    """Base error for slot resolution problems."""


class UnknownProfileError(CapabilitySlotsError):
    """The requested profile has no definition under bindings/profiles/."""


class UnknownSlotError(CapabilitySlotsError):
    """The slot is not declared in bindings/slots.yaml."""


class SlotUnavailable(CapabilitySlotsError):
    """No candidate in the slot's binding chain is usable.

    Carries structured ``reasons`` so callers (and doctor) can report exactly
    which candidate failed and why.
    """

    def __init__(self, slot: str, profile: str, reasons: list[dict[str, Any]]):
        self.slot = slot
        self.profile = profile
        self.reasons = reasons
        detail = "; ".join(
            f"{r.get('tool')}: {r.get('code')}" for r in reasons
        ) or "no candidates declared"
        super().__init__(f"slot {slot!r} has no usable binding in profile {profile!r} ({detail})")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def bindings_dir() -> Path:
    """Return the active bindings directory (env override wins)."""
    override = os.environ.get(BINDINGS_DIR_ENV)
    return Path(override) if override else _DEFAULT_BINDINGS_DIR


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def load_slots() -> dict:
    """Return the declared slot definitions keyed by slot name."""
    data = _read_yaml(bindings_dir() / "slots.yaml")
    slots = data.get("slots") or {}
    return slots if isinstance(slots, dict) else {}


def active_profile() -> str:
    """Return the profile pointer from ``bindings/model_bindings.yaml``."""
    data = _read_yaml(bindings_dir() / "model_bindings.yaml")
    return str(data.get("active_profile") or "")


def load_profile(profile: str) -> dict:
    """Load a profile merged over ``bindings/common.yaml``.

    Profile bindings win over common bindings slot by slot; degradation plans
    are concatenated (common first) so a profile can add to the shared ones.
    """
    if not profile or not _PROFILE_NAME_RE.match(profile):
        raise UnknownProfileError(f"invalid profile name {profile!r}")
    root = bindings_dir()
    common = _read_yaml(root / "common.yaml")
    path = root / "profiles" / f"{profile}.yaml"
    if not path.is_file():
        raise UnknownProfileError(
            f"unknown profile {profile!r}: no {path.relative_to(root).as_posix()}"
        )
    data = _read_yaml(path)
    merged = dict(common)
    merged.update(data)
    bindings = dict(common.get("bindings") or {})
    bindings.update(data.get("bindings") or {})
    merged["bindings"] = bindings
    merged["degradation"] = list(common.get("degradation") or []) + list(
        data.get("degradation") or []
    )
    merged.setdefault("profile", profile)
    return merged


def load_account_state() -> dict:
    """Return ``{tool_name: account_state}`` from bindings/account_state.yaml."""
    data = _read_yaml(bindings_dir() / "account_state.yaml")
    providers = data.get("providers") or {}
    return providers if isinstance(providers, dict) else {}


def load_catalog() -> dict:
    """Aggregate the base capability catalog and provider fragments.

    ``bindings/capability_catalog.yaml`` plus every ``bindings/catalog/*.yaml``
    (independent provider fragments -- no shared-file editing).
    """
    root = bindings_dir()
    entries: list[dict] = []
    base = _read_yaml(root / "capability_catalog.yaml")
    entries.extend(base.get("entries") or [])
    fragment_dir = root / "catalog"
    if fragment_dir.is_dir():
        for path in sorted(fragment_dir.glob("*.yaml")):
            fragment = _read_yaml(path)
            entries.extend(fragment.get("entries") or [])
    return {"version": str(base.get("version") or "1.0"), "entries": entries}


def catalog_entry(model: Optional[str], operation: Optional[str]) -> Optional[dict]:
    """Look up one catalog entry by model+operation, or None."""
    if not model:
        return None
    for entry in load_catalog()["entries"]:
        if entry.get("model") == model and entry.get("operation") == operation:
            return entry
    return None


def slot_definition(slot: str) -> dict:
    """Return the declaration for ``slot`` or raise :class:`UnknownSlotError`."""
    slots = load_slots()
    if slot not in slots:
        raise UnknownSlotError(
            f"unknown capability slot {slot!r}; declared: {', '.join(sorted(slots))}"
        )
    return dict(slots[slot] or {})


# ---------------------------------------------------------------------------
# Tool registry seam
# ---------------------------------------------------------------------------


def tool_state(name: str) -> dict:
    """Return the registry view of a tool.

    This is the only place the module touches the live tool registry. Tests
    monkeypatch this seam; production reads it read-only.

    Shape::

        {"registered": bool, "status": "available"|"unavailable"|"degraded"|"missing",
         "supports": {...}, "capabilities": [...], "module_path": str,
         "install_instructions": str, "error": str|None}
    """
    from tools.tool_registry import registry

    try:
        registry.ensure_discovered()
        tool = registry.get(name)
    except Exception as exc:  # registry import/discovery failure is reported, not raised
        return {
            "registered": False,
            "status": "missing",
            "supports": {},
            "capabilities": [],
            "module_path": None,
            "install_instructions": "",
            "error": f"tool registry unavailable: {exc}",
        }
    if tool is None:
        return {
            "registered": False,
            "status": "missing",
            "supports": {},
            "capabilities": [],
            "module_path": None,
            "install_instructions": "",
            "error": None,
        }
    install_instructions = str(getattr(tool, "install_instructions", "") or "")
    try:
        status = tool.get_status().value
    except Exception as exc:
        return {
            "registered": True,
            "status": "unavailable",
            "supports": dict(getattr(tool, "supports", {}) or {}),
            "capabilities": list(getattr(tool, "capabilities", []) or []),
            "module_path": getattr(tool.__class__, "__module__", None),
            "install_instructions": install_instructions,
            "error": f"status check failed: {exc}",
        }
    return {
        "registered": True,
        "status": status,
        "supports": dict(getattr(tool, "supports", {}) or {}),
        "capabilities": list(getattr(tool, "capabilities", []) or []),
        "module_path": getattr(tool.__class__, "__module__", None),
        "install_instructions": install_instructions,
        "error": None,
    }


def normalize_legacy_supports(raw: dict) -> dict:
    """Map legacy ``BaseTool.supports`` keys onto canonical capability fields.

    Unknown-shaped flags are dropped rather than turned into ``True``: e.g.
    ``{"resolution": True}`` says a resolution exists but not which ones, so it
    must not satisfy an ``output_resolutions`` requirement.
    """
    normalized: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        canonical = _LEGACY_ALIASES.get(key, key)
        if canonical not in CAPABILITY_FIELDS:
            continue
        if key in ("aspect_ratio", "resolution"):
            if isinstance(value, list):
                normalized[canonical] = [str(v) for v in value]
            continue
        if key == "aspect_ratios":
            if isinstance(value, list):
                normalized[canonical] = [str(v) for v in value]
            continue
        if isinstance(value, bool) and canonical in (
            "reference_images_max",
            "min_duration_s",
            "max_duration_s",
        ):
            continue
        normalized[canonical] = value
    return normalized


def _binding_capabilities(candidate: dict, operation: Optional[str]) -> tuple[dict, Optional[dict]]:
    """Merge catalog entry, binding overrides and normalized tool supports.

    Precedence: catalog (per model x operation) < binding capabilities (the
    profile's deliberate statement about this exact binding).
    """
    caps: dict[str, Any] = {}
    model = candidate.get("catalog_model")
    entry = catalog_entry(model, operation)
    if entry:
        caps.update({k: v for k, v in entry.items() if k not in ("model", "operation")})
    overrides = candidate.get("capabilities") or {}
    if isinstance(overrides, dict):
        caps.update(overrides)
    state = tool_state(str(candidate.get("tool") or ""))
    for key, value in normalize_legacy_supports(state.get("supports") or {}).items():
        caps.setdefault(key, value)
    operations = list(candidate.get("operations") or state.get("capabilities") or [])
    caps["operations"] = operations
    return caps, entry


def _candidate_operation(candidate: dict, operation: Optional[str]) -> Optional[str]:
    if operation:
        return operation
    declared = candidate.get("operations") or []
    return str(declared[0]) if declared else None


def _candidate_usable(
    candidate: dict, account: dict, operation: Optional[str]
) -> tuple[bool, Optional[str], str]:
    """Return (usable, rejection_code, detail) for one binding candidate."""
    tool = str(candidate.get("tool") or "")
    if not tool:
        return False, "BINDING_INVALID", "binding has no tool"
    state = tool_state(tool)
    if not state.get("registered"):
        return False, "TOOL_NOT_REGISTERED", f"{tool} is not in the tool registry"
    account_state = account.get(tool) or {}
    if str(account_state.get("status") or "").upper() == "EXHAUSTED":
        return False, "CREDITS_EXHAUSTED", f"{tool} credits/quota exhausted"
    status = str(state.get("status") or "unavailable")
    if status == "unavailable":
        return False, "CREDENTIALS_MISSING", f"{tool} is unavailable (missing credential/dependency)"
    if status not in ("available", "degraded"):
        return False, "TOOL_UNAVAILABLE", f"{tool} status is {status}"
    if operation:
        declared = list(candidate.get("operations") or [])
        tool_caps = list(state.get("capabilities") or [])
        if operation not in declared and operation not in tool_caps:
            return False, "OPERATION_UNSUPPORTED", f"{tool} does not declare {operation}"
    return True, None, ""


# ---------------------------------------------------------------------------
# Frozen interface
# ---------------------------------------------------------------------------


def resolve(slot: str, profile: str, *, operation=None) -> str:
    """Resolve ``slot`` to a concrete tool name through ``profile``.

    Walks the profile's priority chain and returns the first candidate whose
    tool is registered, whose account is not exhausted, and that declares
    ``operation`` when one is given.

    Raises :class:`UnknownSlotError`, :class:`UnknownProfileError`, or
    :class:`SlotUnavailable` with per-candidate reasons.
    """
    if not profile:
        profile = active_profile()
    profile_data = load_profile(profile)
    slot_definition(slot)  # validate the slot name even if the profile skips it

    candidates = _slot_chain(profile_data, slot)
    if not candidates:
        raise SlotUnavailable(
            slot,
            profile,
            [{"tool": None, "code": "SLOT_UNBOUND", "detail": "profile binds no candidate"}],
        )

    account = load_account_state()
    reasons: list[dict[str, Any]] = []
    for candidate in candidates:
        usable, code, detail = _candidate_usable(candidate, account, operation)
        if usable:
            return str(candidate["tool"])
        reasons.append({"tool": candidate.get("tool"), "code": code, "detail": detail})
    raise SlotUnavailable(slot, profile, reasons)


def supports(slot: str, profile: str) -> dict:
    """Return normalized capabilities of the slot's best binding.

    Does not raise when the binding is unusable: the declaration is still
    useful to callers and carries ``available: false``. An unbound slot does
    raise :class:`SlotUnavailable` -- there is nothing to describe.
    """
    if not profile:
        profile = active_profile()
    profile_data = load_profile(profile)
    slot_def = slot_definition(slot)

    candidates = _slot_chain(profile_data, slot)
    if not candidates:
        raise SlotUnavailable(
            slot,
            profile,
            [{"tool": None, "code": "SLOT_UNBOUND", "detail": "profile binds no candidate"}],
        )

    account = load_account_state()
    chosen: Optional[dict] = None
    chosen_available = False
    for candidate in candidates:
        usable, _code, _detail = _candidate_usable(candidate, account, None)
        if chosen is None:
            chosen = candidate
        if usable:
            chosen = candidate
            chosen_available = True
            break
    assert chosen is not None

    required_operation = _first_required_operation(_slot_default_requirements(slot_def))
    operation = _candidate_operation(chosen, required_operation)
    caps, entry = _binding_capabilities(chosen, operation)
    result: dict[str, Any] = {
        "slot": slot,
        "profile": profile,
        "tool": chosen.get("tool"),
        "provider": chosen.get("provider"),
        "model": chosen.get("catalog_model"),
        "tier": chosen.get("tier"),
        "operations": list(caps.get("operations") or []),
        "available": chosen_available,
        "verification_status": (entry or {}).get("verification_status", _UNVERIFIED),
        "source": (entry or {}).get("source"),
        "verified_at": (entry or {}).get("verified_at"),
        "commercial_rights": (entry or {}).get("commercial_rights"),
        "required_capabilities": slot_def.get("required_capabilities") or {},
    }
    for field in CAPABILITY_FIELDS:
        result[field] = caps.get(field, "unknown")
    return result


def plan_degradation(slots: list, profile: str, requirements: dict) -> list[dict]:
    """Plan how each slot reaches its requirements, using declared plans only.

    ``requirements`` is either a flat dict applied to every slot or a
    ``{slot: requirements}`` mapping. Recognized keys::

        operations                 list of required operations
        output_resolutions         list of required resolutions
        capabilities               {capability: required_value}
        <capability>: <value>      shorthand for one capability

    Each returned entry::

        {slot, status: OK|DEGRADED|BLOCKED, plan_id, lost, added, path,
         approval_required, reason}

    A DEGRADED entry always points at a ``path`` predeclared in the profile's
    ``degradation:`` block; the planner never synthesizes sub-stages. When no
    declared plan matches an unmet requirement the slot is BLOCKED.
    """
    if not profile:
        profile = active_profile()
    profile_data = load_profile(profile)
    slot_list = list(slots or [])
    for slot in slot_list:
        slot_definition(slot)  # fail fast on unknown slot names

    account = load_account_state()
    results: list[dict] = []
    for slot in slot_list:
        reqs = _merge_requirements(
            _slot_default_requirements(slot_definition(slot)),
            _requirements_for(slot, requirements),
        )
        chain = _slot_chain(profile_data, slot)

        chosen: Optional[dict] = None
        rejection: Optional[dict[str, Any]] = None
        fallback_index = 0
        for index, candidate in enumerate(chain):
            usable, code, detail = _candidate_usable(
                candidate, account, _first_required_operation(reqs)
            )
            if usable:
                chosen = candidate
                fallback_index = index
                break
            if rejection is None:
                rejection = {"tool": candidate.get("tool"), "code": code, "detail": detail}

        if chosen is None:
            if not chain:
                rejection = {"tool": None, "code": "SLOT_UNBOUND", "detail": "no candidates"}
            info = {"usable": False, "chain_degraded": False}
            unmet = _Unmet(capabilities=set(), operations=set(), resolutions=set())
            plan = _select_plan(profile_data, slot, unmet, info)
            if plan is not None:
                results.append(_plan_result(slot, plan, unmet, info))
            else:
                results.append(
                    {
                        "slot": slot,
                        "status": "BLOCKED",
                        "plan_id": None,
                        "lost": _unmet_list(unmet),
                        "added": 0,
                        "path": [],
                        "approval_required": False,
                        "reason": f"no predeclared degradation path ({rejection['code']})",
                        "rejection": rejection,
                    }
                )
            continue

        operation = _first_required_operation(reqs) or _candidate_operation(chosen, None)
        caps, _entry = _binding_capabilities(chosen, operation)
        unmet = _unmet_requirements(caps, reqs)
        info = {"usable": True, "chain_degraded": fallback_index > 0}

        if not _unmet_list(unmet):
            results.append(
                {
                    "slot": slot,
                    "status": "OK",
                    "plan_id": None,
                    "lost": [],
                    "added": 0,
                    "path": [],
                    "approval_required": False,
                    "reason": "",
                    "binding": chosen.get("tool"),
                    "chain_degraded": info["chain_degraded"],
                }
            )
            continue

        # Requirements that are met still degrade if the chain fell back to a
        # lower-priority binding. Unmet requirements are planned on their own
        # so a capability plan outranks the generic chain-fallback plan.
        plan = _select_plan(profile_data, slot, unmet, info)
        if plan is not None:
            results.append(_plan_result(slot, plan, unmet, info))
        elif info["chain_degraded"]:
            first = chain[0]
            results.append(
                {
                    "slot": slot,
                    "status": "DEGRADED",
                    "plan_id": None,
                    "lost": sorted({str(first.get("tier") or "primary")}),
                    "added": 0,
                    "path": [{"stage": slot, "binding": chosen.get("tool")}],
                    "approval_required": True,
                    "reason": "declared fallback chain used; no degradation plan attached",
                    "binding": chosen.get("tool"),
                    "chain_degraded": True,
                }
            )
        else:
            results.append(
                {
                    "slot": slot,
                    "status": "BLOCKED",
                    "plan_id": None,
                    "lost": _unmet_list(unmet),
                    "added": 0,
                    "path": [],
                    "approval_required": False,
                    "reason": "requirements unmet and no predeclared degradation path matches",
                    "binding": chosen.get("tool"),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Cost comparison and dual-tier binding (MOD-08)
# ---------------------------------------------------------------------------
#
# Two tiers, one comparison:
#
# * **draft** -- among candidates that are usable and satisfy the requirements
#   (capabilities *and* output resolutions, MOD-08/A2), choose the lowest
#   estimated cash cost. Ties keep the profile's declared chain order.
# * **delivery** -- never auto-selected. The plan lists every candidate with
#   its quality/verification, cost estimate (or "unknown"), quota and rights,
#   and requires an explicit operator confirmation before a switch is recorded.
#   Nothing forces the most expensive (premium) candidate.
#
# Prices provider fragments cannot carry (the capability-catalog schema has
# ``additionalProperties: false``) live in ``_MODEL_COSTS`` below with their
# evidence. A binding may always override with ``cost_cny_per_s`` /
# ``cost_cny_per_run`` / ``cost_unknown`` / ``quota``.
#
# M0: nothing here calls a provider. Unknown costs are reported as unknown --
# never guessed as 0, because "free" that is not free is exactly the cost bug
# this layer exists to prevent.

DRAFT_TIER = "draft"
DELIVERY_TIER = "delivery"
_TIERS = (DRAFT_TIER, DELIVERY_TIER)

_RESOLUTION_RANK = {"480p": 1, "720p": 2, "1080p": 3, "1440p": 4, "4k": 5}

#: Verification status -> rough quality weight (0..1). Used only to order the
#: delivery shortlist; it is not a quality promise.
_VERIFICATION_QUALITY = {
    "VERIFIED": 1.0,
    "MOCKED": 0.6,
    "UNVERIFIED": 0.4,
    "BLOCKED": 0.0,
}

#: Known cash prices by catalog model. ``cash_cny_per_s`` is multiplied by
#: ``duration_s``; ``cash_cny_per_run`` is a flat per-generation price.
_MODEL_COSTS: dict[str, dict[str, Any]] = {
    "molica/wan30video": {
        "cash_cny_per_run": 0.0,
        "basis": "prepaid membership credits: 105 credits per 5s 480p run, "
                 "measured in the account ledger 2026-11-07 "
                 "(tools/video/molica_video.py _MEASURED_MIN_CREDITS); no cash spend",
        "quota": {"unit": "credits", "per_run": 105, "source": "account ledger 2026-11-07"},
    },
    "holopix/seedance-2-0": {
        "cash_cny_per_run": 0.0,
        "basis": "membership compute points; no cash spend (account exhausted 2026-11)",
        "quota": {"unit": "compute_points", "per_run": None, "source": "holopix membership"},
    },
    "holopix/seedance-mini": {
        "cash_cny_per_run": 0.0,
        "basis": "membership compute points; no cash spend",
        "quota": {"unit": "compute_points", "per_run": None, "source": "holopix membership"},
    },
    "zhipu/cogvideox-flash": {
        "cash_cny_per_run": 0.0,
        "basis": "published free tier: CNY 0 per generation (tools/video/zhipu_video.py _MODEL_PRICE_RMB)",
        "quota": {"unit": "generations", "per_run": None, "source": "free tier, no total cap"},
    },
    "zhipu/cogvideox-2": {
        "cash_cny_per_run": 0.5,
        "basis": "published list price CNY 0.5 per generation (tools/video/zhipu_video.py)",
    },
    "zhipu/cogvideox-3": {
        "cash_cny_per_run": 1.0,
        "basis": "published list price CNY 1.0 per generation (tools/video/zhipu_video.py)",
    },
    "agnes/agnes-video-2.5-flash": {
        "cash_cny_per_s": 0.0,
        "basis": "free trial: $0/second while the trial runs (tools/video/agnes_video.py); "
                 "price is volatile and must be re-measured before delivery",
    },
    "dashscope/wan3.0-video": {
        "cash_cny_per_s": 0.30,
        "basis": "Bailian price sheet 2026-11-11, 480P tier (requirements §13.1)",
    },
}


class TierApprovalRequired(CapabilitySlotsError):
    """A tier switch was requested without explicit operator confirmation."""


def _unknown_cost(why: str) -> dict[str, Any]:
    return {
        "estimated_cny": None,
        "cost_known": False,
        "cost_basis": f"unknown: {why}",
        "cost_currency": "CNY",
        "quota": None,
    }


def _candidate_cost(candidate: dict, duration_s: float) -> dict[str, Any]:
    """Estimate one binding's cash cost for a single clip/run.

    Returns ``estimated_cny`` (float or None), ``cost_known``, a human
    ``cost_basis`` and the quota declaration. Unknown stays unknown.
    """
    seconds = max(float(duration_s or 0.0), 0.0)
    quota = candidate.get("quota")
    if candidate.get("cost_cny_per_s") is not None:
        rate = float(candidate["cost_cny_per_s"])
        return {
            "estimated_cny": round(rate * seconds, 4),
            "cost_known": True,
            "cost_basis": f"{rate:g} CNY/output-second x {seconds:g}s (binding declaration)",
            "cost_currency": "CNY",
            "quota": quota,
        }
    if candidate.get("cost_cny_per_run") is not None:
        value = float(candidate["cost_cny_per_run"])
        return {
            "estimated_cny": round(value, 4),
            "cost_known": True,
            "cost_basis": f"{value:g} CNY/run (binding declaration)",
            "cost_currency": "CNY",
            "quota": quota,
        }
    if candidate.get("cost_unknown"):
        return _unknown_cost("binding marks the price as unknown")

    spec = _MODEL_COSTS.get(str(candidate.get("catalog_model") or ""))
    if isinstance(spec, dict):
        basis = str(spec.get("basis") or "provider price")
        if spec.get("cash_cny_per_s") is not None:
            rate = float(spec["cash_cny_per_s"])
            return {
                "estimated_cny": round(rate * seconds, 4),
                "cost_known": True,
                "cost_basis": f"{rate:g} CNY/output-second x {seconds:g}s ({basis})",
                "cost_currency": "CNY",
                "quota": quota or spec.get("quota"),
            }
        value = float(spec.get("cash_cny_per_run") or 0.0)
        return {
            "estimated_cny": round(value, 4),
            "cost_known": True,
            "cost_basis": f"{value:g} CNY/run ({basis})",
            "cost_currency": "CNY",
            "quota": quota or spec.get("quota"),
        }

    tier = str(candidate.get("tier") or "")
    if tier in ("free", "credit", "local"):
        return {
            "estimated_cny": 0.0,
            "cost_known": True,
            "cost_basis": f"declared '{tier}' tier: no cash cost (price not published)",
            "cost_currency": "CNY",
            "quota": quota,
        }
    return _unknown_cost(
        "no binding price, no known model price, and the provider publishes no tariff"
    )


def _max_resolution(caps: dict) -> str:
    resolutions = caps.get("output_resolutions")
    if not isinstance(resolutions, list) or not resolutions:
        return "unknown"
    ranked = [r for r in resolutions if r in _RESOLUTION_RANK]
    if not ranked:
        return str(sorted(map(str, resolutions))[-1])
    return max(ranked, key=lambda r: _RESOLUTION_RANK[r])


def _quality_score(row: dict) -> float:
    verification = str(row.get("verification_status") or _UNVERIFIED)
    base = _VERIFICATION_QUALITY.get(verification, 0.35)
    rank = _RESOLUTION_RANK.get(str(row.get("max_resolution") or ""), 0)
    return round(min(1.0, base * 0.7 + min(rank, 3) / 3.0 * 0.3), 3)


def _comparison_rows(
    slot: str,
    profile_data: dict,
    *,
    operation: Optional[str],
    duration_s: float,
    requirements: dict,
) -> list[dict]:
    """One comparison row per declared candidate, in chain order."""
    account = load_account_state()
    rows: list[dict] = []
    for index, candidate in enumerate(_slot_chain(profile_data, slot)):
        tool = str(candidate.get("tool") or "")
        usable, code, detail = _candidate_usable(candidate, account, operation)
        candidate_operation = _candidate_operation(candidate, operation)
        caps, entry = _binding_capabilities(candidate, candidate_operation)
        unmet = _unmet_requirements(caps, requirements)
        cost = _candidate_cost(candidate, duration_s)
        max_res = _max_resolution(caps)
        rows.append(
            {
                "slot": slot,
                "tool": tool,
                "provider": candidate.get("provider"),
                "model": candidate.get("catalog_model"),
                # Plain provider model name declared by the binding; distinct
                # from the catalog id ("provider/model") that providers reject
                # when it is passed as their model parameter.
                "tool_model": candidate.get("model"),
                "fallback_models": candidate.get("fallback_models"),
                "tier": candidate.get("tier"),
                "chain_index": index,
                # Candidates marked ``primary_eligible: false`` (membership
                # gateways with unverified ToS, R25) stay visible in the
                # comparison but can never be auto-selected as the draft tier.
                "primary_eligible": bool(candidate.get("primary_eligible", True)),
                "usable": usable,
                "rejection": None if usable else {
                    "code": code,
                    "detail": detail,
                },
                "meets_requirements": not _unmet_list(unmet),
                "unmet": _unmet_list(unmet),
                "estimated_cny": cost["estimated_cny"],
                "cost_known": cost["cost_known"],
                "cost_basis": cost["cost_basis"],
                "cost_currency": cost["cost_currency"],
                "quota": cost.get("quota"),
                "rights": (entry or {}).get("commercial_rights")
                or candidate.get("commercial_rights")
                or "unknown",
                "verification_status": (entry or {}).get("verification_status", _UNVERIFIED),
                "max_resolution": max_res,
                "output_resolutions": caps.get("output_resolutions", "unknown"),
            }
        )
    return rows


def cost_comparison(
    slot: str,
    profile: str,
    *,
    operation: Optional[str] = None,
    duration_s: float = 5.0,
    requirements: Optional[dict] = None,
) -> list[dict]:
    """Return the documented comparison basis for every candidate in ``slot``.

    Each row carries the cost estimate (or an explicit ``unknown``), quota,
    rights, verification status and whether it meets the requirements, so
    doctor/plan output can show *why* a binding was chosen (MOD-08).
    """
    if not profile:
        profile = active_profile()
    profile_data = load_profile(profile)
    slot_def = slot_definition(slot)
    reqs = _merge_requirements(_slot_default_requirements(slot_def), requirements or {})
    op = operation or _first_required_operation(reqs)
    return _comparison_rows(
        slot, profile_data, operation=op, duration_s=duration_s, requirements=reqs
    )


def _delivery_resolution(profile_data: dict) -> Optional[str]:
    """Derive a delivery resolution tier from ``delivery.target_resolution``."""
    delivery = profile_data.get("delivery") or {}
    if not isinstance(delivery, dict):
        return None
    target = str(delivery.get("target_resolution") or "")
    match = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", target)
    if not match:
        return None
    short_side = min(int(match.group(1)), int(match.group(2)))
    if short_side >= 2160:
        return "4k"
    if short_side >= 1080:
        return "1080p"
    if short_side >= 720:
        return "720p"
    if short_side >= 480:
        return "480p"
    return None


def _draft_order(row: dict) -> tuple:
    """Cheapest first; unknown prices cannot be proven cheapest, so go last."""
    return (
        0 if row["cost_known"] else 1,
        float(row["estimated_cny"]) if row["cost_known"] else 0.0,
        row["chain_index"],
    )


def _delivery_order(row: dict) -> tuple:
    """Highest quality first, then cheapest known cost, then chain order."""
    return (
        -_quality_score(row),
        0 if row["cost_known"] else 1,
        float(row["estimated_cny"]) if row["cost_known"] else 0.0,
        row["chain_index"],
    )


def _tier_choice(row: Optional[dict]) -> dict:
    if row is None:
        return {}
    return {
        "tool": row["tool"],
        "provider": row["provider"],
        "model": row["model"],
        "tier": row["tier"],
        "estimated_cny": row["estimated_cny"],
        "cost_known": row["cost_known"],
        "cost_basis": row["cost_basis"],
        "quota": row["quota"],
        "rights": row["rights"],
        "verification_status": row["verification_status"],
        "max_resolution": row["max_resolution"],
        "quality_score": _quality_score(row),
    }


def plan_tiers(
    slot: str,
    profile: str,
    *,
    operation: Optional[str] = None,
    duration_s: float = 5.0,
    requirements: Optional[dict] = None,
    delivery_requirements: Optional[dict] = None,
) -> dict:
    """Plan the draft and delivery tiers for one slot (MOD-08).

    Draft picks the lowest estimated cash cost among usable candidates that
    satisfy the requirements. Delivery never auto-selects: it returns the
    comparison and ``PENDING_APPROVAL`` until :func:`confirm_tier` records an
    operator decision. Lower-resolution delivery falls back to the profile's
    predeclared upscale plan, or BLOCKED when none is declared.
    """
    if not profile:
        profile = active_profile()
    profile_data = load_profile(profile)
    slot_def = slot_definition(slot)

    draft_reqs = _merge_requirements(_slot_default_requirements(slot_def), requirements or {})
    op = operation or _first_required_operation(draft_reqs)
    rows = _comparison_rows(
        slot, profile_data, operation=op, duration_s=duration_s, requirements=draft_reqs
    )
    eligible = [
        row for row in rows
        if row["usable"] and row["meets_requirements"] and row["primary_eligible"]
    ]
    eligible.sort(key=_draft_order)
    draft_choice = eligible[0] if eligible else None

    draft = {
        "tier": DRAFT_TIER,
        "requirements": draft_reqs,
        "status": "OK" if draft_choice else "BLOCKED",
        "approval_required": False,
        "candidates": eligible,
        "reason": "" if draft_choice else (
            "no usable primary-eligible candidate satisfies the draft requirements"
        ),
    }
    draft.update(_tier_choice(draft_choice))

    delivery_reqs = _merge_requirements(draft_reqs, delivery_requirements or {})
    target = _delivery_resolution(profile_data)
    if target and not (delivery_requirements or {}).get("output_resolutions"):
        delivery_reqs["output_resolutions"] = sorted(
            set(list(delivery_reqs.get("output_resolutions") or []) + [target]),
            key=lambda r: _RESOLUTION_RANK.get(r, 0),
        )
    delivery_rows = _comparison_rows(
        slot, profile_data, operation=op, duration_s=duration_s, requirements=delivery_reqs
    )
    native = [row for row in delivery_rows if row["usable"] and row["meets_requirements"]]
    native.sort(key=_delivery_order)

    delivery_data = profile_data.get("delivery") or {}
    if not isinstance(delivery_data, dict):
        delivery_data = {}
    upscale_plan_id = delivery_data.get("degradation_id")

    upscale_required = False
    delivery_candidates = native
    delivery_status = "PENDING_APPROVAL" if native else "BLOCKED"
    delivery_reason = (
        f"delivery requirements include {target or 'the declared target resolution'}; "
        "operator confirmation required (quality / cost / quota / rights)"
    )
    if not native:
        fallback = [row for row in rows if row["usable"] and row["meets_requirements"]]
        fallback.sort(key=_delivery_order)
        if fallback and upscale_plan_id:
            upscale_required = True
            delivery_candidates = fallback
            delivery_status = "PENDING_APPROVAL"
            delivery_reason = (
                f"no candidate renders {target or 'the delivery resolution'} natively; "
                f"declared upscale plan {upscale_plan_id!r} required (DEGRADED)"
            )
        elif fallback:
            delivery_candidates = fallback
            delivery_status = "BLOCKED"
            delivery_reason = (
                f"no candidate renders {target or 'the delivery resolution'} natively and "
                "no predeclared upscale plan exists"
            )
        else:
            delivery_candidates = []
            delivery_reason = "no usable candidate satisfies the slot requirements"

    suggested = delivery_candidates[0] if delivery_candidates else None
    delivery = {
        "tier": DELIVERY_TIER,
        "requirements": delivery_reqs,
        "status": delivery_status,
        "selected": None,
        "suggested": _tier_choice(suggested) or None,
        "candidates": delivery_candidates,
        "approval_required": True,
        "requires_confirmation": True,
        "upscale_required": upscale_required,
        "upscale_plan_id": upscale_plan_id if upscale_required else None,
        "upscale_cost": (
            _unknown_cost(
                "the local upscale stage declares no price; compute cost is not "
                "estimated in M0 (DEL-01/F34)"
            )
            if upscale_required
            else None
        ),
        "comparison_basis": [
            "quality (verification status + max declared resolution)",
            "estimated cash cost (unknown is never treated as free)",
            "quota (credits / compute points per run)",
            "commercial rights (UNKNOWN blocks delivery)",
        ],
        "reason": delivery_reason,
    }

    return {
        "slot": slot,
        "profile": profile,
        "operation": op,
        "duration_s": float(duration_s),
        "currency": "CNY",
        "draft": draft,
        "delivery": delivery,
        "approval_required": True,
        "cost_comparison": rows,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decision_log_path(path: Optional[str] = None) -> Path:
    return Path(path) if path else bindings_dir() / "decision_log.json"


def load_tier_approvals(decision_log_path: Optional[str] = None) -> list[dict]:
    """Return recorded draft/delivery tier switches from the bindings log."""
    path = _decision_log_path(decision_log_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    decisions = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, list):
        return []
    return [
        entry
        for entry in decisions
        if isinstance(entry, dict)
        and str(entry.get("subject") or "").startswith(
            (f"{DRAFT_TIER}_tier:", f"{DELIVERY_TIER}_tier:")
        )
    ]


def confirm_tier(
    slot: str,
    profile: str,
    tier: str,
    *,
    confirmed: bool,
    tool: Optional[str] = None,
    approved_by: str = "owner",
    reason: str = "",
    duration_s: float = 5.0,
    operation: Optional[str] = None,
    requirements: Optional[dict] = None,
    delivery_requirements: Optional[dict] = None,
    decision_log_path: Optional[str] = None,
) -> dict:
    """Record an explicit tier switch in the bindings decision log.

    ``confirmed`` must be ``True``; a missing confirmation raises
    :class:`TierApprovalRequired` and writes nothing. The record is appended
    (never mutated) so every switch -- including switching back -- stays
    auditable (MOD-08).
    """
    if tier not in _TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {_TIERS}")
    plan = plan_tiers(
        slot,
        profile,
        operation=operation,
        duration_s=duration_s,
        requirements=requirements,
        delivery_requirements=delivery_requirements,
    )
    section = plan[tier]
    if not confirmed:
        raise TierApprovalRequired(
            f"{tier} tier selection for slot {slot!r} in profile {profile!r} "
            "needs explicit operator confirmation"
        )
    if section.get("status") == "BLOCKED":
        raise CapabilitySlotsError(
            f"cannot confirm {tier} tier for slot {slot!r}: {section.get('reason')}"
        )

    candidates = list(section.get("candidates") or [])
    if not candidates:
        raise CapabilitySlotsError(
            f"no eligible {tier} candidate for slot {slot!r} in profile {profile!r}"
        )
    default_tool = (section.get("suggested") or section).get("tool")
    chosen_tool = tool or default_tool
    chosen = next((row for row in candidates if row["tool"] == chosen_tool), None)
    if chosen is None:
        raise CapabilitySlotsError(
            f"{chosen_tool!r} is not an eligible {tier} candidate for slot {slot!r}; "
            f"eligible: {', '.join(str(row['tool']) for row in candidates)}"
        )

    options: list[dict] = []
    for row in candidates:
        options.append(
            {
                "option_id": str(row["tool"]),
                "label": f"{row['provider'] or 'unknown'}/{row['model'] or row['tool']}",
                "score": _quality_score(row),
                "reason": (
                    f"{row['cost_basis']}; quota={row['quota'] or 'unknown'}; "
                    f"rights={row['rights']}"
                ),
                "rejected_because": "" if row["tool"] == chosen_tool else "not selected",
            }
        )

    decision_id = f"dec_tier_{uuid.uuid4().hex[:12]}"
    entry = {
        "decision_id": decision_id,
        "stage": "assembly",
        "category": "budget_tradeoff",
        "subject": f"{tier}_tier:{slot}",
        "options_considered": options,
        "selected": chosen_tool,
        "chosen": chosen_tool,
        "reason": reason or (
            f"{tier} tier: {chosen['cost_basis']}; rights={chosen['rights']}"
        ),
        "user_visible": True,
        "user_approved": True,
        "estimated_cny": chosen["estimated_cny"],
        "cost_known": chosen["cost_known"],
        "upscale_required": bool(section.get("upscale_required")),
        "profile": plan["profile"],
        "decided_at": _now_iso(),
        "decided_by": approved_by,
    }

    path = _decision_log_path(decision_log_path)
    data: dict[str, Any] = {"version": "1.0", "decisions": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CapabilitySlotsError(f"{path} is not valid JSON: {exc}") from exc
        if isinstance(loaded, dict):
            data = loaded
    data.setdefault("version", "1.0")
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
    decisions.append(entry)
    data["decisions"] = decisions
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "decision_id": decision_id,
        "slot": slot,
        "profile": plan["profile"],
        "tier": tier,
        "tool": chosen_tool,
        "estimated_cny": chosen["estimated_cny"],
        "cost_known": chosen["cost_known"],
        "cost_basis": chosen["cost_basis"],
        "upscale_required": bool(section.get("upscale_required")),
        "decision_log": str(path),
    }


def doctor(profile: str) -> dict:
    """Health-check a profile: GREEN / DEGRADED / BLOCKED with reasons.

    Returns::

        {"status": "GREEN"|"DEGRADED"|"BLOCKED",
         "profile": str,
         "reasons": [{code, slot, severity, message, tool?}],
         "degradation": [planned non-OK paths and declared delivery degradations]}
    """
    if not profile:
        profile = active_profile()
    reasons: list[dict[str, Any]] = []

    def add(code: str, message: str, *, slot: Optional[str] = None,
            severity: str = "warning", tool: Optional[str] = None) -> None:
        reasons.append(
            {"code": code, "slot": slot, "severity": severity,
             "message": message, "tool": tool}
        )

    try:
        profile_data = load_profile(profile)
    except UnknownProfileError as exc:
        return {
            "status": "BLOCKED",
            "profile": profile,
            "reasons": [
                {"code": "UNKNOWN_PROFILE", "slot": None, "severity": "blocker",
                 "message": str(exc), "tool": None}
            ],
            "degradation": [],
            "cost_comparison": [],
        }

    slots = load_slots()
    if not slots:
        return {
            "status": "BLOCKED",
            "profile": profile,
            "reasons": [
                {"code": "BINDINGS_MISSING", "slot": None, "severity": "blocker",
                 "message": f"no slots.yaml under {bindings_dir()}", "tool": None}
            ],
            "degradation": [],
            "cost_comparison": [],
        }

    account = load_account_state()
    degradation: list[dict] = []

    for slot, slot_def in slots.items():
        if not isinstance(slot_def, dict) or not slot_def.get("required"):
            continue
        chain = _slot_chain(profile_data, slot)
        if not chain:
            add(
                "SLOT_UNBOUND",
                f"required slot {slot!r} has no binding in profile {profile!r}",
                slot=slot,
                severity="blocker",
            )
            unmet = _Unmet(set(), set(), set())
            plan = _select_plan(profile_data, slot, unmet, {"usable": False})
            if plan is not None:
                degradation.append(_plan_result(slot, plan, unmet, {"usable": False}))
            continue

        chosen: Optional[dict] = None
        fallback_index = 0
        rejections: list[dict[str, Any]] = []
        for index, candidate in enumerate(chain):
            usable, code, detail = _candidate_usable(candidate, account, None)
            if usable:
                chosen = candidate
                fallback_index = index
                break
            rejections.append({"tool": candidate.get("tool"), "code": code, "detail": detail})

        if chosen is None:
            unmet = _Unmet(set(), set(), set())
            info = {"usable": False, "chain_degraded": False}
            plan = _select_plan(profile_data, slot, unmet, info)
            # A predeclared plan turns "no usable candidate" into DEGRADED;
            # without one it is a hard blocker. The rejections stay visible as
            # warnings so the operator still sees why the native binding failed.
            severity = "warning" if plan is not None else "blocker"
            for rejection in rejections:
                add(
                    str(rejection["code"]),
                    str(rejection["detail"]),
                    slot=slot,
                    severity=severity,
                    tool=str(rejection["tool"]) if rejection.get("tool") else None,
                )
                # Never leave a blocked slot without an actionable setup hint:
                # the tool owns the install/credential instructions.
                if rejection["code"] in (
                    "CREDENTIALS_MISSING",
                    "TOOL_UNAVAILABLE",
                ) and rejection.get("tool"):
                    hint = str(
                        tool_state(str(rejection["tool"])).get("install_instructions")
                        or ""
                    ).strip()
                    if hint:
                        add(
                            "SETUP_REQUIRED",
                            f"{slot}: {rejection['tool']} setup: "
                            + " ".join(hint.split()),
                            slot=slot,
                            severity=severity,
                            tool=str(rejection["tool"]),
                        )
            if plan is not None:
                degrading = _plan_result(slot, plan, unmet, info)
                degradation.append(degrading)
                add(
                    "DEGRADATION_DECLARED",
                    f"{slot}: {plan.get('note') or 'declared degradation path'}",
                    slot=slot,
                )
            continue

        # Fallback through the declared priority chain.
        if fallback_index > 0:
            first = chain[0]
            add(
                "PRIMARY_UNAVAILABLE_FALLBACK",
                f"{slot}: primary {first.get('tool')} unusable; using {chosen.get('tool')}",
                slot=slot,
                tool=str(chosen.get("tool")),
            )
            plan = _select_plan(
                profile_data,
                slot,
                _Unmet(set(), set(), set()),
                {"usable": True, "chain_degraded": True},
            )
            if plan is not None:
                degradation.append(
                    _plan_result(
                        slot,
                        plan,
                        _Unmet(set(), set(), set()),
                        {"usable": True, "chain_degraded": True},
                    )
                )
            else:
                degradation.append(
                    {
                        "slot": slot,
                        "status": "DEGRADED",
                        "plan_id": None,
                        "lost": sorted(
                            {str(first.get("tier") or "primary")}
                        ),
                        "added": 0,
                        "path": [{"stage": slot, "binding": chosen.get("tool")}],
                        "approval_required": True,
                        "reason": "declared fallback chain used; no degradation plan attached",
                    }
                )

        chosen_state = tool_state(str(chosen.get("tool")))
        if str(chosen_state.get("status")) == "degraded":
            add(
                "TOOL_DEGRADED",
                f"{chosen.get('tool')} reports degraded status",
                slot=slot,
                tool=str(chosen.get("tool")),
            )

        # Session-token providers must prove the session, not just the token.
        if chosen.get("session_auth"):
            account_state = account.get(str(chosen.get("tool"))) or {}
            if not account_state.get("session_verified"):
                add(
                    "SESSION_UNVERIFIED",
                    f"{chosen.get('tool')}: session token present but not verified (F15)",
                    slot=slot,
                    tool=str(chosen.get("tool")),
                )

        # Required capabilities and their verification status.
        default_reqs = _slot_default_requirements(slot_def)
        operation = _candidate_operation(chosen, _first_required_operation(default_reqs))
        caps, entry = _binding_capabilities(chosen, operation)
        unmet = _unmet_requirements(caps, default_reqs)
        if _unmet_list(unmet):
            info = {"usable": True, "chain_degraded": fallback_index > 0}
            plan = _select_plan(profile_data, slot, unmet, info)
            if plan is not None:
                degradation.append(_plan_result(slot, plan, unmet, info))
                add(
                    "DEGRADATION_DECLARED",
                    f"{slot}: {plan.get('note') or 'declared degradation path'}",
                    slot=slot,
                )
            else:
                add(
                    "CAPABILITY_UNMET",
                    f"{slot}: {', '.join(_unmet_list(unmet))} not satisfied and no "
                    "predeclared degradation path",
                    slot=slot,
                    severity="blocker",
                    tool=str(chosen.get("tool")),
                )
            continue

        if default_reqs.get("capabilities"):
            verification = str((entry or {}).get("verification_status") or _UNVERIFIED)
            if verification not in (_VERIFIED,):
                add(
                    "CAPABILITY_UNVERIFIED",
                    f"{slot}: {chosen.get('catalog_model')} declared {verification}, "
                    "not VERIFIED (unknown != supported)",
                    slot=slot,
                    tool=str(chosen.get("tool")),
                )

        # Declared draft -> delivery upscale path (DEL-01/F34).
    delivery = profile_data.get("delivery") or {}
    if isinstance(delivery, dict) and delivery.get("requires_upscale"):
        plan_id = delivery.get("degradation_id")
        plan = _find_plan(profile_data, None, plan_id) if plan_id else None
        degradation.append(
            {
                "slot": "video_primary",
                "status": "DEGRADED",
                "plan_id": plan_id,
                "lost": list((plan or {}).get("lost") or ["native_1080p"]),
                "added": _plan_added(plan),
                "path": list((plan or {}).get("path") or []),
                "approval_required": bool((plan or {}).get("approval_required", False)),
                "reason": (plan or {}).get("note")
                or "draft resolution below delivery target; upscale required",
                "declared": True,
            }
        )
        add(
            "DRAFT_UPSCALE_DECLARED",
            f"{profile}: draft {delivery.get('draft_resolution')} -> "
            f"{delivery.get('target_resolution')} requires upscale (DEGRADED)",
            slot="video_primary",
        )

    # MOD-08: doctor also reports the draft/delivery comparison basis --
    # estimates, quota, rights and explicit "unknown" prices -- without letting
    # cost information change the three-state health verdict.
    cost_rows: list[dict] = []
    for slot, slot_def in slots.items():
        if not isinstance(slot_def, dict) or not slot_def.get("required"):
            continue
        if slot_def.get("capability_family") != "video_generation":
            continue
        default_reqs = _slot_default_requirements(slot_def)
        cost_rows.extend(
            _comparison_rows(
                slot,
                profile_data,
                operation=_first_required_operation(default_reqs),
                duration_s=5.0,
                requirements=default_reqs,
            )
        )

    if any(r["severity"] == "blocker" for r in reasons):
        status = "BLOCKED"
    elif any(r["severity"] in ("warning", "degraded") for r in reasons):
        status = "DEGRADED"
    else:
        status = "GREEN"

    return {
        "status": status,
        "profile": profile,
        "reasons": _dedupe_reasons(reasons),
        "degradation": degradation,
        "cost_comparison": cost_rows,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _Unmet:
    """Set of unmet capability / operation / resolution requirements."""

    def __init__(self, capabilities: set, operations: set, resolutions: set):
        self.capabilities = capabilities
        self.operations = operations
        self.resolutions = resolutions


def _slot_chain(profile_data: dict, slot: str) -> list[dict]:
    binding = (profile_data.get("bindings") or {}).get(slot) or {}
    chain = binding.get("chain") if isinstance(binding, dict) else None
    if not isinstance(chain, list):
        return []
    return [c for c in chain if isinstance(c, dict) and c.get("tool")]


def _requirements_for(slot: str, requirements: dict) -> dict:
    if not requirements:
        return {}
    if all(
        isinstance(value, dict) and key in load_slots()
        for key, value in requirements.items()
    ):
        return dict(requirements.get(slot) or {})
    return dict(requirements)


def _slot_default_requirements(slot_def: dict) -> dict:
    reqs: dict[str, Any] = {}
    capabilities = slot_def.get("required_capabilities") or {}
    if capabilities:
        reqs["capabilities"] = (
            dict(capabilities) if isinstance(capabilities, dict)
            else {name: True for name in capabilities}
        )
    if slot_def.get("required_operations"):
        reqs["operations"] = list(slot_def["required_operations"])
    if slot_def.get("required_output_resolutions"):
        reqs["output_resolutions"] = list(slot_def["required_output_resolutions"])
    return reqs


def _merge_requirements(base: dict, override: dict) -> dict:
    """Layer explicit requirements over a slot's declared defaults."""
    merged = {key: value for key, value in base.items()}
    for key, value in (override or {}).items():
        if key == "capabilities" and isinstance(value, dict):
            caps = dict(merged.get("capabilities") or {})
            caps.update(value)
            merged["capabilities"] = caps
        else:
            merged[key] = value
    return merged


def _first_required_operation(requirements: dict) -> Optional[str]:
    ops = requirements.get("operations") if isinstance(requirements, dict) else None
    if ops:
        return str(list(ops)[0])
    return None


def _unmet_requirements(caps: dict, requirements: dict) -> _Unmet:
    unmet_caps: set[str] = set()
    unmet_ops: set[str] = set()
    unmet_res: set[str] = set()

    for operation in requirements.get("operations") or []:
        if operation not in list(caps.get("operations") or []):
            unmet_ops.add(str(operation))

    for resolution in requirements.get("output_resolutions") or []:
        have = caps.get("output_resolutions")
        if not isinstance(have, list) or resolution not in have:
            unmet_res.add(str(resolution))

    capability_reqs = dict(requirements.get("capabilities") or {})
    for key, value in requirements.items():
        if key in ("operations", "output_resolutions", "capabilities"):
            continue
        capability_reqs.setdefault(key, value)

    for name, expected in capability_reqs.items():
        actual = caps.get(name, "unknown")
        if not _satisfies(actual, expected):
            unmet_caps.add(str(name))

    return _Unmet(unmet_caps, unmet_ops, unmet_res)


def _satisfies(actual: Any, expected: Any) -> bool:
    """Return whether ``actual`` meets ``expected``; unknown never satisfies."""
    if actual in ("unknown", None) or isinstance(actual, str):
        # Strings are only equal to the exact same string; "unknown" fails all
        # boolean/numeric expectations by construction.
        return actual == expected and expected not in (True, False)
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return isinstance(actual, (int, float)) and actual >= expected
    if isinstance(expected, list):
        return isinstance(actual, list) and all(item in actual for item in expected)
    return actual == expected


def _unmet_list(unmet: _Unmet) -> list[str]:
    items: list[str] = []
    items.extend(sorted(unmet.capabilities))
    items.extend(f"operation:{op}" for op in sorted(unmet.operations))
    items.extend(f"resolution:{res}" for res in sorted(unmet.resolutions))
    return items


def _plan_matches(plan: dict, unmet: _Unmet, info: dict) -> bool:
    when = plan.get("when") or {}
    if not isinstance(when, dict) or not when:
        return True
    checks: list[bool] = []
    if "missing_capabilities" in when:
        checks.append(set(when["missing_capabilities"]) <= unmet.capabilities)
    if "missing_operations" in when:
        checks.append(set(when["missing_operations"]) <= unmet.operations)
    if "missing_output_resolutions" in when:
        checks.append(set(when["missing_output_resolutions"]) <= unmet.resolutions)
    if "chain_degraded" in when:
        checks.append(bool(info.get("chain_degraded")) == bool(when["chain_degraded"]))
    if "unavailable" in when:
        checks.append(bool(not info.get("usable")) == bool(when["unavailable"]))
    if "slot_unavailable" in when:
        checks.append(bool(not info.get("usable")))
    return all(checks)


def _select_plan(
    profile_data: dict,
    slot: Optional[str],
    unmet: _Unmet,
    info: dict,
) -> Optional[dict]:
    """Return the most specific predeclared plan matching the situation."""
    matched: list[tuple[int, dict]] = []
    for plan in profile_data.get("degradation") or []:
        if not isinstance(plan, dict):
            continue
        if slot is not None and plan.get("slot") not in (slot, None):
            continue
        if not _plan_matches(plan, unmet, info):
            continue
        if slot is None and not plan.get("when"):
            # Slot-less lookup is only used for declared delivery plans; they
            # are matched by id via _find_plan.
            continue
        matched.append((len(plan.get("when") or {}), plan))
    if not matched:
        return None
    matched.sort(key=lambda item: item[0], reverse=True)
    return matched[0][1]


def _find_plan(profile_data: dict, slot: Optional[str], plan_id: str) -> Optional[dict]:
    for plan in profile_data.get("degradation") or []:
        if not isinstance(plan, dict):
            continue
        if plan.get("id") != plan_id:
            continue
        if slot is not None and plan.get("slot") not in (slot, None):
            continue
        return plan
    return None


def _plan_added(plan: Optional[dict]) -> int:
    if not plan:
        return 0
    if "added" in plan:
        return int(plan["added"])
    return len(plan.get("path") or [])


def _plan_result(slot: str, plan: dict, unmet: _Unmet, info: dict) -> dict:
    lost = list(plan.get("lost") or _unmet_list(unmet))
    return {
        "slot": slot,
        "status": "DEGRADED",
        "plan_id": plan.get("id"),
        "lost": lost,
        "added": _plan_added(plan),
        "path": list(plan.get("path") or []),
        "approval_required": bool(plan.get("approval_required", False)),
        "reason": plan.get("note") or "",
        "chain_degraded": bool(info.get("chain_degraded")),
    }


def _dedupe_reasons(reasons: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for reason in reasons:
        key = (reason.get("code"), reason.get("slot"), reason.get("tool"))
        if key in seen:
            continue
        seen.add(key)
        out.append(reason)
    return out