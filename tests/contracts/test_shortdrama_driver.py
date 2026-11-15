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
from shortdrama.driver import Driver, classify_provider_error
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

    def catalog_entry(model: str) -> dict:
        return {
            "model": model,
            "operation": None,
            "lipsync_from_dialogue": False,
            "reference_audio": False,
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "min_duration_s": 1,
            "max_duration_s": 30,
            "output_resolutions": ["480p", "720p", "1080p"],
            "verification_status": "VERIFIED",
            "commercial_rights": "test fixture",
        }

    catalog_entries = []
    for model in ("test/alpha", "test/beta", "test/gone"):
        for operation in ("image_to_video", "text_to_video"):
            entry = catalog_entry(model)
            entry["operation"] = operation
            catalog_entries.append(entry)
    _write_yaml(root / "catalog" / "test.yaml", {"version": "1.0", "entries": catalog_entries})

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

    keyframes_dir = projects / "demo" / "ep01" / "assets" / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    for index in range(shots):
        (keyframes_dir / f"sh_{index:03d}_v1.png").write_bytes(b"fake-png")

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
    assert next(x for x in by_stage["shot_gen"]["slots"] if x["slot"]=="video_primary")["tool"] == "alpha_video"
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
    # No ledger or spend artifact is created by the refused run.
    assert not (projects / "demo" / "ep01" / "ledger.jsonl").exists()
    assets = projects / "demo" / "ep01" / "assets"
    assert sorted(item.name for item in assets.iterdir()) == ["keyframes"]


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
    # The whole-run estimate is checked before the first call, so a shortfall
    # is reported instead of being discovered after several paid shots.
    assert error.context["budget"] == pytest.approx(0.001)
    assert error.context["estimate"]["worst_case_usd"] > error.context["budget"]
    assert calls == []
    assert error.side_effects == "none"
    ledger = SubmissionLedger("demo", "ep01", projects)
    assert ledger.reserved_total("USD") == 0.0
    assert ledger._latest() == {}


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
        return next(x for x in stage["slots"] if x["slot"]=="video_primary")["tool"]

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

# ---------------------------------------------------------------------------
# 9. review gate: a generation stage stops the run until a human approves
# ---------------------------------------------------------------------------


def artifact_executor(calls: list) -> callable:
    """Executor that reports the tool's output file like the live registry."""

    def execute(tool: str, inputs: dict) -> dict:
        calls.append((tool, inputs))
        output = inputs.get("output_path")
        return {
            "success": True,
            "task_id": f"task-{inputs['stage']}-{inputs['slot']}",
            "cost_cny": 0.0,
            "artifacts": [output] if output else [],
        }

    return execute


