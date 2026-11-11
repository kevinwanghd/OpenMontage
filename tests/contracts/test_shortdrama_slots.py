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


def test_tiktok_draft_profile_declares_free_then_paid_480p_chain():
    data = cs.load_profile("tiktok-draft")
    chain = data["bindings"]["video_primary"]["chain"]
    assert [c["tier"] for c in chain] == ["free", "credit", "paid_fallback"]
    paid = chain[-1]
    assert paid["tool"] == "dashscope_video"
    assert paid["catalog_model"] == "dashscope/wan3.0-video"
    assert paid["capabilities"]["output_resolutions"] == ["480p"]
    assert paid["cost_cny_per_s"] == 0.30


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