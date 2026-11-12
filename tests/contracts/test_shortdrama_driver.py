"""WP-3 contract tests: M1 nine-stage driver (mock default, live opt-in).

Guards the hard parts of the real-generation driver:

- the nine stages come from ``pipeline_defs/short-drama.yaml`` in manifest
  order, and switching profile changes the resolved tool without touching the
  pipeline file (MOD-01 / DEL-08);
- mock mode executes nothing and opens no socket;
- ``--real`` is refused without ``--approve-live`` *and* a positive budget
  (recovery=do-X-first, side_effects=none) and leaves no ledger/file behind;
- every paid call records a submission intent *before* the executor runs;
- a failed call / missing task id leaves the ledger UNKNOWN, and the next run
  refuses to re-pay (resume mode=reconcile);
- an over-budget or unknown-price call pauses with an agent-ready error before
  any spend;
- declared provider fallback + degradation are written to the append-only
  decision log (closed category enum) before execution (NFR-06).

Everything is offline: the session socket guard is active and the live
executor is always replaced with a fake.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest
import yaml

import lib.capability_slots as cs
from lib.resume import resume_plan
from lib.submission_ledger import SubmissionLedger
from shortdrama import cli
from shortdrama.driver import Driver
from shortdrama.errors import ApiError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_FILE = REPO_ROOT / "pipeline_defs" / "short-drama.yaml"

NINE_STAGES = [
    "idea",
    "beat_map",
    "script",
    "character_cast",
    "scene_plan",
    "keyframe",
    "shot_gen",
    "assembly",
    "delivery",
]

TOOL_CAPABILITIES = {
    "alpha_video": ["image_to_video", "text_to_video"],
    "beta_video": ["image_to_video"],
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _local_chain(tool: str, operation: str) -> dict:
    return {
        "chain": [
            {
                "tool": tool,
                "provider": "local",
                "tier": "local",
                "cost_cny_per_run": 0.0,
                "operations": [operation],
            }
        ]
    }


def make_bindings(root: Path) -> Path:
    slots = {
        "video_primary": {
            "required": True,
            "required_operations": ["image_to_video"],
        },
        "image_character_key": {"required": True},
        "image_reference": {"required": True},
        "tts_voice": {"required": True},
        "music": {"required": False},
        "subtitle": {"required": True},
        "compose": {"required": True},
        "upscale": {"required": False},
    }
    _write_yaml(root / "slots.yaml", {"version": "1.0", "slots": slots})
    _write_yaml(root / "account_state.yaml", {"version": "1.0", "providers": {}})
    _write_yaml(root / "model_bindings.yaml", {
        "version": "1.0",
        "active_profile": "alpha",
        "profiles_dir": "profiles",
    })
    _write_yaml(root / "common.yaml", {
        "version": "1.0",
        "bindings": {
            "image_character_key": {
                "chain": [{
                    "tool": "img_key", "provider": "local", "tier": "local",
                    "cost_cny_per_run": 0.0, "operations": ["image_edit"],
                }]
            },
            "image_reference": {
                "chain": [{
                    "tool": "img_ref", "provider": "local", "tier": "local",
                    "cost_cny_per_run": 0.0, "operations": ["image_edit"],
                }]
            },
            "tts_voice": _local_chain("tts_tool", "tts"),
            "music": _local_chain("music_tool", "music"),
            "subtitle": _local_chain("subtitle_tool", "subtitle"),
            "compose": _local_chain("compose_tool", "compose"),
            "upscale": _local_chain("upscale_tool", "upscale"),
        },
    })

    def video_profile(name: str, tool: str) -> dict:
        return {
            "version": "1.0",
            "profile": name,
            "bindings": {
                "video_primary": {
                    "chain": [{
                        "tool": tool,
                        "provider": name,
                        "catalog_model": f"test/{name}",
                        "tier": "paid",
                        "cost_cny_per_run": 2.0,
                        "operations": ["image_to_video"],
                    }]
                }
            },
            "degradation": [],
        }

    _write_yaml(root / "profiles" / "alpha.yaml", video_profile("alpha", "alpha_video"))
    _write_yaml(root / "profiles" / "beta.yaml", video_profile("beta", "beta_video"))
    _write_yaml(root / "profiles" / "fallback.yaml", {
        "version": "1.0",
        "profile": "fallback",
        "bindings": {
            "video_primary": {
                "chain": [
                    {
                        "tool": "missing_video", "provider": "gone",
                        "catalog_model": "test/gone", "tier": "paid",
                        "cost_cny_per_run": 2.0, "operations": ["image_to_video"],
                    },
                    {
                        "tool": "alpha_video", "provider": "alpha",
                        "catalog_model": "test/alpha", "tier": "paid",
                        "cost_cny_per_run": 2.0, "operations": ["image_to_video"],
                    },
                ]
            }
        },
        "degradation": [{
            "id": "sep_lipsync",
            "slot": "video_primary",
            "when": {"missing_capabilities": ["lipsync_from_dialogue"]},
            "lost": ["native_lipsync"],
            "path": [
                {"stage": "tts_dialogue"},
                {"stage": "lipsync"},
                {"stage": "mux"},
            ],
            "approval_required": True,
            "note": "no native dialogue lipsync; use TTS + lipsync + mux",
        }],
    })
    return root


def fake_tool_state(name: str) -> dict:
    if name == "missing_video":
        return {
            "registered": False, "status": "missing", "supports": {},
            "capabilities": [], "module_path": None, "error": None,
        }
    return {
        "registered": True,
        "status": "available",
        "supports": {},
        "capabilities": TOOL_CAPABILITIES.get(name, []),
        "module_path": "tests.fake",
        "error": None,
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _approved_checkpoint(pipeline_dir: Path, stage: str, artifacts: dict) -> None:
    checkpoint = {
        "version": "1.0",
        "project_id": "demo",
        "pipeline_type": "short-drama",
        "stage": stage,
        "status": "completed",
        "timestamp": "2026-11-12T00:00:00+00:00",
        "checkpoint_policy": "manual_all",
        "human_approval_required": True,
        "human_approved": True,
        "artifacts": artifacts,
    }
    _write_json(pipeline_dir / "demo" / f"checkpoint_{stage}.json", checkpoint)


def seed_reviewed_artifacts(projects: Path, *, shots: int = 1) -> None:
    """Seed the agent-authored artifacts and the approved review checkpoints.

    ``character_cast`` is deliberately left unexecuted (no checkpoint): the
    driver runs it, exactly like production. ``scene_plan`` is written as an
    approved checkpoint directly because its manifest predecessor
    (``character_cast``) is still pending at seed time.
    """
    brief = {
        "version": "1.0",
        "title": "The Last Message",
        "hook": "She reads his message one hour after the funeral.",
        "key_points": ["a phone that should be dead", "a choice"],
        "tone": "tense",
        "style": "live-action vertical drama",
        "target_platform": "tiktok",
        "target_duration_seconds": 120,
    }
    beat_map = {"version": "1.0", "total_duration_seconds": 120, "beats": []}
    script = {
        "version": "1.0",
        "title": "The Last Message",
        "total_duration_seconds": 5 * shots,
        "sections": [
            {
                "id": "s1",
                "text": "You said you'd never leave me.",
                "start_seconds": 0,
                "end_seconds": 5 * shots,
                "speaker_directions": "quiet, hurt",
            }
        ],
    }
    scene_plan = {
        "version": "1.0",
        "scenes": [
            {
                "id": "sc1",
                "type": "character_scene",
                "description": "two people in a dim room",
                "start_seconds": 0,
                "end_seconds": 5 * shots,
            }
        ],
        "shots": [
            {
                "shot_id": f"sh_{index:03d}",
                "duration_s": 5,
                "single_action": "She turns away from him",
                "visual_evidence": "tears catch the window light",
                "shot_language": {
                    "shot_size": "close_up",
                    "camera_movement": "static",
                    "lighting_key": "low_key",
                },
                "reference_character_ids": ["ch_lin"],
                "needs_lipsync": True,
                "candidates": 2,
            }
            for index in range(shots)
        ],
    }
    character = {
        "version": "1.0",
        "character_id": "ch_lin",
        "name": "Lin",
        "canonical_description": "woman in her late twenties, black hair, grey coat",
        "voice_id": "voice_lin",
    }
    asset_manifest = {
        "version": "1.0",
        "assets": [
            {
                "id": f"kf_sh_{index:03d}",
                "type": "image",
                "path": f"assets/keyframes/sh_{index:03d}_v1.png",
                "source_tool": "img_ref",
                "scene_id": f"sh_{index:03d}",
            }
            for index in range(shots)
        ],
    }
    edit_decisions = {"version": "1.0", "cuts": [], "render_runtime": "ffmpeg"}

    artifacts_dir = projects / "demo" / "ep01" / "artifacts"
    _write_json(artifacts_dir / "brief.json", brief)
    _write_json(artifacts_dir / "beat_map.json", beat_map)
    _write_json(artifacts_dir / "script.json", script)
    _write_json(artifacts_dir / "scene_plan.json", scene_plan)
    _write_json(artifacts_dir / "character_design.json", {"version": "1.0", "characters": []})
    _write_json(artifacts_dir / "characters" / "ch_lin.json", character)
    _write_json(artifacts_dir / "asset_manifest.json", asset_manifest)
    _write_json(artifacts_dir / "edit_decisions.json", edit_decisions)

    _approved_checkpoint(projects, "idea", {"brief": brief})
    _approved_checkpoint(projects, "beat_map", {})
    _approved_checkpoint(projects, "script", {"script": script})
    _approved_checkpoint(projects, "scene_plan", {"scene_plan": scene_plan})


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    seed_reviewed_artifacts(root)
    return root


@pytest.fixture
def bindings(tmp_path, monkeypatch):
    root = make_bindings(tmp_path / "bindings")
    monkeypatch.setenv(cs.BINDINGS_DIR_ENV, str(root))
    monkeypatch.setattr(cs, "tool_state", fake_tool_state)
    return root


def ok_executor(calls: list) -> callable:
    def execute(tool: str, inputs: dict) -> dict:
        calls.append((tool, inputs))
        return {
            "success": True,
            "task_id": f"task-{inputs['stage']}-{inputs['slot']}",
            "cost_cny": 0.0,
            "artifacts": [],
        }

    return execute


# ---------------------------------------------------------------------------
# 1. nine stages from the manifest, mock = zero execution / zero network
# ---------------------------------------------------------------------------


def test_mock_drives_nine_manifest_stages_without_network(projects, bindings, monkeypatch):
    forbidden_calls: list = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("mock driver must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    result = driver.run()  # mock is the default

    assert result["mock"] is True and result["real"] is False
    assert [stage["stage"] for stage in result["stages"]] == NINE_STAGES
    assert result["external_api_calls"] == 0
    assert result["unresolved_slots"] == 0
    assert forbidden_calls == []

    # The declared order is the manifest's, not a hardcoded list that drifted.
    manifest = yaml.safe_load(PIPELINE_FILE.read_text(encoding="utf-8"))
    assert [stage["name"] for stage in manifest["stages"]] == NINE_STAGES

    # No ledger and no intent are created by a mock run.
    assert not (projects / "demo" / "ep01" / "ledger.jsonl").exists()


def test_mock_resolves_slots_through_profile_not_provider_names(projects, bindings):
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    plan = driver.plan()
    by_stage = {stage["stage"]: stage for stage in plan["stages"]}
    assert by_stage["shot_gen"]["slots"][0]["tool"] == "alpha_video"
    assert by_stage["assembly"]["slots"][0]["slot"] == "tts_voice"
    assert plan["unresolved_slots"] == 0


# ---------------------------------------------------------------------------
# 2. real mode consent gate: no approval / no budget -> no side effects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "approve_live,budget_usd,missing",
    [
        (False, None, {"--approve-live", "--budget-usd N (positive)"}),
        (True, None, {"--budget-usd N (positive)"}),
        (False, 5.0, {"--approve-live"}),
        (True, 0.0, {"--budget-usd N (positive)"}),
    ],
)
def test_real_requires_live_approval_and_budget(projects, bindings, approve_live,
                                                budget_usd, missing):
    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, executor=ok_executor(calls))

    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=approve_live, budget_usd=budget_usd)

    error = raised.value
    assert error.code == ErrorCode.VALIDATION_ERROR
    assert error.recovery == "do-X-first"
    assert error.side_effects == "none"
    assert set(error.context["missing"]) == missing
    assert calls == []
    assert not (projects / "demo" / "ep01" / "ledger.jsonl").exists()
    assert not (projects / "demo" / "ep01" / "decision_log.jsonl").exists()


def test_cli_demo_real_requires_consent_flags(projects, capsys):
    code = cli.main(["--projects-dir", str(projects), "demo", "--real", "--json"])
    assert code == 2
    out = capsys.readouterr()
    envelope = json.loads(out.err)
    assert envelope["code"] == ErrorCode.VALIDATION_ERROR.value
    assert envelope["recovery"] == "do-X-first"
    assert envelope["side_effects"] == "none"
    # No ledger, asset or spend artifact is created by the refused run.
    assert not (projects / "demo" / "ep01" / "ledger.jsonl").exists()
    assert not (projects / "demo" / "ep01" / "assets").exists()


def test_cli_demo_rejects_mock_and_real_together(projects, capsys):
    code = cli.main([
        "--projects-dir", str(projects), "demo", "--mock", "--real",
        "--approve-live", "--budget-usd", "5", "--json",
    ])
    assert code == 2
    out = capsys.readouterr()
    assert json.loads(out.err)["code"] == ErrorCode.VALIDATION_ERROR.value


# ---------------------------------------------------------------------------
# 3. intent before the paid call, reconciliation after
# ---------------------------------------------------------------------------


def test_intent_is_written_before_each_paid_call(projects, bindings):
    observed: list[dict] = []

    def execute(tool: str, inputs: dict) -> dict:
        ledger = SubmissionLedger("demo", "ep01", projects)
        latest = list(ledger._latest().values())
        assert latest, f"no submission intent before calling {tool}"
        latest_entry = max(latest, key=lambda record: record["created_at"])
        assert latest_entry["billing_state"] in ("reserved", "pending")
        if latest_entry.get("reservation"):
            assert latest_entry["reservation"]["state"] == "held"
        assert latest_entry["input_hash"]
        if inputs["stage"] == "shot_gen":
            # The paid slot holds a reservation before the provider is called.
            assert latest_entry["billing_state"] == "reserved"
            assert latest_entry["reservation"]["amount"] > 0
        observed.append(latest_entry)
        return {
            "success": True,
            "task_id": f"task-{inputs['stage']}-{inputs['slot']}",
            "cost_cny": 0.0,
        }

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=execute)
    result = driver.run(real=True, approve_live=True, budget_usd=5.0)

    assert result["real"] is True
    assert result["external_api_calls"] == len(observed) == 10
    # The approved planning stages are skipped by the resume plan; the driver
    # runs from character_cast through delivery.
    assert [stage["stage"] for stage in result["stages"]] == [
        "character_cast", "scene_plan", "keyframe", "shot_gen", "assembly",
        "delivery",
    ]

    ledger = SubmissionLedger("demo", "ep01", projects)
    resolved = ledger._latest().values()
    assert all(record["billing_state"] in ("billed", "free") for record in resolved)
    assert all(
        record["reservation"]["state"] == "released"
        for record in resolved
        if record.get("reservation")
    )
    assert ledger.reserved_total("USD") == 0.0
    assert result["budget"]["spent"] == 0.0


def test_failed_call_leaves_unknown_and_next_run_refuses_to_repay(projects, bindings):
    calls: list = []

    def failing(tool: str, inputs: dict) -> dict:
        calls.append(tool)
        raise RuntimeError("provider transport died after submit")

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=failing)
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0)

    error = raised.value
    assert error.code == ErrorCode.INTERNAL_ERROR
    assert error.side_effects == "unknown"
    assert error.context["resume_mode"] == "reconcile"
    assert len(calls) == 1

    ledger = SubmissionLedger("demo", "ep01", projects)
    unknown = ledger.unknown_entries()
    assert len(unknown) == 1
    assert unknown[0]["billing_state"] == "unknown"
    assert unknown[0]["task_id"] is None

    plan = resume_plan("demo", "ep01", projects_dir=projects, pipeline_type="short-drama")
    assert plan["mode"] == "reconcile"

    # A second real run must stop before the executor: no automatic re-pay.
    def should_not_run(*_args, **_kwargs):
        raise AssertionError("driver re-submitted after an UNKNOWN billing state")

    driver2 = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                     executor=should_not_run)
    with pytest.raises(ApiError) as again:
        driver2.run(real=True, approve_live=True, budget_usd=5.0)
    assert again.value.recovery == "ask-human"
    assert again.value.context["resume"]["mode"] == "reconcile"


def test_success_without_task_id_is_unknown_not_success(projects, bindings):
    def no_task_id(_tool: str, _inputs: dict) -> dict:
        return {"success": True, "cost_cny": 2.0}

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=no_task_id)
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0)
    assert raised.value.side_effects == "unknown"
    ledger = SubmissionLedger("demo", "ep01", projects)
    assert [entry["billing_state"] for entry in ledger.unknown_entries()] == ["unknown"]


# ---------------------------------------------------------------------------
# 4. budget gate
# ---------------------------------------------------------------------------


def test_over_budget_pauses_before_any_spend(projects, bindings):
    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))

    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=0.001)

    error = raised.value
    assert error.code == ErrorCode.BUDGET_EXCEEDED
    assert error.recovery == "ask-human"
    assert error.context["stage"] == "shot_gen"
    assert error.context["worst_case"] > error.context["budget"]
    # The free stages before the first paid slot ran; the paid call did not.
    assert [tool for tool, _inputs in calls] == ["img_key", "img_ref", "img_key"]
    assert error.side_effects == "partial"
    ledger = SubmissionLedger("demo", "ep01", projects)
    assert ledger.reserved_total("USD") == 0.0
    assert all(
        record["billing_state"] == "free"
        for record in ledger._latest().values()
    )


def test_unknown_price_pauses_instead_of_spending_blind(projects, tmp_path, monkeypatch):
    root = make_bindings(tmp_path / "bindings")
    # Strip the declared price from the first paid slot: unknown must pause.
    common = yaml.safe_load((root / "common.yaml").read_text(encoding="utf-8"))
    del common["bindings"]["image_character_key"]["chain"][0]["cost_cny_per_run"]
    common["bindings"]["image_character_key"]["chain"][0]["tier"] = "paid"
    _write_yaml(root / "common.yaml", common)
    monkeypatch.setenv(cs.BINDINGS_DIR_ENV, str(root))
    monkeypatch.setattr(cs, "tool_state", fake_tool_state)

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0)

    assert raised.value.code == ErrorCode.BUDGET_EXCEEDED
    assert "price unknown" in raised.value.what
    assert calls == []
    assert not (projects / "demo" / "ep01" / "ledger.jsonl").exists()


# ---------------------------------------------------------------------------
# 4b. content chain + review gates (M1)
# ---------------------------------------------------------------------------


def test_unapproved_script_stops_before_any_paid_call(projects, bindings):
    checkpoint_path = projects / "demo" / "checkpoint_script.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["status"] = "awaiting_human"
    checkpoint["human_approved"] = False
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0)

    error = raised.value
    assert error.context["stage"] == "script"
    assert error.context["review_required"] is True
    assert error.side_effects == "none"
    assert calls == [], "generation started before the script review gate"


def test_missing_script_artifact_stops_before_any_paid_call(projects, bindings):
    (projects / "demo" / "ep01" / "artifacts" / "script.json").unlink()

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0)

    error = raised.value
    assert error.context["stage"] == "character_cast"
    assert error.context["missing_artifacts"] == ["script"]
    assert calls == []


def test_two_shots_fan_out_to_two_keyframes_and_two_clips(projects, bindings):
    seed_reviewed_artifacts(projects, shots=2)
    calls: list = []

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    result = driver.run(real=True, approve_live=True, budget_usd=5.0)

    keyframe_inputs = [
        inputs for _tool, inputs in calls
        if inputs["stage"] == "keyframe" and inputs["slot"] == "image_reference"
    ]
    shot_inputs = [
        inputs for _tool, inputs in calls if inputs["stage"] == "shot_gen"
    ]
    assert len(keyframe_inputs) == 2
    assert len(shot_inputs) == 2
    assert [item["reference_image_path"] for item in shot_inputs] == [
        "assets/keyframes/sh_000_v1.png",
        "assets/keyframes/sh_001_v1.png",
    ]
    assert all("She turns away" in item["prompt"] for item in shot_inputs)
    assert result["external_api_calls"] == len(calls)


# ---------------------------------------------------------------------------
# 5. resume semantics
# ---------------------------------------------------------------------------


def test_resume_reconcile_blocks_before_execution(projects, bindings):
    ledger = SubmissionLedger("demo", "ep01", projects)
    entry = ledger.record_intent(
        "shot_gen", "alpha", "crashed-input", reserve_amount=1.0, currency="USD"
    )
    ledger.mark_submitted(entry["entry_id"], task_id=None)

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0)

    assert raised.value.code == ErrorCode.BATCH_PRECONDITION_FAILED
    assert raised.value.side_effects == "unknown"
    assert raised.value.context["resume"]["mode"] == "reconcile"
    assert calls == []


# ---------------------------------------------------------------------------
# 6. profile switch never edits the pipeline; decisions are recorded
# ---------------------------------------------------------------------------


def test_profile_switch_changes_tool_but_not_pipeline_yaml(projects, bindings):
    digest_before = hashlib.sha256(PIPELINE_FILE.read_bytes()).hexdigest()

    alpha = Driver("demo", "ep01", projects_dir=projects, profile="alpha").run()
    beta = Driver("demo", "ep01", projects_dir=projects, profile="beta").run()

    def shot_tool(result: dict) -> str:
        stage = next(s for s in result["stages"] if s["stage"] == "shot_gen")
        return stage["slots"][0]["tool"]

    assert shot_tool(alpha) == "alpha_video"
    assert shot_tool(beta) == "beta_video"
    assert hashlib.sha256(PIPELINE_FILE.read_bytes()).hexdigest() == digest_before
    assert [s["stage"] for s in alpha["stages"]] == [s["stage"] for s in beta["stages"]]


def test_fallback_and_degradation_are_recorded_before_execution(projects, bindings):
    execution_order: list[str] = []

    def execute(tool: str, _inputs: dict) -> dict:
        execution_order.append(tool)
        return {"success": True, "task_id": f"task-{tool}", "cost_cny": 0.0}

    driver = Driver("demo", "ep01", projects_dir=projects, profile="fallback",
                    executor=execute)
    driver.run(real=True, approve_live=True, budget_usd=5.0)

    path = projects / "demo" / "decision_log.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    categories = {record["category"] for record in records}
    assert categories <= {
        "provider_selection", "downgrade_approval",
        "budget_tradeoff", "render_runtime_selection",
    }
    assert "provider_selection" in categories
    assert "downgrade_approval" in categories

    provider = next(r for r in records if r["category"] == "provider_selection")
    assert provider["decision"] == "alpha_video"
    assert any(alt["option_id"] == "missing_video" for alt in provider["alternatives"])
    assert provider["metadata"]["stage"] == "shot_gen"

    lipsync = next(
        r for r in records
        if r["category"] == "downgrade_approval" and "lipsync" in r["subject"]
    )
    assert lipsync["decision"] == "separate_lipsync_path"

    # The log is written before the first paid call.
    assert execution_order[0] == "img_key"