def test_pause_for_review_writes_awaiting_human_and_stops(projects, bindings):
    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects,
                    executor=artifact_executor(calls))

    result = driver.run(real=True, approve_live=True, budget_usd=5.0,
                        pause_for_review=True)

    assert result["status"] == "waiting-for-review"
    assert result["waiting_for"] == "character_cast"
    assert [inputs["stage"] for _, inputs in calls] == ["character_cast"]

    checkpoint = json.loads(
        (projects / "demo" / "checkpoint_character_cast.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "awaiting_human"
    assert checkpoint["human_approved"] is False
    generated = checkpoint["artifacts"]["generated"]
    assert generated["image_character_key"], "generated files must be reviewable"
    assert checkpoint["cost_snapshot"]["spent"] >= 0


def test_awaiting_review_stage_is_never_re_executed(projects, bindings):
    calls: list = []
    Driver("demo", "ep01", projects_dir=projects,
           executor=artifact_executor(calls)).run(
        real=True, approve_live=True, budget_usd=5.0, pause_for_review=True
    )
    calls.clear()

    again = Driver("demo", "ep01", projects_dir=projects,
                   executor=artifact_executor(calls)).run(
        real=True, approve_live=True, budget_usd=5.0, pause_for_review=True
    )

    assert again["status"] == "waiting-for-review"
    assert again["waiting_for"] == "character_cast"
    assert calls == [], "an unreviewed stage must not be paid for twice"


def test_approving_the_checkpoint_resumes_at_the_next_generation_stage(projects, bindings):
    calls: list = []
    Driver("demo", "ep01", projects_dir=projects,
           executor=artifact_executor(calls)).run(
        real=True, approve_live=True, budget_usd=5.0, pause_for_review=True
    )

    code = cli.main([
        "--projects-dir", str(projects),
        "checkpoint", "approve", "--project", "demo",
        "--stage", "character_cast", "--note", "key images match the cards", "--json",
    ])
    assert code == 0
    checkpoint = json.loads(
        (projects / "demo" / "checkpoint_character_cast.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "completed"
    assert checkpoint["human_approved"] is True
    assert checkpoint["metadata"]["approved_note"] == "key images match the cards"

    calls.clear()
    result = Driver("demo", "ep01", projects_dir=projects,
                    executor=artifact_executor(calls)).run(
        real=True, approve_live=True, budget_usd=5.0, pause_for_review=True
    )
    assert result["status"] == "waiting-for-review"
    assert result["waiting_for"] == "keyframe"
    # keyframe runs both of its declared slots; the previous stage is not re-run.
    assert {inputs["stage"] for _, inputs in calls} == {"keyframe"}
    assert {inputs["slot"] for _, inputs in calls} == {
        "image_reference", "image_character_key",
    }


def test_checkpoint_approve_rejects_unknown_stage(projects, capsys):
    code = cli.main([
        "--projects-dir", str(projects),
        "checkpoint", "approve", "--project", "demo",
        "--stage", "character_cast", "--json",
    ])
    assert code == 2
    assert json.loads(capsys.readouterr().err)["code"] == ErrorCode.VALIDATION_ERROR.value


def test_ledger_reconcile_unblocks_the_next_real_run(projects, bindings):
    def failing(_tool: str, _inputs: dict) -> dict:
        raise RuntimeError("provider transport died after submit")

    with pytest.raises(ApiError):
        Driver("demo", "ep01", projects_dir=projects, profile="alpha",
               executor=failing).run(real=True, approve_live=True, budget_usd=5.0)
    entry_id = SubmissionLedger("demo", "ep01", projects).unknown_entries()[0]["entry_id"]

    code = cli.main([
        "--projects-dir", str(projects), "ledger", "reconcile",
        "--project", "demo", "--episode", "ep01",
        "--entry-id", entry_id, "--state", "free",
        "--note", "provider console: request rejected, no charge", "--json",
    ])
    assert code == 0
    plan = resume_plan("demo", "ep01", projects_dir=projects, pipeline_type="short-drama")
    assert plan["mode"] == "run"

    calls: list = []
    Driver("demo", "ep01", projects_dir=projects, profile="alpha",
           executor=ok_executor(calls)).run(real=True, approve_live=True, budget_usd=5.0)
    assert calls, "the reconciled run must be able to proceed"


def test_ledger_reconcile_refuses_non_unknown_entries(projects, bindings, capsys):
    calls: list = []
    Driver("demo", "ep01", projects_dir=projects, profile="alpha",
           executor=ok_executor(calls)).run(real=True, approve_live=True, budget_usd=5.0)
    resolved = [
        entry for entry in SubmissionLedger("demo", "ep01", projects)._latest().values()
        if entry["billing_state"] in ("billed", "free")
    ][0]

    code = cli.main([
        "--projects-dir", str(projects), "ledger", "reconcile",
        "--project", "demo", "--episode", "ep01",
        "--entry-id", resolved["entry_id"], "--state", "free", "--json",
    ])
    assert code == 2
    assert json.loads(capsys.readouterr().err)["code"] == ErrorCode.VALIDATION_ERROR.value


def test_start_at_shot_gen_without_keyframes_uses_text_to_video(projects, bindings):
    (projects / "demo" / "ep01" / "artifacts" / "asset_manifest.json").unlink()
    _approved_checkpoint(projects, "character_cast", {})
    _approved_checkpoint(projects, "keyframe", {})

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls), keyframes_required=False)
    result = driver.run(real=True, approve_live=True, budget_usd=5.0,
                        pause_for_review=True, start_at="shot_gen")

    assert result["waiting_for"] == "shot_gen"
    assert [inputs["stage"] for _, inputs in calls] == ["shot_gen"]
    assert calls[0][1]["operation"] == "text_to_video"
    assert calls[0][1]["ratio"] == "9:16"


def test_driver_does_not_send_the_catalog_model_id_as_a_tool_input(projects, bindings):
    """``dashscope/wan3.0-video`` is a catalog id; providers reject it (400)."""
    calls: list = []
    Driver("demo", "ep01", projects_dir=projects, profile="alpha",
           executor=ok_executor(calls)).run(
        real=True, approve_live=True, budget_usd=5.0)
    assert calls
    for _, inputs in calls:
        if "model" in inputs:
            assert "/" not in str(inputs["model"])


def test_resuming_a_paused_stage_reuses_completed_items(projects, bindings):
    """A half-done stage must not re-pay for items already on disk."""
    calls: list = []

    def writing_executor(tool: str, inputs: dict) -> dict:
        calls.append((tool, inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-clip")
        return {"success": True, "task_id": f"task-{len(calls)}", "cost_cny": 0.1,
                "artifacts": [str(output)]}

    Driver("demo", "ep01", projects_dir=projects, profile="alpha",
           executor=writing_executor).run(
        real=True, approve_live=True, budget_usd=5.0, start_at="shot_gen",
        stop_after="shot_gen")
    assert len(calls) == 1

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("re-executed an item that already succeeded")

    result = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=should_not_run).run(
        real=True, approve_live=True, budget_usd=5.0, start_at="shot_gen",
        stop_after="shot_gen")
    shot_gen = next(s for s in result["stages"] if s["stage"] == "shot_gen")
    video = next(x for x in shot_gen["slots"] if x["slot"]=="video_primary")
    assert video["items"][0]["reused"] is True
    assert video["items"][0]["artifacts"]


def test_synchronous_tool_needs_no_task_id(projects, bindings, monkeypatch):
    """Image APIs return their receipt inline; a missing task id is not UNKNOWN."""
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor([]))
    monkeypatch.setattr(driver, "_requires_task_id", lambda tool: False)
    result = driver.run(real=True, approve_live=True, budget_usd=5.0,
                        stop_after="shot_gen")
    shot_gen = next(s for s in result["stages"] if s["stage"] == "shot_gen")
    video = next(x for x in shot_gen["slots"] if x["slot"]=="video_primary")
    assert video["items"][0]["billing_state"] in ("billed", "free")


def test_continuity_gate_blocks_unknown_character_before_spend(projects, bindings):
    plan_path = projects / "demo" / "ep01" / "artifacts" / "scene_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["shots"][0]["reference_character_ids"] = ["ch_ghost"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0,
                   start_at="shot_gen")
    assert raised.value.code == ErrorCode.BATCH_PRECONDITION_FAILED
    assert raised.value.context["continuity_errors"]
    assert calls == [], "continuity gate must fire before any provider call"


def test_model_contract_gate_blocks_before_any_provider_call(
    projects, bindings, monkeypatch
):
    monkeypatch.setattr(
        cs, "catalog_entry",
        lambda model, operation: (
            {
                "model": model, "operation": operation,
                "min_duration_s": 2, "max_duration_s": 30,
                "aspect_ratios": ["9:16", "16:9", "1:1"],
                "output_resolutions": ["480p", "720p", "1080p"],
                "lipsync_from_dialogue": True, "reference_audio": True,
            }
            if model == "dashscope/wan3.0-video" else None
        ),
    )
    plan_path = projects / "demo" / "ep01" / "artifacts" / "scene_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["shots"][0]["duration_s"] = 1  # below wan3.0's 2s minimum
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls))
    # Emulate the real binding: a catalog-declared model with a known contract.
    driver._cost_row = lambda slot: {
        "tool": "alpha_video", "provider": "dashscope",
        "model": "dashscope/wan3.0-video", "tool_model": "wan3.0-video",
        "fallback_models": None, "estimated_cny": 0.0, "cost_known": True,
    }
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=5.0,
                   start_at="shot_gen")
    assert raised.value.code == ErrorCode.BATCH_PRECONDITION_FAILED
    assert raised.value.context["contract_errors"]
    assert any("below the model minimum" in e
               for e in raised.value.context["contract_errors"])
    assert calls == [], "contract gate must fire before any provider call"


