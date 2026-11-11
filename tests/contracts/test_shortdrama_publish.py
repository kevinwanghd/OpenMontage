"""WP-AB contract tests: DEL-02/03/04/05/06 (publish pipeline, offline).

Everything is machine-checkable and offline: export metadata is supplied as
data, the 90s variant is derived from the declared beat-map scaling function,
compliance evidence/confirmations are supplied records, batch provider results
are fixture data, and publish metadata is plain JSON. No renderer, platform API
or paid call runs here. Tests assert that UNKNOWN never masquerades as PASS.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import beat_map
from lib.batch import (
    BatchError,
    BatchLock,
    BatchRunner,
    COMPLETED,
    PAUSED_QUOTA,
    QuotaAccount,
    SingleWriterViolation,
    WAITING_PROVIDER,
)
from lib.compliance import (
    BLOCKED,
    COMPLIANCE_ITEMS,
    ComplianceError,
    HUMAN_CONFIRMED,
    HUMAN_REJECTED,
    NEEDS_EVIDENCE,
    PENDING_HUMAN,
    confirm_item,
    screen_compliance,
)
from lib.export_presets import (
    CREATIVE_VARIANT_DURATION_S,
    PLATFORM_EXPORT_PRESETS,
    TARGET_PLATFORMS,
    ExportError,
    build_duration_variant,
    build_variants,
    evaluate_duration_variant,
    evaluate_export,
)
from lib.publish_metadata import (
    STATUS_BLOCKED,
    STATUS_INCOMPLETE,
    STATUS_READY,
    build_episode_packages,
    build_publish_package,
    build_release_schedule,
    load_publish_manifest,
    save_publish_manifest,
)

ROOT = Path(__file__).resolve().parents[2]

VALID_METADATA = {
    "width": 1080,
    "height": 1920,
    "duration_seconds": 90.0,
    "fps": 30,
    "video_codec": "h264",
    "audio_codec": "aac",
    "has_audio": True,
    "container": "mp4",
}


def _metadata(**overrides):
    document = dict(VALID_METADATA)
    document.update(overrides)
    return document


def _cover(**overrides):
    document = {"width": 1080, "height": 1920, "path": "covers/ep01.jpg", "frame_time_s": 1.5}
    document.update(overrides)
    return document


def _compliance_document(**overrides):
    document = {
        "assets": [
            {"id": "bg1", "license": "licensed", "rights_source": "artlist-2026-11"},
            {"id": "voice1", "license": "own", "rights_source": "in-house recording"},
        ],
        "rating": "teen",
        "sensitive_scan": {
            "scanned": True,
            "flags": [
                {"category": "violence", "match": False},
                {"category": "adult", "match": False},
            ],
        },
        "ai_disclosure": {
            "statement": "Contains AI-generated imagery and voice.",
            "platform_setting": "enabled",
        },
    }
    document.update(overrides)
    return document


def _confirm_all():
    confirmations: dict[str, dict] = {}
    for item in COMPLIANCE_ITEMS:
        confirm_item(
            confirmations,
            item,
            reviewer="reviewer@example.com",
            reviewed_at="2026-11-10T09:00:00+00:00",
            evidence=f"{item} checked against platform policy snapshot",
        )
    return confirmations


def _plan():
    return {
        "episodes": [
            {
                "episode_id": "ep01",
                "steps": [
                    {"step_id": "script", "units": 1.0},
                    {"step_id": "video", "units": 3.0, "provider": "async"},
                    {"step_id": "compose", "units": 2.0},
                ],
            },
            {
                "episode_id": "ep02",
                "steps": [
                    {"step_id": "script", "units": 1.0},
                    {"step_id": "video", "units": 3.0, "provider": "async"},
                ],
            },
        ]
    }


# --- DEL-02: three platform presets -----------------------------------------


def test_three_platform_presets_are_declared_and_versioned():
    assert TARGET_PLATFORMS == ("tiktok", "youtube_shorts", "instagram_reels")
    versions = set()
    required = {
        "preset_version",
        "width",
        "height",
        "aspect_ratio",
        "duration_seconds",
        "fps_range",
        "video_codecs",
        "audio_codecs",
        "containers",
        "cover",
        "safe_area",
        "title_max_chars",
    }
    for platform, preset in PLATFORM_EXPORT_PRESETS.items():
        assert preset["platform"] == platform
        assert required <= set(preset)
        assert preset["aspect_ratio"] == "9:16"
        assert preset["cover"]["aspect_ratio"] == "9:16"
        assert preset["duration_seconds"]["max"] >= preset["duration_seconds"]["min"]
        assert preset["safe_area"]["content_rect"]["width"] > 0
        versions.add(preset["preset_version"])
    assert len(versions) == 3
    # Official platform rules are engineering defaults pending confirmation.
    assert PLATFORM_EXPORT_PRESETS["tiktok"]["calibrated"] is False


@pytest.mark.parametrize("platform", TARGET_PLATFORMS)
def test_export_compliance_passes_for_compliant_supplied_metadata(platform):
    report = evaluate_export(platform, _metadata(), cover=_cover())
    assert report.to_dict()["qc_gate"] == "PASS"
    assert report.ok is True
    assert report.status == "PASS"
    assert report.to_dict()["rule_verification"] == "UNCONFIRMED"


def test_export_unknown_facts_never_clear_the_qc_gate():
    report = evaluate_export(
        "tiktok",
        {"duration_seconds": 95.0, "width": 1080, "height": 1920},
        cover=None,
    )
    assert report.status == "FAIL"
    assert report.ok is False
    assert "fps" in report.unknowns
    assert "audio_stream" in {c.name for c in report.checks}


def test_duration_ceiling_differs_by_platform_and_never_truncates():
    master = build_variants({"duration_s": 120.0, "cover": _cover()})
    by_platform = {v["platform"]: v for v in master["variants"]}
    assert by_platform["tiktok"]["duration_ok"] is True
    assert by_platform["youtube_shorts"]["duration_ok"] is True
    reels = by_platform["instagram_reels"]
    assert reels["duration_ok"] is False
    assert reels["requires_reoptimization"] is True
    assert reels["truncated"] is False
    assert reels["recommended_duration_s"] == 90.0
    assert master["all_duration_ok"] is False


def test_safe_area_check_uses_each_platform_preset():
    # 1460..1520px sits inside the Reels content rect but below the TikTok and
    # Shorts content bottoms -- geometry is per-platform, not a copy.
    rect = {"x": 60, "y": 1460, "width": 700, "height": 60}
    reports = {
        platform: evaluate_export(
            platform,
            _metadata(),
            subtitle_rect=rect,
            cover=_cover(),
        )
        for platform in TARGET_PLATFORMS
    }
    assert reports["tiktok"].status == "FAIL"
    assert reports["youtube_shorts"].status == "FAIL"
    assert reports["instagram_reels"].status == "PASS"


def test_build_variants_rejects_master_without_duration():
    with pytest.raises(ExportError):
        build_variants({"title": "no duration"})


# --- DEL-03: 90s creative variant (re-built, not truncated) ------------------


def test_90s_variant_is_rebuilt_by_the_scaling_function():
    master = beat_map.build_beat_map(120.0)
    variant = build_duration_variant(master, CREATIVE_VARIANT_DURATION_S)
    document = variant["variant"]
    derivation = variant["derivation"]

    assert document["duration_s"] == 90.0
    assert beat_map.validate_beat_map(document) == []
    assert derivation["truncated"] is False
    assert derivation["scaling_rule"] == beat_map.SCALING_RULE_ID
    assert derivation["source_duration_s"] == 120.0
    assert derivation["target_duration_s"] == 90.0
    assert derivation["target_segment_ratios"] == list(beat_map.segment_ratios(90.0))
    assert derivation["target_segment_ratios"] != list(beat_map.segment_ratios(120.0))
    assert len(document["segments"]) == 7
    assert document["beats"] != master["beats"]
    assert (
        sum(1 for b in document["beats"] if b.get("hook_candidate"))
        == beat_map.min_hook_candidates(90.0)
    )
    assert derivation["human_confirmation_required"] is True
    assert derivation["human_confirmation"]["narrative_complete"] == "unconfirmed"
    assert derivation["human_confirmation"]["cliffhanger_preserved"] == "unconfirmed"


def test_duration_variant_machine_checks_all_three_presets():
    variant = build_duration_variant(beat_map.build_beat_map(120.0), 90.0)
    result = evaluate_duration_variant(
        variant, metadata=_metadata(duration_seconds=90.0), cover=_cover()
    )
    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["artifact_checked"] is True
    assert result["artifact_gate"] == "PASS"
    assert result["beat_map_problems"] == []
    assert {r["platform"] for r in result["variants"]} == set(TARGET_PLATFORMS)
    assert all(r["compliance"]["status"] == "PASS" for r in result["variants"])
    assert result["human_confirmation_required"] is True


def test_duration_variant_without_render_metadata_does_not_claim_pass():
    variant = build_duration_variant(beat_map.build_beat_map(120.0), 90.0)
    result = evaluate_duration_variant(variant)
    assert result["ok"] is True  # plan-level duration/structure only
    assert result["artifact_checked"] is False
    assert result["artifact_gate"] == "UNKNOWN"
    assert all(r["compliance"] is None for r in result["variants"])


def test_duration_variant_rejects_invalid_master():
    with pytest.raises(ExportError):
        build_duration_variant({"version": "1.0", "duration_s": 120.0}, 90.0)


# --- DEL-04: compliance screening -------------------------------------------


def test_machine_pass_without_human_confirmation_is_pending_human():
    report = screen_compliance(_compliance_document())
    assert report.status == PENDING_HUMAN
    assert report.publish_ready is False
    assert set(report.pending_human) == set(COMPLIANCE_ITEMS)


def test_missing_evidence_is_unknown_not_pass():
    report = screen_compliance({})
    assert report.status == NEEDS_EVIDENCE
    assert report.publish_ready is False
    for item in COMPLIANCE_ITEMS:
        assert report.item(item).machine_status == "UNKNOWN"
        assert report.item(item).machine_status != "PASS"
    payload = report.to_dict()
    assert payload["publish_ready"] is False
    assert len(payload["unknowns"]) == len(COMPLIANCE_ITEMS)


def test_machine_pass_plus_human_confirmation_clears_del04():
    report = screen_compliance(_compliance_document(), _confirm_all())
    assert report.status == "PASS"
    assert report.publish_ready is True


def test_unlicensed_asset_blocks_even_with_human_confirmation():
    document = _compliance_document(
        assets=[{"id": "clip7", "license": "unlicensed", "rights_source": ""}]
    )
    report = screen_compliance(document, _confirm_all())
    assert report.status == BLOCKED
    assert report.publish_ready is False
    assert report.item("copyright").machine_status == "FAIL"


def test_sensitive_content_hit_blocks():
    document = _compliance_document(
        sensitive_scan={"scanned": True, "flags": [{"category": "graphic_violence", "match": True}]}
    )
    report = screen_compliance(document, _confirm_all())
    assert report.status == BLOCKED
    assert "sensitive_content" in report.blockers[0]


def test_human_rejection_blocks():
    confirmations = _confirm_all()
    confirm_item(
        confirmations,
        "ai_disclosure",
        reviewer="reviewer@example.com",
        reviewed_at="2026-11-10T09:00:00+00:00",
        evidence="disclosure label wording is not acceptable",
        status=HUMAN_REJECTED,
    )
    report = screen_compliance(_compliance_document(), confirmations)
    assert report.status == BLOCKED
    assert report.item("ai_disclosure").human_status == HUMAN_REJECTED


def test_confirm_item_requires_reviewer_timestamp_and_evidence():
    confirmations: dict[str, dict] = {}
    with pytest.raises(ComplianceError):
        confirm_item(confirmations, "copyright", reviewer="", reviewed_at="2026-11-10", evidence="x")
    with pytest.raises(ComplianceError):
        confirm_item(confirmations, "copyright", reviewer="r", reviewed_at="not-a-date", evidence="x")
    with pytest.raises(ComplianceError):
        confirm_item(confirmations, "copyright", reviewer="r", reviewed_at="2026-11-10", evidence="")
    confirm_item(
        confirmations,
        "copyright",
        reviewer="r",
        reviewed_at="2026-11-10",
        evidence="licenses checked",
    )
    assert confirmations["copyright"]["status"] == HUMAN_CONFIRMED


# --- DEL-05: batch throughput / R5 quota / resume / single writer -----------


def _runner(tmp_path, plan=None, quota=None):
    runner = BatchRunner(tmp_path / "batch_state.json", quota=quota or QuotaAccount(100))
    state = runner.run(plan or _plan())
    return runner, state


def test_batch_runs_n_episodes_and_resolves_async_provider_tasks(tmp_path):
    runner = BatchRunner(tmp_path / "batch_state.json", quota=QuotaAccount(100))
    first = runner.run(_plan())
    assert first["status"] == WAITING_PROVIDER
    assert first["episodes"]["ep01"]["pending_tasks"]
    assert first["episodes"]["ep01"]["completed_steps"].keys() == {"script"}
    # quota was reserved at provider submission time
    assert first["quota"]["used"] == 1.0 + 3.0 + 1.0 + 3.0

    second = runner.resume(
        provider_results=[
            {"episode_id": "ep01", "step_id": "video", "ok": True},
            {"episode_id": "ep02", "step_id": "video", "ok": True},
        ]
    )
    assert second["status"] == COMPLETED
    assert second["episodes"]["ep01"]["status"] == "completed"
    assert second["episodes"]["ep02"]["status"] == "completed"
    assert all(not ep["pending_tasks"] for ep in second["episodes"].values())
    assert (tmp_path / "batch_state.json").exists()
    assert not runner.lock_path.exists()

    completed_events = [e for e in second["events"] if e["type"] == "step_completed"]
    keys = [(e["episode_id"], e["step_id"]) for e in completed_events]
    assert len(keys) == len(set(keys)) == 5
    submitted = [e for e in second["events"] if e["type"] == "provider_submitted"]
    assert len(submitted) == 2


def test_quota_exhaustion_pauses_then_resume_skips_completed_steps(tmp_path):
    plan = {
        "episodes": [
            {
                "episode_id": "ep01",
                "steps": [
                    {"step_id": "a", "units": 2.0},
                    {"step_id": "b", "units": 2.0},
                ],
            },
            {"episode_id": "ep02", "steps": [{"step_id": "c", "units": 2.0}]},
        ]
    }
    quota = QuotaAccount(4.0)
    runner = BatchRunner(tmp_path / "batch_state.json", quota=quota)
    paused = runner.run(plan)

    assert paused["status"] == PAUSED_QUOTA
    assert paused["stop_reason"]["reason"] == "quota_exceeded"
    assert paused["stop_reason"]["resume_from"] == {"episode_id": "ep02", "step_id": "c"}
    assert set(paused["episodes"]["ep01"]["completed_steps"]) == {"a", "b"}
    assert paused["episodes"]["ep02"]["completed_steps"] == {}
    assert paused["quota"]["remaining"] == 0.0

    quota.advance_window()
    resumed = runner.resume()
    assert resumed["status"] == COMPLETED
    assert set(resumed["episodes"]["ep02"]["completed_steps"]) == {"c"}
    completed = [e for e in resumed["events"] if e["type"] == "step_completed"]
    assert len(completed) == 3
    assert [e for e in completed if e["episode_id"] == "ep01"] == [
        e for e in paused["events"] if e["type"] == "step_completed"
    ]


def test_waiting_provider_task_is_not_resubmitted_on_resume(tmp_path):
    plan = {
        "episodes": [
            {
                "episode_id": "ep01",
                "steps": [{"step_id": "video", "units": 5.0, "provider": "async"}],
            }
        ]
    }
    runner = BatchRunner(tmp_path / "batch_state.json", quota=QuotaAccount(10))
    first = runner.run(plan)
    assert first["status"] == WAITING_PROVIDER
    assert "ep01:video" in first["episodes"]["ep01"]["pending_tasks"]
    assert first["quota"]["used"] == 5.0

    again = runner.resume()
    assert again["status"] == WAITING_PROVIDER
    assert again["quota"]["used"] == 5.0
    assert len([e for e in again["events"] if e["type"] == "provider_submitted"]) == 1

    done = runner.resume(provider_results=[{"episode_id": "ep01", "step_id": "video"}])
    assert done["status"] == COMPLETED
    assert done["episodes"]["ep01"]["pending_tasks"] == {}


def test_second_writer_is_rejected(tmp_path):
    state_path = tmp_path / "batch_state.json"
    runner = BatchRunner(state_path, quota=QuotaAccount(100))
    lock = BatchLock(tmp_path, name=state_path.name + ".lock").acquire()
    try:
        with pytest.raises(SingleWriterViolation):
            runner.run(_plan())
    finally:
        lock.release()
    # after the owner releases, the writer can proceed
    assert runner.run(_plan())["status"] == WAITING_PROVIDER


def test_unknown_provider_result_is_rejected(tmp_path):
    runner = BatchRunner(tmp_path / "batch_state.json", quota=QuotaAccount(100))
    runner.run(_plan())
    with pytest.raises(BatchError):
        runner.resume(
            provider_results=[{"episode_id": "ep01", "step_id": "compose", "ok": True}]
        )


def test_plan_validation_rejects_duplicate_episodes(tmp_path):
    runner = BatchRunner(tmp_path / "batch_state.json", quota=QuotaAccount(100))
    plan = {
        "episodes": [
            {"episode_id": "ep01", "steps": [{"step_id": "a"}]},
            {"episode_id": "ep01", "steps": [{"step_id": "b"}]},
        ]
    }
    with pytest.raises(BatchError):
        runner.run(plan)


# --- DEL-06: publish metadata package ---------------------------------------


@pytest.mark.parametrize("platform", TARGET_PLATFORMS)
def test_publish_package_ready_when_all_metadata_supplied(platform):
    package = build_publish_package(
        "ep01",
        platform,
        title="The Inheritance Trap — Episode 1",
        tags=["#shortdrama", "shortdrama", "revenge", "  #revenge "],
        cover=_cover(),
        publish_at="2026-11-12T18:00:00+00:00",
        account="@drama.en",
    )
    assert package["status"] == STATUS_READY
    assert package["metadata_complete"] is True
    assert package["tags"] == ["shortdrama", "revenge"]
    assert package["publish_ready"] is False  # DEL-09/A6 remain outstanding
    assert package["rule_verification"] == "UNCONFIRMED"
    assert package["compliance_status"] == "UNKNOWN"
    assert package["monetization_eligibility"] == "UNVERIFIED"


def test_publish_package_missing_fields_are_explicitly_incomplete():
    package = build_publish_package("ep01", "tiktok", title="Episode 1")
    assert package["status"] == STATUS_INCOMPLETE
    assert package["metadata_complete"] is False
    assert set(package["missing"]) == {"tags", "cover", "publish_at"}


def test_publish_package_invalid_values_block():
    long_title = "x" * (PLATFORM_EXPORT_PRESETS["youtube_shorts"]["title_max_chars"] + 1)
    package = build_publish_package(
        "ep01",
        "youtube_shorts",
        title=long_title,
        tags=["a"],
        cover={"width": 1280, "height": 720, "path": "cover.jpg"},
        publish_at="not-a-timestamp",
    )
    assert package["status"] == STATUS_BLOCKED
    assert any("title too long" in issue for issue in package["issues"])
    assert any("cover" in issue for issue in package["issues"])
    assert any("ISO-8601" in issue for issue in package["issues"])


def test_episode_packages_report_duplicates():
    entry = {
        "episode_id": "ep01",
        "platform": "tiktok",
        "title": "Ep 1",
        "tags": ["drama"],
        "cover": _cover(),
        "publish_at": "2026-11-12T18:00:00+00:00",
    }
    result = build_episode_packages([entry, dict(entry)])
    assert result["ok"] is False
    assert any("duplicate" in issue for issue in result["issues"])
    assert len(result["packages"]) == 1


def test_release_schedule_orders_and_flags_same_platform_collisions():
    def package(episode, platform, when):
        return build_publish_package(
            episode,
            platform,
            title=f"{episode} title",
            tags=["drama"],
            cover=_cover(),
            publish_at=when,
        )

    packages = [
        package("ep02", "tiktok", "2026-11-13T18:00:00+00:00"),
        package("ep01", "tiktok", "2026-11-12T18:00:00+00:00"),
        # same episode, different platform, same instant is legitimate
        package("ep01", "youtube_shorts", "2026-11-12T18:00:00+00:00"),
        # different episode, same platform, same instant splits the audience
        package("ep03", "tiktok", "2026-11-12T18:00:00+00:00"),
    ]
    schedule = build_release_schedule(packages)
    assert [row["episode_id"] for row in schedule["schedule"]][0] == "ep01"
    assert schedule["ok"] is False
    assert any("collision" in issue for issue in schedule["issues"])


def test_publish_manifest_roundtrip(tmp_path):
    package = build_publish_package(
        "ep01",
        "tiktok",
        title="Episode 1",
        tags=["drama"],
        cover=_cover(),
        publish_at="2026-11-12T18:00:00+00:00",
    )
    path = tmp_path / "publish_manifest.json"
    saved = save_publish_manifest(path, [package])
    loaded = load_publish_manifest(path)
    assert saved["ok"] is True
    assert loaded["packages"][0]["episode_id"] == "ep01"
    assert json.loads(path.read_text(encoding="utf-8"))["packages"][0]["status"] == STATUS_READY