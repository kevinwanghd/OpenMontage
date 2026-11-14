"""T5 contract tests for capability slots, bindings and doctor (MOD-01/02/03/05/06).

Guards:
- Switching a profile never touches a pipeline file -- manifests keep
  referencing slots, the profile decides the tool (MOD-01).
- The draft priority chain is free -> credit -> paid fallback, and only a
  full free/credit exhaustion reaches wan3.0-video at 480P (v2.4 / §12.10).
- Holopix with exhausted credits reports UNAVAILABLE, not "missing" (MOD-03).
- Doctor is three-state and only GREEN when the native chain is complete and
  verified; a token that cannot be proven live is DEGRADED (F15).
- Unknown is not support; every DEGRADED path comes from a plan predeclared in
  the profile, and no plan means BLOCKED (MOD-02/06, F14).

M0: local files + monkeypatched tool registry only, zero external API calls.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

import lib.capability_slots as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_BINDINGS = REPO_ROOT / "bindings"

_MISSING = {
    "registered": False,
    "status": "missing",
    "supports": {},
    "capabilities": [],
    "module_path": None,
    "error": None,
}


def _tool(status="available", supports=None, capabilities=None):
    return {
        "registered": True,
        "status": status,
        "supports": dict(supports or {}),
        "capabilities": list(capabilities or []),
        "module_path": "tests.fake",
        "error": None,
    }


# --- fixture bindings tree -------------------------------------------------

DEFAULT_SLOTS = {
    "version": "1.0",
    "slots": {
        "video_primary": {
            "required": True,
            "required_operations": ["image_to_video"],
            "required_capabilities": {"first_frame_conditioning": True},
            "required_output_resolutions": ["480p"],
        },
        "tts_voice": {"required": True},
    },
}

CATALOG_FREE = {
    "model": "mock/free",
    "operation": "image_to_video",
    "native_audio": True,
    "lipsync_from_dialogue": False,
    "reference_images_max": 1,
    "reference_video": False,
    "reference_audio": False,
    "aspect_ratios": ["9:16"],
    "min_duration_s": 5,
    "max_duration_s": 10,
    "output_resolutions": ["480p"],
    "first_frame_conditioning": True,
    "source": "fixture",
    "verified_at": None,
    "verification_status": "VERIFIED",
    "commercial_rights": "fixture",
}

CATALOG_HOLOPIX = {
    "model": "holopix/seedance-2-0",
    "operation": "image_to_video",
    "native_audio": True,
    "lipsync_from_dialogue": False,
    "reference_images_max": 1,
    "reference_video": False,
    "reference_audio": False,
    "aspect_ratios": ["9:16"],
    "min_duration_s": 5,
    "max_duration_s": 10,
    "output_resolutions": ["480p"],
    "first_frame_conditioning": True,
    "source": "fixture",
    "verified_at": None,
    "verification_status": "VERIFIED",
    "commercial_rights": "fixture",
}

CATALOG_PAID = {
    "model": "dashscope/wan3.0-video",
    "operation": "image_to_video",
    "native_audio": False,
    "lipsync_from_dialogue": False,
    "reference_images_max": 1,
    "reference_video": False,
    "reference_audio": False,
    "aspect_ratios": ["9:16"],
    "min_duration_s": 5,
    "max_duration_s": 10,
    "output_resolutions": ["480p"],
    "first_frame_conditioning": True,
    "source": "fixture",
    "verified_at": None,
    "verification_status": "VERIFIED",
    "commercial_rights": "fixture",
}

FREE = {
    "tool": "mock_free_video",
    "provider": "mockfree",
    "catalog_model": "mock/free",
    "tier": "free",
    "operations": ["text_to_video", "image_to_video"],
}
CREDIT = {
    "tool": "holopix_video",
    "provider": "holopix",
    "catalog_model": "holopix/seedance-2-0",
    "tier": "credit",
    "session_auth": True,
    "operations": ["text_to_video", "image_to_video"],
}
PAID = {
    "tool": "dashscope_video",
    "provider": "dashscope",
    "catalog_model": "dashscope/wan3.0-video",
    "tier": "paid_fallback",
    "cost_cny_per_s": 0.30,
    "operations": ["text_to_video", "image_to_video"],
    "capabilities": {"output_resolutions": ["480p"]},
}

DEGRADATION = [
    {
        "id": "free_tier_exhausted",
        "slot": "video_primary",
        "when": {"chain_degraded": True},
        "lost": ["free_tier"],
        "path": [{"stage": "shot_gen", "binding": "dashscope_video", "resolution": "480p"}],
        "approval_required": True,
    },
    {
        "id": "no_native_lipsync",
        "slot": "video_primary",
        "when": {"missing_capabilities": ["lipsync_from_dialogue"]},
        "lost": ["native_lipsync"],
        "path": [{"stage": "tts_dialogue"}, {"stage": "lipsync"}, {"stage": "mux"}],
        "approval_required": True,
    },
    {
        "id": "delivery_upscale",
        "slot": "video_primary",
        "when": {"missing_output_resolutions": ["1080p"]},
        "lost": ["native_1080p"],
        "path": [{"stage": "delivery_upscale", "binding": "upscale"}],
        "approval_required": False,
        "declared": True,
    },
]


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _bindings_tree(
    tmp_path: Path,
    monkeypatch,
    *,
    profiles: dict[str, dict],
    tools: dict[str, dict],
    catalog: list[dict] | None = None,
    account: dict[str, dict] | None = None,
    slots: dict | None = None,
    active: str | None = None,
) -> Path:
    root = tmp_path / "bindings"
    _write_yaml(root / "slots.yaml", slots or DEFAULT_SLOTS)
    for name, data in profiles.items():
        data = copy.deepcopy(data)
        data.setdefault("profile", name)
        _write_yaml(root / "profiles" / f"{name}.yaml", data)
    entries = (
        catalog
        if catalog is not None
        else [CATALOG_FREE, CATALOG_HOLOPIX, CATALOG_PAID]
    )
    _write_yaml(root / "capability_catalog.yaml", {"version": "1.0", "entries": entries})
    _write_yaml(root / "account_state.yaml", {"version": "1.0", "providers": account or {}})
    _write_yaml(
        root / "model_bindings.yaml",
        {"version": "1.0", "active_profile": active or next(iter(profiles))},
    )

    states = {name: state for name, state in tools.items()}

    def fake_tool_state(name: str) -> dict:
        return copy.deepcopy(states.get(name, _MISSING))

    monkeypatch.setattr(cs, "tool_state", fake_tool_state)
    monkeypatch.setenv(cs.BINDINGS_DIR_ENV, str(root))
    return root


def _draft_profile(**overrides) -> dict:
    data = {
        "version": "1.0",
        "bindings": {
            "video_primary": {"chain": [FREE, CREDIT, PAID]},
            "tts_voice": {"chain": [{"tool": "mock_tts", "tier": "local"}]},
        },
        "degradation": copy.deepcopy(DEGRADATION),
    }
    data.update(overrides)
    return data


def _plain_profile(**overrides) -> dict:
    data = {
        "version": "1.0",
        "bindings": {
            "video_primary": {"chain": [FREE]},
            "tts_voice": {"chain": [{"tool": "mock_tts", "tier": "local"}]},
        },
        "degradation": [],
    }
    data.update(overrides)
    return data


TOOLS_BASE = {
    "mock_free_video": _tool(capabilities=["text_to_video", "image_to_video"]),
    "holopix_video": _tool(capabilities=["text_to_video", "image_to_video"]),
    "dashscope_video": _tool(capabilities=["text_to_video", "image_to_video"]),
    "mock_tts": _tool(capabilities=["tts"]),
}


# --- real bindings ---------------------------------------------------------


def test_repo_catalog_validates_against_capability_schema():
    catalog = cs.load_catalog()
    schema = yaml.safe_load(
        (REPO_ROOT / "schemas" / "artifacts" / "capability_catalog.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(catalog)
    assert catalog["entries"], "catalog must not be empty"


# risk:test-removal reason:"旧用例 test_tiktok_draft_profile_declares_free_then_paid_480p_chain 已被本用例取代，覆盖同一绑定链并新增官方主链与会员非主链断言" owner:@kevinwanghd reviewed:2026-11-12
def test_tiktok_draft_video_primary_is_official_dashscope():
    """Owner decision 2026-11-12: the official Bailian API is the primary chain.

    Membership gateways may only follow it. Their catalog entries stay
    UNVERIFIED because R25 (membership ToS) is unresolved.
    """
    data = cs.load_profile("tiktok-draft")
    chain = data["bindings"]["video_primary"]["chain"]
    primary = chain[0]
    assert primary["tool"] == "dashscope_video"
    assert primary["provider"] == "dashscope"
    assert primary["catalog_model"] == "dashscope/wan3.0-video"
    assert primary["capabilities"]["output_resolutions"] == ["480p", "720p", "1080p"]
    assert primary["cost_cny_per_run"] == 3.0
    assert primary.get("primary_eligible", True) is True

    membership_tools = {"molica_video", "holopix_video"}
    for index, candidate in enumerate(chain[1:], start=1):
        assert candidate["tool"] in membership_tools
        assert candidate["primary_eligible"] is False, (
            f"{candidate['tool']} must not be usable as the primary chain"
        )
        assert candidate["role"] == "optional_fallback"
        assert "R25" in candidate["note"]
        entry = cs.catalog_entry(candidate["catalog_model"], "image_to_video")
        assert entry is not None
        assert entry["verification_status"] == "UNVERIFIED"

    # Live probe 2026-11-13 measured 480P portrait i2v; the entry is VERIFIED.
    video_entry = cs.catalog_entry("dashscope/wan3.0-video", "image_to_video")
    assert video_entry["verification_status"] == "VERIFIED"


def test_membership_gateways_are_optional_not_primary():
    data = cs.load_profile("tiktok-draft")
    chain = data["bindings"]["video_primary"]["chain"]
    assert chain[0]["tool"] not in {"molica_video", "holopix_video"}
    positions = {c["tool"]: i for i, c in enumerate(chain)}
    assert positions["dashscope_video"] == 0
    assert positions["molica_video"] > positions["dashscope_video"]
    assert positions["holopix_video"] > positions["dashscope_video"]


def test_image_slots_prefer_official_dashscope_image():
    """Image slots also follow the official-first decision.

    dashscope_image is an official Bailian API tool already present in the
    registry; the legacy fal.ai gateway remains only as a fallback. The primary
    is wan2.7-image: it is the only image model with live free quota on this
    account (qwen-image-* is arrears-blocked).
    """
    data = cs.load_profile("tiktok-draft")
    for slot in ("image_character_key", "image_reference"):
        chain = data["bindings"][slot]["chain"]
        assert chain[0]["tool"] == "dashscope_image"
        assert chain[0]["provider"] == "dashscope"
        assert chain[0]["catalog_model"] == "dashscope/wan2.7-image"
        assert chain[0]["model"] == "wan2.7-image"
        assert chain[1]["tool"] == "bria_fal_image"

    entry = cs.catalog_entry("dashscope/wan2.7-image", "text_to_image")
    assert entry is not None
    assert entry["verification_status"] == "VERIFIED"


def _available(capabilities=None, install=""):
    state = _tool(capabilities=capabilities)
    state["install_instructions"] = install
    return state


def _unavailable(install=""):
    state = _tool(status="unavailable")
    state["install_instructions"] = install
    return state


def _patch_real_profile_states(monkeypatch, overrides=None):
    """Patch the registry seam for tests that read the real bindings tree."""
    monkeypatch.setenv(cs.BINDINGS_DIR_ENV, str(REAL_BINDINGS))
    overrides = overrides or {}
    default = _MISSING

    def fake_tool_state(name):
        if name in overrides:
            return copy.deepcopy(overrides[name])
        return copy.deepcopy(default)

    monkeypatch.setattr(cs, "tool_state", fake_tool_state)


def test_tiktok_draft_resolves_official_dashscope_when_available(monkeypatch):
    _patch_real_profile_states(
        monkeypatch,
        {
            "dashscope_video": _available(["text_to_video", "image_to_video"]),
            "molica_video": _available(["text_to_video", "image_to_video"]),
            "holopix_video": _available(["text_to_video", "image_to_video"]),
        },
    )
    assert cs.resolve("video_primary", "tiktok-draft") == "dashscope_video"

    caps = cs.supports("video_primary", "tiktok-draft")
    assert caps["tool"] == "dashscope_video"
    assert caps["provider"] == "dashscope"
    assert caps["verification_status"] == "VERIFIED"
    assert caps["output_resolutions"] == ["480p", "720p", "1080p"]


def test_official_unavailable_falls_back_to_optional_membership(monkeypatch):
    """Membership gateways stay reachable -- but only after the official primary."""
    _patch_real_profile_states(
        monkeypatch,
        {
            "dashscope_video": _unavailable("Set DASHSCOPE_API_KEY."),
            "molica_video": _available(["text_to_video", "image_to_video"]),
        },
    )
    assert cs.resolve("video_primary", "tiktok-draft") == "molica_video"


def test_draft_tier_never_auto_selects_membership_gateways(monkeypatch):
    _patch_real_profile_states(
        monkeypatch,
        {
            "dashscope_video": _available(["text_to_video", "image_to_video"]),
            "molica_video": _available(["text_to_video", "image_to_video"]),
            "holopix_video": _available(["text_to_video", "image_to_video"]),
        },
    )
    plan = cs.plan_tiers("video_primary", "tiktok-draft")
    assert plan["draft"]["status"] == "OK", plan["draft"].get("reason")
    assert plan["draft"]["tool"] == "dashscope_video"

    rows = {row["tool"]: row for row in plan["cost_comparison"]}
    assert rows["molica_video"]["primary_eligible"] is False
    assert rows["holopix_video"]["primary_eligible"] is False
    assert rows["dashscope_video"]["primary_eligible"] is True


def test_switching_profile_keeps_the_real_short_drama_pipeline_bytes(monkeypatch):
    pipeline = REPO_ROOT / "pipeline_defs" / "short-drama.yaml"
    before = pipeline.read_bytes()
    _patch_real_profile_states(
        monkeypatch,
        {
            "dashscope_video": _available(["text_to_video", "image_to_video"]),
            "seedance_ark": _available(["text_to_video", "image_to_video"]),
        },
    )

    assert cs.resolve("video_primary", "tiktok-draft") == "dashscope_video"
    cs.load_profile("seedance")
    assert cs.resolve("video_primary", "seedance") == "seedance_ark"
    cs.load_profile("tiktok-draft")

    assert pipeline.read_bytes() == before
    text = before.decode("utf-8")
    assert "slot:video_primary" in text
    assert "dashscope_video" not in text


def test_doctor_without_credentials_is_blocked_and_reports_setup(monkeypatch):
    """No key -> BLOCKED with actionable setup guidance, never a fake GREEN."""
    hint = "Set DASHSCOPE_API_KEY to your Alibaba Cloud Bailian API key."
    _patch_real_profile_states(
        monkeypatch,
        {
            "dashscope_video": _unavailable(hint),
            "dashscope_image": _unavailable(hint),
            "bria_fal_image": _unavailable("Set FAL_KEY for the fal.ai gateway."),
            "doubao_tts": _unavailable("Set DOUBAO_SPEECH_API_KEY."),
        },
    )
    report = cs.doctor("tiktok-draft")
    assert report["status"] == "BLOCKED"
    codes = {r["code"] for r in report["reasons"]}
    assert "CREDENTIALS_MISSING" in codes
    setup = [r for r in report["reasons"] if r["code"] == "SETUP_REQUIRED"]
    assert setup, "a blocked slot must explain how to fix its credential"
    assert any("DASHSCOPE_API_KEY" in r["message"] for r in setup)
    assert any(r["slot"] == "image_character_key" for r in setup)


def test_doctor_verified_dashscope_video_warns_only_about_upscale(monkeypatch):
    """VERIFIED video removes CAPABILITY_UNVERIFIED; 480p draft still needs upscale."""
    _patch_real_profile_states(
        monkeypatch,
        {
            "dashscope_video": _available(["text_to_video", "image_to_video"]),
            "dashscope_image": _available(["text_to_image"]),
            "bria_fal_image": _available(["image_edit"]),
            "doubao_tts": _available(["tts"]),
            "kling_lip_sync": _available(["lipsync"]),
            "music_gen": _available(["music"]),
            "video_compose": _available(["compose"]),
            "subtitle_gen": _available(["subtitle"]),
            "upscale": _available(["upscale"]),
        },
    )
    report = cs.doctor("tiktok-draft")
    assert report["status"] == "DEGRADED"
    video_reasons = [r for r in report["reasons"] if r["slot"] == "video_primary"]
    assert not any(r["code"] == "CAPABILITY_UNVERIFIED" for r in video_reasons)
    assert any(r["code"] == "DRAFT_UPSCALE_DECLARED" for r in video_reasons)
    assert not any("VERIFIED" == str(r.get("message", "")) for r in report["reasons"])


def test_holopix_is_registered_but_credits_exhausted():
    account = cs.load_account_state()
    assert account["holopix_video"]["status"] == "EXHAUSTED"
    # Kept registered on purpose; exhaustion is account state, not deletion.
    assert "holopix" in [p.stem for p in (REAL_BINDINGS / "profiles").glob("*.yaml")]


def test_repo_profiles_bind_only_declared_slots_and_predeclare_degradation():
    slots = cs.load_slots()
    for name in ("tiktok-draft", "seedance", "veo", "kling", "holopix"):
        data = cs.load_profile(name)
        for slot in data["bindings"]:
            assert slot in slots, f"{name} binds undeclared slot {slot}"
        for plan in data["degradation"]:
            assert plan["id"]
            assert plan["slot"] in slots
            assert plan.get("when"), f"{plan['id']} must declare a condition"
            assert plan.get("path"), f"{plan['id']} must declare sub-stages"
            assert "note" in plan or "lost" in plan


# --- profile switching -----------------------------------------------------


def test_switching_profile_does_not_touch_pipeline_file(tmp_path, monkeypatch):
    profile_a = _plain_profile()
    profile_b = {
        "version": "1.0",
        "bindings": {
            "video_primary": {
                "chain": [dict(FREE, tool="dashscope_video", provider="dashscope")]
            },
            "tts_voice": {"chain": [{"tool": "mock_tts", "tier": "local"}]},
        },
        "degradation": [],
    }
    root = _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"a": profile_a, "b": profile_b},
        tools=TOOLS_BASE,
        active="a",
    )
    pipeline = tmp_path / "short-drama.yaml"
    pipeline.write_text(
        "stages:\n  - name: shot_gen\n    tools_available:\n      - slot:video_primary\n",
        encoding="utf-8",
    )
    before = pipeline.read_bytes()

    assert cs.resolve("video_primary", "a") == "mock_free_video"
    assert cs.resolve("video_primary", "b") == "dashscope_video"
    assert pipeline.read_bytes() == before

    # One pointer line is the whole switch.
    assert cs.active_profile() == "a"
    (root / "model_bindings.yaml").write_text(
        "version: '1.0'\nactive_profile: b\n", encoding="utf-8"
    )
    assert cs.active_profile() == "b"
    assert pipeline.read_bytes() == before


# --- resolution priority ---------------------------------------------------


def test_resolve_prefers_free_binding(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path, monkeypatch, profiles={"draft": _draft_profile()}, tools=TOOLS_BASE
    )
    assert cs.resolve("video_primary", "draft") == "mock_free_video"
    assert cs.resolve("video_primary", "draft", operation="text_to_video") == "mock_free_video"


def test_free_exhausted_falls_back_to_wan30_480p(tmp_path, monkeypatch):
    tools = copy.deepcopy(TOOLS_BASE)
    tools["mock_free_video"] = _tool(status="unavailable")
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"draft": _draft_profile()},
        tools=tools,
        account={"holopix_video": {"status": "EXHAUSTED"}},
    )

    assert cs.resolve("video_primary", "draft") == "dashscope_video"

    caps = cs.supports("video_primary", "draft")
    assert caps["tool"] == "dashscope_video"
    assert caps["tier"] == "paid_fallback"
    assert caps["output_resolutions"] == ["480p"]

    report = cs.doctor("draft")
    assert report["status"] == "DEGRADED"
    codes = {r["code"] for r in report["reasons"]}
    assert "PRIMARY_UNAVAILABLE_FALLBACK" in codes
    plans = {d["plan_id"] for d in report["degradation"]}
    assert "free_tier_exhausted" in plans
    fallback = next(d for d in report["degradation"] if d["plan_id"] == "free_tier_exhausted")
    assert fallback["path"][0]["resolution"] == "480p"
    assert fallback["approval_required"] is True


def test_holopix_credits_exhausted_is_unavailable(tmp_path, monkeypatch):
    profile = _plain_profile()
    profile["bindings"]["video_primary"]["chain"] = [CREDIT]
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"holopix-only": profile},
        tools=TOOLS_BASE,
        account={"holopix_video": {"status": "EXHAUSTED"}},
    )

    with pytest.raises(cs.SlotUnavailable) as excinfo:
        cs.resolve("video_primary", "holopix-only")
    assert excinfo.value.reasons[0]["code"] == "CREDITS_EXHAUSTED"

    report = cs.doctor("holopix-only")
    assert report["status"] == "BLOCKED"
    assert any(r["code"] == "CREDITS_EXHAUSTED" for r in report["reasons"])


def test_all_candidates_unavailable_blocks(tmp_path, monkeypatch):
    tools = copy.deepcopy(TOOLS_BASE)
    tools["mock_free_video"] = _tool(status="unavailable")
    del tools["dashscope_video"]  # pre-WP-F: paid tool not registered yet
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"draft": _draft_profile()},
        tools=tools,
        account={"holopix_video": {"status": "EXHAUSTED"}},
    )
    report = cs.doctor("draft")
    assert report["status"] == "BLOCKED"
    codes = {r["code"] for r in report["reasons"]}
    assert {"CREDENTIALS_MISSING", "CREDITS_EXHAUSTED", "TOOL_NOT_REGISTERED"} <= codes


# --- doctor three-state ----------------------------------------------------


def test_complete_verified_chain_is_green(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path, monkeypatch, profiles={"green": _plain_profile()}, tools=TOOLS_BASE
    )
    report = cs.doctor("green")
    assert report["status"] == "GREEN", report["reasons"]
    assert report["reasons"] == []
    assert report["degradation"] == []


def test_unverified_session_is_not_green(tmp_path, monkeypatch):
    profile = _plain_profile()
    profile["bindings"]["video_primary"]["chain"] = [CREDIT]
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"session": profile},
        tools=TOOLS_BASE,
        account={"holopix_video": {"status": "AVAILABLE", "session_verified": False}},
    )
    report = cs.doctor("session")
    assert report["status"] == "DEGRADED"
    assert any(r["code"] == "SESSION_UNVERIFIED" for r in report["reasons"])


def test_verified_session_and_capabilities_is_green(tmp_path, monkeypatch):
    profile = _plain_profile()
    profile["bindings"]["video_primary"]["chain"] = [CREDIT]
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"session": profile},
        tools=TOOLS_BASE,
        account={"holopix_video": {"status": "AVAILABLE", "session_verified": True}},
    )
    assert cs.doctor("session")["status"] == "GREEN"


def test_unverified_capability_is_degraded_not_green(tmp_path, monkeypatch):
    catalog = [dict(CATALOG_FREE, verification_status="UNVERIFIED")]
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"green": _plain_profile()},
        tools=TOOLS_BASE,
        catalog=catalog,
    )
    report = cs.doctor("green")
    assert report["status"] == "DEGRADED"
    assert any(r["code"] == "CAPABILITY_UNVERIFIED" for r in report["reasons"])


def test_unknown_capability_is_not_support(tmp_path, monkeypatch):
    catalog = [dict(CATALOG_FREE, first_frame_conditioning="unknown")]
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"green": _plain_profile()},
        tools=TOOLS_BASE,
        catalog=catalog,
    )
    caps = cs.supports("video_primary", "green")
    assert caps["first_frame_conditioning"] == "unknown"
    plans = cs.plan_degradation(
        ["video_primary"], "green", {"capabilities": {"first_frame_conditioning": True}}
    )
    assert plans[0]["status"] == "BLOCKED"
    assert "first_frame_conditioning" in plans[0]["lost"]


def test_unknown_profile_blocks_instead_of_raising():
    report = cs.doctor("does-not-exist")
    assert report["status"] == "BLOCKED"
    assert report["reasons"][0]["code"] == "UNKNOWN_PROFILE"

    with pytest.raises(cs.UnknownSlotError):
        cs.resolve("no_such_slot", "tiktok-draft")


# --- degradation planning --------------------------------------------------


def test_predeclared_degradation_path_is_returned(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path, monkeypatch, profiles={"draft": _draft_profile()}, tools=TOOLS_BASE
    )
    plans = cs.plan_degradation(
        ["video_primary"], "draft", {"capabilities": {"lipsync_from_dialogue": True}}
    )
    entry = plans[0]
    assert entry["status"] == "DEGRADED"
    assert entry["plan_id"] == "no_native_lipsync"
    assert [step["stage"] for step in entry["path"]] == ["tts_dialogue", "lipsync", "mux"]
    assert entry["lost"] == ["native_lipsync"]
    assert entry["added"] == 3
    assert entry["approval_required"] is True


def test_undeclared_degradation_is_blocked(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path, monkeypatch, profiles={"draft": _draft_profile()}, tools=TOOLS_BASE
    )
    plans = cs.plan_degradation(
        ["video_primary"], "draft", {"capabilities": {"reference_video": True}}
    )
    assert plans[0]["status"] == "BLOCKED"
    assert plans[0]["path"] == []
    assert "reference_video" in plans[0]["lost"]


def test_met_requirements_are_ok(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path, monkeypatch, profiles={"draft": _draft_profile()}, tools=TOOLS_BASE
    )
    plans = cs.plan_degradation(
        ["video_primary"], "draft", {"output_resolutions": ["480p"]}
    )
    assert plans[0]["status"] == "OK"
    assert plans[0]["lost"] == []


def test_doctor_reports_declared_delivery_upscale(tmp_path, monkeypatch):
    profile = _plain_profile(
        delivery={
            "draft_resolution": "480p",
            "target_resolution": "1080x1920",
            "requires_upscale": True,
            "degradation_id": "delivery_upscale",
        },
        degradation=[copy.deepcopy(DEGRADATION[2])],
    )
    _bindings_tree(
        tmp_path, monkeypatch, profiles={"draft": profile}, tools=TOOLS_BASE
    )
    report = cs.doctor("draft")
    assert report["status"] == "DEGRADED"
    assert any(r["code"] == "DRAFT_UPSCALE_DECLARED" for r in report["reasons"])
    upscale = next(d for d in report["degradation"] if d["plan_id"] == "delivery_upscale")
    assert upscale["declared"] is True
    assert upscale["path"][0]["stage"] == "delivery_upscale"


# --- legacy supports normalization (MOD-02 / F13) --------------------------


def test_legacy_supports_keys_are_normalized_and_booleans_not_upgraded(tmp_path, monkeypatch):
    profile = _plain_profile()
    profile["bindings"]["video_primary"]["chain"] = [
        {"tool": "legacy_video", "tier": "local", "operations": ["image_to_video"]}
    ]
    tools = copy.deepcopy(TOOLS_BASE)
    tools["legacy_video"] = _tool(
        supports={
            "max_reference_images": 3,
            "audio": True,
            "first_frame": True,
            "resolution": True,
            "aspect_ratio": True,
        },
        capabilities=["image_to_video"],
    )
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"legacy": profile},
        tools=tools,
        catalog=[],
        slots={
            "version": "1.0",
            "slots": {"video_primary": {"required": True}},
        },
    )
    caps = cs.supports("video_primary", "legacy")
    assert caps["reference_images_max"] == 3
    assert caps["native_audio"] is True
    assert caps["first_frame_conditioning"] is True
    # `resolution: true` names no concrete resolution -> stays unknown.
    assert caps["output_resolutions"] == "unknown"
    assert caps["aspect_ratios"] == "unknown"

# --- console encoding safety ------------------------------------------------


class _GbkConsole:
    """Mimics a GBK ``TextIOWrapper``: every write must encode strictly."""

    encoding = "gbk"

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, text: str) -> None:
        self.chunks.append(text.encode("gbk"))  # raises exactly like the real CRT

    def getvalue(self) -> str:
        return b"".join(self.chunks).decode("gbk")


def test_cli_write_degrades_unencodable_characters_on_gbk_console():
    """`om doctor` must not crash when a report carries a character GBK cannot
    encode (the reported defect: `¥` in a degradation note on a CN console)."""
    from shortdrama import cli

    console = _GbkConsole()
    report = {
        "status": "DEGRADED",
        "reasons": [{"message": "wan3.0-video 480P at ¥0.30/s"}],
    }
    cli._write(report, True, stream=console)  # must not raise UnicodeEncodeError

    rendered = console.getvalue()
    assert "0.30/s" in rendered, "the readable part of the reason must survive"
    assert rendered.encode("gbk")  # the whole line stayed valid GBK


def test_om_doctor_survives_a_gbk_console_end_to_end():
    """The real entry point, under a GBK stdio encoding, exits cleanly."""
    proc = subprocess.run(
        [sys.executable, "-m", "shortdrama", "doctor", "--json"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONIOENCODING": "gbk"},
        capture_output=True,
    )
    stderr = proc.stderr.decode("gbk", errors="replace")
    assert "UnicodeEncodeError" not in stderr, stderr
    assert proc.returncode in (0, 4), stderr  # 0 healthy, 4 BLOCKED -- never 1
    proc.stdout.decode("gbk")  # every byte written is valid GBK