def test_shot_gen_visual_gate_blocks_when_verdict_is_missing(projects, bindings):
    # No executor -> production path, where the visual gate is armed.
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError) as raised:
        driver._preflight_stage("shot_gen")
    assert raised.value.code == ErrorCode.BATCH_PRECONDITION_FAILED
    assert raised.value.context["visual_review_errors"]
    assert "visual review not run" in raised.value.context["visual_review_errors"][0]


def test_shot_gen_visual_gate_passes_with_a_clean_verdict(projects, bindings):
    artifacts = projects / "demo" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "qa_visual.json").write_text(
        json.dumps({"verdict": "pass", "blocking": [], "warnings": []}),
        encoding="utf-8",
    )
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    driver._preflight_stage("shot_gen")  # must not raise


def test_cost_preview_prices_each_pending_item(projects, bindings):
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor([]))
    preview = driver.cost_preview(["shot_gen", "assembly"])

    video = next(row for row in preview["rows"]
                 if row["stage"] == "shot_gen" and row["slot"] == "video_primary")
    assert video["item_count"] == 1
    assert video["estimated_cny"] == pytest.approx(2.0)
    assert preview["estimated_cny"] >= 2.0
    # Worst case carries the declared retry allowance.
    assert preview["worst_case_cny"] == pytest.approx(
        preview["estimated_cny"] * (1 + driver.retries)
    )


