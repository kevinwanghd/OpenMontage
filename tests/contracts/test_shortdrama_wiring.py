"""Wiring contract tests: driver/CLI actually enforce the v2.5 gates.

Library-level contracts live in their own test files; these pin the runtime
wiring: D19 startup gate (W1), director gates before paid stages (W5), the
music license gate at delivery (W3).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from lib.review import ReviewDecision, ReviewItem, ReviewStore
from shortdrama.driver import Driver
from shortdrama.errors import ApiError, ErrorCode

from tests.contracts.test_shortdrama_driver import ok_executor, seed_reviewed_artifacts


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    seed_reviewed_artifacts(root)
    return root


@pytest.fixture
def bindings(tmp_path, monkeypatch):
    from lib import capability_slots as cs
    from tests.contracts.test_shortdrama_driver import fake_tool_state, make_bindings

    root = make_bindings(tmp_path / "bindings")
    monkeypatch.setenv(cs.BINDINGS_DIR_ENV, str(root))
    monkeypatch.setattr(cs, "tool_state", fake_tool_state)
    return root


def _invalid_agent_models(tmp_path: Path) -> Path:
    roles = {
        role: {
            "producer": {"model": "m-a", "family": "deepseek"},
            "critic": {"model": "m-b", "family": "deepseek"},
        }
        for role in (
            "script", "visual", "shot", "edit_sound", "director",
        )
    }
    path = tmp_path / "agent_models.yaml"
    path.write_text(yaml.safe_dump({"version": "1.0", "roles": roles}), encoding="utf-8")
    return path


def test_w1_same_family_config_blocks_the_driver(projects, bindings, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODELS_PATH", str(_invalid_agent_models(tmp_path)))
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError) as raised:
        driver._preflight_stage("shot_gen")
    error = raised.value
    assert error.code == ErrorCode.BATCH_PRECONDITION_FAILED
    assert "agent model" in str(error.message if hasattr(error, "message") else error)


def test_w5_missing_director_gate_blocks_shot_gen(projects, bindings):
    (projects / "demo" / "ep01" / "reviews.jsonl").unlink()
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError) as raised:
        driver._preflight_stage("shot_gen")
    assert "script_shot" in str(raised.value)


def test_w5_seeded_gate_lets_shot_gen_pass(projects, bindings):
    # A fake executor keeps the keyframe visual gate off; the director gate is
    # what this test pins.
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha",
                    executor=ok_executor([]))
    driver._preflight_stage("shot_gen")  # must not raise


def test_w5_plan_reports_all_four_gates(projects, bindings):
    plan = Driver("demo", "ep01", projects_dir=projects, profile="alpha").plan()
    gates = plan["director_gates"]
    assert set(gates) == {"brief", "script_shot", "assembly", "delivery"}
    assert gates["script_shot"] is True and gates["assembly"] is True
    assert gates["delivery"] is False  # full_watch not recorded yet


def _edit_decisions_with_music(asset_id: str) -> dict:
    return {
        "version": "1.0",
        "audio": {"music": {"asset_id": asset_id, "volume": 0.3}},
    }


def _library(*tracks: dict) -> Path:
    return {"version": 1, "tracks": list(tracks)}


def _registered_track(file="track.mp3"):
    return {
        "file": file,
        "title": "Track",
        "artist": "Artist",
        "source": "archive.org",
        "license": "CC0 1.0",
        "commercial_use": True,
        "attribution_required": False,
        "url": "https://example.org/1",
        "downloaded": "2026-11-16",
    }


def test_w3_unregistered_music_blocks_delivery(projects, bindings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "music_library").mkdir()
    (tmp_path / "music_library" / "licenses.json").write_text(
        json.dumps(_library(_registered_track())), encoding="utf-8"
    )
    artifacts = projects / "demo" / "ep01" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "edit_decisions.json").write_text(
        json.dumps(_edit_decisions_with_music("unknown.mp3")), encoding="utf-8"
    )
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError) as raised:
        driver._preflight_stage("delivery")
    assert "not registered" in str(raised.value)


def test_w3_missing_library_blocks_delivery_with_music(projects, bindings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    artifacts = projects / "demo" / "ep01" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "edit_decisions.json").write_text(
        json.dumps(_edit_decisions_with_music("track.mp3")), encoding="utf-8"
    )
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError) as raised:
        driver._preflight_stage("delivery")
    assert "music library" in str(raised.value).lower()


def test_w3_registered_commercial_music_passes(projects, bindings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "music_library").mkdir()
    (tmp_path / "music_library" / "licenses.json").write_text(
        json.dumps(_library(_registered_track())), encoding="utf-8"
    )
    artifacts = projects / "demo" / "ep01" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "edit_decisions.json").write_text(
        json.dumps(_edit_decisions_with_music("track.mp3")), encoding="utf-8"
    )
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    driver._preflight_stage("delivery")  # must not raise


def test_w3_attribution_required_without_credit_blocks(projects, bindings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    track = _registered_track()
    track["attribution_required"] = True
    (tmp_path / "music_library").mkdir()
    (tmp_path / "music_library" / "licenses.json").write_text(
        json.dumps(_library(track)), encoding="utf-8"
    )
    artifacts = projects / "demo" / "ep01" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "edit_decisions.json").write_text(
        json.dumps(_edit_decisions_with_music("track.mp3")), encoding="utf-8"
    )
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError) as raised:
        driver._preflight_stage("delivery")
    assert "attribution" in str(raised.value)


def test_w3_no_music_referenced_never_blocks(projects, bindings):
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    driver._preflight_stage("delivery")  # seeded edit_decisions has no music


def test_w5_creative_reject_does_not_close_gate(projects, bindings):
    # Replace the seeded assembly approval with a creative REJECT: the gate
    # must reopen and block the delivery stage.
    store = ReviewStore("demo", "ep01", projects_dir=projects)
    store.undo("gate_assembly", reason="recheck")
    store.decide("gate_assembly", ReviewDecision(action="accept", gate_type="technical"))
    store.decide(
        "gate_assembly",
        ReviewDecision(action="reject", reason_code="PACING", gate_type="creative"),
    )
    driver = Driver("demo", "ep01", projects_dir=projects, profile="alpha")
    with pytest.raises(ApiError):
        driver._preflight_stage("delivery")