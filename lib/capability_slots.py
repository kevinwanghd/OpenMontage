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

import os
import re
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
         "supports": {...}, "capabilities": [...], "module_path": str, "error": str|None}
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
            "error": f"tool registry unavailable: {exc}",
        }
    if tool is None:
        return {
            "registered": False,
            "status": "missing",
            "supports": {},
            "capabilities": [],
            "module_path": None,
            "error": None,
        }
    try:
        status = tool.get_status().value
    except Exception as exc:
        return {
            "registered": True,
            "status": "unavailable",
            "supports": dict(getattr(tool, "supports", {}) or {}),
            "capabilities": list(getattr(tool, "capabilities", []) or []),
            "module_path": getattr(tool.__class__, "__module__", None),
            "error": f"status check failed: {exc}",
        }
    return {
        "registered": True,
        "status": status,
        "supports": dict(getattr(tool, "supports", {}) or {}),
        "capabilities": list(getattr(tool, "capabilities", []) or []),
        "module_path": getattr(tool.__class__, "__module__", None),
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