def test_cost_preview_prices_reused_items_at_zero(projects, bindings):
    def writing_executor(tool: str, inputs: dict) -> dict:
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-clip")
        return {"success": True, "task_id": f"task-{output.name}", "cost_cny": 0.1,
                "artifacts": [str(output)]}

    Driver("demo", "ep01", projects_dir=projects, profile="alpha",
           executor=writing_executor).run(
        real=True, approve_live=True, budget_usd=50.0, start_at="shot_gen",
        stop_after="shot_gen")

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor([]))
    preview = driver.cost_preview(["shot_gen"])
    video = next(row for row in preview["rows"] if row["slot"] == "video_primary")
    assert video["items"][0]["reused"] is True
    assert video["items"][0]["estimated_cny"] == 0.0
    assert video["estimated_cny"] == 0.0


def test_per_item_cost_ceiling_blocks_without_spending(projects, bindings):
    calls: list = []
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor(calls), max_item_cost_cny=0.5)
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    assert raised.value.code == ErrorCode.BUDGET_EXCEEDED
    assert raised.value.context["max_item_cost_cny"] == pytest.approx(0.5)
    assert calls == []


def test_content_error_is_remembered_and_never_retried_unchanged(projects, bindings):
    calls: list = []

    def rejecting_executor(tool: str, inputs: dict) -> dict:
        calls.append((tool, inputs))
        raise RuntimeError("InvalidParameter: duration out of range")

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=rejecting_executor)
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    assert raised.value.context["error_kind"] == "content"
    assert raised.value.recovery == "do-X-first"
    assert len(calls) == 1
    memo = projects / "demo" / "ep01" / "failed_inputs.json"
    assert memo.is_file()

    # A content rejection never produced a billable task, but the ledger only
    # knows the submission failed; reconcile it as free before resuming.
    ledger = SubmissionLedger("demo", "ep01", projects)
    for entry in ledger.unknown_entries():
        ledger.mark_result(entry["entry_id"], "free")

    calls.clear()
    with pytest.raises(ApiError) as second:
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    assert second.value.code == ErrorCode.BATCH_PRECONDITION_FAILED
    assert second.value.context["prior_error"]
    assert calls == [], "a content-rejected input must not be re-submitted"


def test_quota_error_is_not_memoized_as_a_content_error(projects, bindings):
    def quota_executor(tool: str, inputs: dict) -> dict:
        return {"success": False,
                "error": "AllocationQuota.FreeTierOnly: free quota exhausted",
                "cost_cny": 0.0}

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=quota_executor)
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    assert raised.value.context["error_kind"] == "quota"
    assert not (projects / "demo" / "ep01" / "failed_inputs.json").exists()


def test_classify_provider_error():
    assert classify_provider_error("InvalidParameter: bad ratio") == "content"
    assert classify_provider_error("Arrearage: account in arrears") == "quota"
    assert classify_provider_error("ReadTimeout") == "transient"
    assert classify_provider_error("something odd") == "unknown"


def test_switching_model_releases_the_content_memo(projects, bindings):
    calls: list = []

    def rejecting_executor(tool: str, inputs: dict) -> dict:
        calls.append((tool, inputs))
        raise RuntimeError("InvalidParameter: duration out of range")

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=rejecting_executor)
    with pytest.raises(ApiError):
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    ledger = SubmissionLedger("demo", "ep01", projects)
    for entry in ledger.unknown_entries():
        ledger.mark_result(entry["entry_id"], "free")

    # Same tool, different model: the memo must not pin the new binding.
    calls.clear()
    driver._cost_row = lambda slot: {
        "tool": "alpha_video", "provider": "alpha", "model": None,
        "tool_model": "another-model", "estimated_cny": 2.0, "cost_known": True,
    }
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    assert raised.value.context.get("error_kind") == "content"
    assert len(calls) == 1, "a model change must release the memo"


def test_config_error_is_not_memoized(projects, bindings):
    def missing_key_executor(tool: str, inputs: dict) -> dict:
        return {"success": False, "error": "DASHSCOPE_API_KEY not set.",
                "cost_cny": 0.0}

    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=missing_key_executor)
    with pytest.raises(ApiError) as raised:
        driver.run(real=True, approve_live=True, budget_usd=50.0,
                   start_at="shot_gen", stop_after="shot_gen")
    assert raised.value.context["error_kind"] == "config"
    assert not (projects / "demo" / "ep01" / "failed_inputs.json").exists()
