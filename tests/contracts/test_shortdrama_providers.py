"""MOD-07/08 contract tests: four-provider catalog + dual-tier binding.

Guards:
- holopix / molica / agnes / zhipu are visible in the aggregated capability
  catalog (each provider owns one fragment) and the video selector discovers
  all four tools from the registry -- MOD-07.
- The draft tier picks the lowest estimated cash cost among candidates that
  satisfy the requirements *including output resolution*; an unknown price is
  never ranked as free, and it is labelled as unknown in doctor/plan output.
- The delivery tier is never auto-selected: it needs an explicit operator
  confirmation, records an append-only approval entry, and does not force the
  premium candidate (MOD-08).
- The existing dashscope catalog entry and the slots contract do not regress.

M0: local files plus monkeypatched tool registry only; zero external calls.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

import lib.capability_slots as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_BINDINGS = REPO_ROOT / "bindings"
CATALOG_SCHEMA = yaml.safe_load(
    (REPO_ROOT / "schemas" / "artifacts" / "capability_catalog.schema.json").read_text(
        encoding="utf-8"
    )
)

_MISSING = {
    "registered": False,
    "status": "missing",
    "supports": {},
    "capabilities": [],
    "module_path": None,
    "error": None,
}


def _tool(status="available", capabilities=None):
    return {
        "registered": True,
        "status": status,
        "supports": {},
        "capabilities": list(capabilities or ["text_to_video", "image_to_video"]),
        "module_path": "tests.fake",
        "error": None,
    }


def _entry(model, resolutions, *, first_frame=True, verification="VERIFIED"):
    return {
        "model": model,
        "operation": "image_to_video",
        "native_audio": True,
        "lipsync_from_dialogue": False,
        "reference_images_max": 1,
        "reference_video": False,
        "reference_audio": False,
        "multi_shot": False,
        "cross_shot_identity": False,
        "aspect_ratios": ["9:16"],
        "min_duration_s": 5,
        "max_duration_s": 10,
        "output_resolutions": list(resolutions),
        "seed_control": True,
        "first_frame_conditioning": first_frame,
        "source": "fixture",
        "verified_at": None,
        "verification_status": verification,
        "commercial_rights": "fixture rights",
    }


FREE = {
    "tool": "mock_free_video",
    "provider": "mockfree",
    "catalog_model": "mock/free-video",
    "tier": "free",
    "operations": ["text_to_video", "image_to_video"],
}
CHEAP = {
    "tool": "mock_cheap_video",
    "provider": "mockcheap",
    "catalog_model": "mock/cheap-video",
    "tier": "paid",
    "cost_cny_per_s": 0.10,
    "operations": ["text_to_video", "image_to_video"],
}
PREMIUM = {
    "tool": "mock_premium_video",
    "provider": "mockpremium",
    "catalog_model": "mock/premium-video",
    "tier": "paid",
    "cost_cny_per_s": 0.50,
    "operations": ["text_to_video", "image_to_video"],
}
UNKNOWN = {
    "tool": "mock_unknown_video",
    "provider": "mockunknown",
    "catalog_model": "mock/unknown-cost-video",
    "tier": "paid",
    "operations": ["text_to_video", "image_to_video"],
}
CREDIT = {
    "tool": "mock_credit_video",
    "provider": "mockcredit",
    "catalog_model": "mock/free-video",
    "tier": "credit",
    "quota": {"unit": "credits", "per_run": 10, "source": "fixture"},
    "operations": ["text_to_video", "image_to_video"],
}

CATALOG = [
    _entry("mock/free-video", ["480p"]),
    _entry("mock/cheap-video", ["480p", "720p"]),
    _entry("mock/premium-video", ["480p", "720p", "1080p"]),
    _entry("mock/unknown-cost-video", ["480p"]),
]

SLOTS = {
    "version": "1.0",
    "slots": {
        "video_primary": {
            "required": True,
            "capability_family": "video_generation",
            "required_operations": ["image_to_video"],
            "required_capabilities": {"first_frame_conditioning": True},
            "required_output_resolutions": ["480p"],
        }
    },
}

TOOLS = {
    "mock_free_video": _tool(),
    "mock_cheap_video": _tool(),
    "mock_premium_video": _tool(),
    "mock_unknown_video": _tool(),
    "mock_credit_video": _tool(),
}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _profile(chain, *, target="720x1280", degradation=None, degradation_id=None):
    delivery = {"target_platform": "tiktok", "target_resolution": target}
    if degradation_id:
        delivery["degradation_id"] = degradation_id
    data = {
        "version": "1.0",
        "bindings": {"video_primary": {"chain": copy.deepcopy(chain)}},
        "degradation": copy.deepcopy(degradation or []),
        "delivery": delivery,
    }
    return data


UPSCALE_PLAN = {
    "id": "delivery_upscale",
    "slot": "video_primary",
    "when": {"missing_output_resolutions": ["1080p"]},
    "lost": ["native_1080p"],
    "path": [{"stage": "delivery_upscale", "binding": "upscale"}],
    "approval_required": False,
    "declared": True,
}


def _bindings_tree(tmp_path, monkeypatch, *, profiles, tools=None, active=None):
    root = tmp_path / "bindings"
    _write_yaml(root / "slots.yaml", SLOTS)
    for name, data in profiles.items():
        data = copy.deepcopy(data)
        data.setdefault("profile", name)
        _write_yaml(root / "profiles" / f"{name}.yaml", data)
    _write_yaml(root / "capability_catalog.yaml", {"version": "1.0", "entries": CATALOG})
    _write_yaml(root / "account_state.yaml", {"version": "1.0", "providers": {}})
    _write_yaml(
        root / "model_bindings.yaml",
        {"version": "1.0", "active_profile": active or next(iter(profiles))},
    )

    states = {name: state for name, state in (tools or TOOLS).items()}

    def fake_tool_state(name: str) -> dict:
        return copy.deepcopy(states.get(name, _MISSING))

    monkeypatch.setattr(cs, "tool_state", fake_tool_state)
    monkeypatch.setenv(cs.BINDINGS_DIR_ENV, str(root))
    return root


# ---------------------------------------------------------------------------
# MOD-07 -- four providers in the catalog, selector auto-discovery
# ---------------------------------------------------------------------------


def test_four_provider_fragments_are_visible_and_schema_valid():
    catalog = cs.load_catalog()
    Draft202012Validator(CATALOG_SCHEMA).validate(catalog)

    models = {entry["model"] for entry in catalog["entries"]}
    for provider in ("holopix", "molica", "agnes", "zhipu"):
        fragment = REAL_BINDINGS / "catalog" / f"{provider}.yaml"
        assert fragment.is_file(), f"missing provider fragment {fragment.name}"
        # Fragments are individually schema-valid declarations.
        Draft202012Validator(CATALOG_SCHEMA).validate(
            yaml.safe_load(fragment.read_text(encoding="utf-8"))
        )
        assert any(model.startswith(f"{provider}/") for model in models), provider


def test_video_selector_discovers_all_four_providers():
    from tools.tool_registry import registry

    registry.ensure_discovered()
    discovered = {tool.name for tool in registry.get_by_capability("video_generation")}
    assert {
        "holopix_video",
        "molica_video",
        "agnes_video",
        "zhipu_video",
    } <= discovered


def test_dashscope_catalog_entry_is_unchanged():
    first = cs.catalog_entry("dashscope/wan3.0-video", "image_to_video")
    assert first is not None
    assert first["output_resolutions"] == ["480p", "720p", "1080p"]
    assert first["first_frame_conditioning"] is True
    assert first["verification_status"] == "VERIFIED"
    entries = [
        e
        for e in cs.load_catalog()["entries"]
        if e["model"] == "dashscope/wan3.0-video"
    ]
    assert len(entries) == 2  # text_to_video + image_to_video, no fragment duplicate


# ---------------------------------------------------------------------------
# MOD-08 -- draft tier: cheapest candidate that meets the requirements
# ---------------------------------------------------------------------------


def test_draft_tier_picks_lowest_estimated_cost(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([FREE, CHEAP, UNKNOWN, PREMIUM])},
    )
    plan = cs.plan_tiers("video_primary", "dual")

    draft = plan["draft"]
    assert draft["status"] == "OK"
    assert draft["tool"] == "mock_free_video"
    assert draft["estimated_cny"] == 0.0
    assert draft["cost_known"] is True
    # Every candidate is shown with its comparison basis; unknown prices rank
    # after known ones, so the cheapest known-cost candidates come first.
    assert [row["tool"] for row in draft["candidates"]] == [
        "mock_free_video",
        "mock_cheap_video",
        "mock_premium_video",
        "mock_unknown_video",
    ]


def test_draft_tier_falls_back_to_cheapest_available(tmp_path, monkeypatch):
    tools = copy.deepcopy(TOOLS)
    tools["mock_free_video"] = _tool(status="unavailable")
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([FREE, CHEAP, PREMIUM])},
        tools=tools,
    )
    plan = cs.plan_tiers("video_primary", "dual")

    assert plan["draft"]["tool"] == "mock_cheap_video"
    assert plan["draft"]["estimated_cny"] == pytest.approx(0.5)  # 0.10 CNY/s x 5s


def test_draft_tier_respects_output_resolution(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([FREE, CHEAP, PREMIUM])},
    )
    plan = cs.plan_tiers(
        "video_primary", "dual", requirements={"output_resolutions": ["720p"]}
    )

    assert plan["draft"]["tool"] == "mock_cheap_video"
    free_row = next(r for r in plan["cost_comparison"] if r["tool"] == "mock_free_video")
    assert free_row["meets_requirements"] is False
    assert "resolution:720p" in free_row["unmet"]


def test_unknown_cost_is_labelled_and_ranked_after_known_costs(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([UNKNOWN, CHEAP])},
    )
    plan = cs.plan_tiers("video_primary", "dual")

    # An unknown price can never be proven cheapest, so the known-cost
    # candidate wins even though it appears later in the chain.
    assert plan["draft"]["tool"] == "mock_cheap_video"
    unknown_row = next(
        row for row in plan["cost_comparison"] if row["tool"] == "mock_unknown_video"
    )
    assert unknown_row["estimated_cny"] is None
    assert unknown_row["cost_known"] is False
    assert "unknown" in unknown_row["cost_basis"]


def test_credit_quota_is_reported(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"credit": _profile([CREDIT])},
    )
    plan = cs.plan_tiers("video_primary", "credit")
    assert plan["draft"]["tool"] == "mock_credit_video"
    assert plan["draft"]["estimated_cny"] == 0.0
    assert plan["draft"]["quota"] == {"unit": "credits", "per_run": 10, "source": "fixture"}


# ---------------------------------------------------------------------------
# MOD-08 -- delivery tier: explicit confirmation, comparison, not premium
# ---------------------------------------------------------------------------


def test_delivery_tier_requires_explicit_confirmation_and_records_approval(
    tmp_path, monkeypatch
):
    root = _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([FREE, CHEAP, PREMIUM])},
    )

    plan = cs.plan_tiers("video_primary", "dual")
    delivery = plan["delivery"]
    assert delivery["status"] == "PENDING_APPROVAL"
    assert delivery["requires_confirmation"] is True
    assert delivery["approval_required"] is True
    assert delivery["selected"] is None
    assert not (root / "decision_log.json").exists()

    with pytest.raises(cs.TierApprovalRequired):
        cs.confirm_tier(
            "video_primary", "dual", cs.DELIVERY_TIER, confirmed=False
        )
    assert not (root / "decision_log.json").exists()

    # Deliberately pick the cheap candidate over the higher-quality premium
    # one: delivery confirmation is a decision, not a forced premium upgrade.
    result = cs.confirm_tier(
        "video_primary",
        "dual",
        cs.DELIVERY_TIER,
        tool="mock_cheap_video",
        confirmed=True,
        approved_by="owner",
        reason="cheaper candidate accepted for this delivery",
    )
    assert result["tool"] == "mock_cheap_video"
    assert result["estimated_cny"] == pytest.approx(0.5)

    data = json.loads((root / "decision_log.json").read_text(encoding="utf-8"))
    entry = data["decisions"][-1]
    assert entry["decision_id"] == result["decision_id"]
    assert entry["subject"] == "delivery_tier:video_primary"
    assert entry["selected"] == "mock_cheap_video"
    assert entry["user_approved"] is True
    assert entry["decided_by"] == "owner"
    assert "cheaper candidate accepted" in entry["reason"]
    assert len(entry["options_considered"]) == len(delivery["candidates"])
    assert all(option["reason"] for option in entry["options_considered"])
    assert cs.load_tier_approvals()[-1]["decision_id"] == result["decision_id"]


def test_tier_switches_append_never_overwrite(tmp_path, monkeypatch):
    root = _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([FREE, CHEAP, PREMIUM])},
    )
    cs.confirm_tier("video_primary", "dual", cs.DRAFT_TIER, confirmed=True)
    cs.confirm_tier(
        "video_primary", "dual", cs.DELIVERY_TIER, confirmed=True, tool="mock_premium_video"
    )

    decisions = json.loads((root / "decision_log.json").read_text(encoding="utf-8"))[
        "decisions"
    ]
    assert [d["subject"] for d in decisions] == [
        "draft_tier:video_primary",
        "delivery_tier:video_primary",
    ]
    assert [d["selected"] for d in decisions] == [
        "mock_free_video",
        "mock_premium_video",
    ]


def test_delivery_without_native_target_uses_declared_upscale_plan(
    tmp_path, monkeypatch
):
    profile = _profile(
        [FREE, CHEAP, UNKNOWN],
        target="1080x1920",
        degradation=[UPSCALE_PLAN],
        degradation_id="delivery_upscale",
    )
    _bindings_tree(tmp_path, monkeypatch, profiles={"draft-only": profile})

    delivery = cs.plan_tiers("video_primary", "draft-only")["delivery"]
    assert delivery["status"] == "PENDING_APPROVAL"
    assert delivery["upscale_required"] is True
    assert delivery["upscale_plan_id"] == "delivery_upscale"
    # The upscale stage's cost is unknown and says so.
    assert delivery["upscale_cost"]["cost_known"] is False
    assert delivery["upscale_cost"]["estimated_cny"] is None
    assert delivery["approval_required"] is True


def test_delivery_without_native_target_or_plan_is_blocked(tmp_path, monkeypatch):
    profile = _profile([FREE, CHEAP], target="1080x1920")
    _bindings_tree(tmp_path, monkeypatch, profiles={"no-plan": profile})

    delivery = cs.plan_tiers("video_primary", "no-plan")["delivery"]
    assert delivery["status"] == "BLOCKED"
    assert delivery["upscale_required"] is False
    with pytest.raises(cs.CapabilitySlotsError):
        cs.confirm_tier(
            "video_primary", "no-plan", cs.DELIVERY_TIER, confirmed=True
        )


# ---------------------------------------------------------------------------
# doctor/plan exposure
# ---------------------------------------------------------------------------


def test_doctor_reports_cost_comparison_basis(tmp_path, monkeypatch):
    _bindings_tree(
        tmp_path,
        monkeypatch,
        profiles={"dual": _profile([FREE, CHEAP, UNKNOWN, PREMIUM])},
    )
    report = cs.doctor("dual")

    rows = report["cost_comparison"]
    assert {row["tool"] for row in rows} == {
        "mock_free_video",
        "mock_cheap_video",
        "mock_unknown_video",
        "mock_premium_video",
    }
    by_tool = {row["tool"]: row for row in rows}
    assert by_tool["mock_cheap_video"]["estimated_cny"] == pytest.approx(0.5)
    assert by_tool["mock_premium_video"]["estimated_cny"] == pytest.approx(2.5)
    assert by_tool["mock_unknown_video"]["cost_known"] is False
    assert by_tool["mock_cheap_video"]["rights"] == "fixture rights"


def test_real_draft_profile_exposes_cost_basis_without_regressing_dashscope():
    report = cs.doctor("tiktok-draft")
    by_tool = {row["tool"]: row for row in report["cost_comparison"]}
    assert {"molica_video", "holopix_video", "dashscope_video"} <= set(by_tool)
    # The draft primary is a free-quota model (happyhorse), so the estimate is
    # zero cash while the price remains explicitly known.
    assert by_tool["dashscope_video"]["estimated_cny"] == pytest.approx(0.0)
    assert by_tool["dashscope_video"]["cost_known"] is True
    # Membership tiers declare no cash cost but keep their credit quota.
    assert by_tool["holopix_video"]["estimated_cny"] == 0.0
    assert by_tool["holopix_video"]["quota"]["unit"] == "compute_points"

    # dashscope.yaml itself is untouched and still resolves through the catalog.
    assert cs.catalog_entry("dashscope/wan3.0-video", "image_to_video") is not None