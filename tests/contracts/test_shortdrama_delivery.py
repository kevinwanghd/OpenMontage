"""WP-P contract tests: DEL-01 / DEL-07a / DEL-08 / DEL-09 / VIS-06 / LOC-02.

Every check is deterministic and offline. ffprobe output is supplied as data,
publish records are hand-entered metadata, and the render-runtime decision log
is a local JSON file -- no renderer, platform API, or paid call runs here.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from lib.decision_log import DecisionLog
from lib.delivery import (
    DEGRADED,
    FAIL,
    PASS,
    PlaybookError,
    TIKTOK_SAFE_AREA,
    load_drama_playbook,
    rect_in_safe_area,
    validate_delivery,
    validate_drama_playbook,
)
from lib.publish_precheck import (
    UNKNOWN,
    PrePublishBlocked,
    PublishRecordError,
    evaluate_pre_publish,
    format_av_evidence,
    load_publish_records,
    save_publish_records,
    validate_publish_record,
)
from lib.render_runtime import (
    BLOCKED,
    RuntimeBlockedError,
    RuntimeGovernanceError,
    approved_runtime,
    build_runtime_proposal,
    record_runtime_change,
    record_runtime_selection,
    require_approved_runtime,
    runtime_changes,
    verify_delivery_runtime,
)
from lib.subtitle import (
    MAX_CUE_DURATION_S,
    MAX_LINE_CHARS,
    MAX_LINES,
    MAX_WORDS_PER_CUE,
    TIKTOK_9X16,
)

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "data" / "playbooks" / "short-drama-realistic.yaml"
DECISION_SCHEMA = json.loads(
    (ROOT / "schemas" / "artifacts" / "decision_log.schema.json").read_text(encoding="utf-8")
)
RECORD_SCHEMA = DECISION_SCHEMA["$defs"]["shortdrama_decision_record"]


@pytest.fixture()
def runtime_log(tmp_path):
    return DecisionLog("drama", projects_dir=tmp_path)


# --- helpers ---------------------------------------------------------------


def _metadata(**overrides):
    base = {
        "width": 1080,
        "height": 1920,
        "duration_seconds": 95.0,
        "fps": 30,
        "video_codec": "h264",
        "audio_codec": "aac",
        "has_audio": True,
        "container": "mp4",
    }
    base.update(overrides)
    return base


def _ffprobe_streams(**overrides):
    document = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "avg_frame_rate": "30/1",
                "duration": "95.2",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "95.2",
            },
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "95.2"},
    }
    document.update(overrides)
    return document


def _record(**overrides):
    base = {
        "account": "@drama.en",
        "episode": "ep01",
        "version": "v1",
        "published_at": "2026-11-01T10:00:00+00:00",
        "collected_at": "2026-11-03T10:00:00+00:00",
        "source": "platform_backend",
        "metric_kind": "platform_raw",
        "views": 1200,
        "three_sec_retention": 0.62,
        "completion_rate": 0.21,
    }
    base.update(overrides)
    return base


def _evidence():
    common = {
        "status": "confirmed",
        "evidence": "official rule reviewed",
        "source": "TikTok community guidelines (screenshot + date recorded)",
        "checked_at": "2026-11-01T09:00:00+00:00",
    }
    return {
        "rights_source": {
            "status": "confirmed",
            "evidence": "self-authored original script + generated assets, signed off",
        },
        "account_availability_region": {
            **common,
            "region": "US",
            "evidence": "account @drama.en active, US region",
        },
        "content_policy": dict(common),
        "ai_disclosure": dict(common),
        "format_av_integrity": {
            "status": "confirmed",
            "evidence": "ffprobe metadata checks PASS",
        },
    }


# --- DEL-01 delivery validation -------------------------------------------


def test_native_1080x1920_delivery_passes():
    report = validate_delivery(_metadata())
    assert report.status == PASS
    assert report.degraded == []
    assert report.check("resolution").status == PASS
    assert report.check("audio_stream").status == PASS


def test_landscape_resolution_fails():
    report = validate_delivery(_metadata(width=1920, height=1080))
    assert report.status == FAIL
    assert report.check("resolution").status == FAIL
    assert report.check("aspect_ratio").status == FAIL


def test_480p_draft_upscale_is_degraded_not_native_1080p():
    report = validate_delivery(_metadata(source_resolution="854x480"))
    assert report.status == DEGRADED
    assert report.degraded == ["native_1080p"]
    assert report.check("upscale").status == DEGRADED
    assert report.ok  # DEGRADED ships, but the label is mandatory


def test_480p_preset_alias_is_also_degraded():
    report = validate_delivery(_metadata(draft_resolution="480p"))
    assert report.check("upscale").status == DEGRADED


def test_missing_audio_stream_fails():
    report = validate_delivery(_metadata(has_audio=False, audio_codec=None))
    assert report.status == FAIL
    assert report.check("audio_stream").status == FAIL


def test_frame_rate_outside_preset_fails():
    report = validate_delivery(_metadata(fps=12))
    assert report.check("fps").status == FAIL


def test_ffprobe_stream_shape_is_parsed():
    report = validate_delivery(_ffprobe_streams())
    assert report.status == PASS
    assert report.check("fps").status == PASS
    assert report.check("container").status == PASS


def test_missing_metadata_cannot_pass():
    report = validate_delivery({})
    assert report.status == FAIL
    assert report.check("resolution").status == "UNKNOWN"


# --- VIS-06 safe area ------------------------------------------------------


def test_tiktok_preset_derives_from_subtitle_safe_area():
    # VIS-06 single source of truth: delivery geometry comes from WP-N's preset.
    assert TIKTOK_SAFE_AREA["preset"] == TIKTOK_9X16.name
    assert TIKTOK_SAFE_AREA["version"] == TIKTOK_9X16.version
    assert TIKTOK_SAFE_AREA["calibrated"] is TIKTOK_9X16.calibrated
    assert TIKTOK_SAFE_AREA["calibrated"] is False  # provisional until calibrated


def test_subtitle_rect_inside_preset_passes():
    ok, reasons = rect_in_safe_area(TIKTOK_SAFE_AREA["subtitle_rect"])
    assert ok, reasons


def test_subtitle_rect_in_bottom_overlay_fails():
    bad = {"x": 54, "y": 1300, "width": 810, "height": 300}
    ok, reasons = rect_in_safe_area(bad)
    assert not ok
    assert any("content_rect" in reason or "bottom_caption_music" in reason for reason in reasons)


def test_subtitle_and_face_rects_are_checked_in_delivery():
    subtitle = dict(TIKTOK_SAFE_AREA["subtitle_rect"])
    face_inside = {"x": 300, "y": 500, "width": 200, "height": 220}
    report = validate_delivery(
        _metadata(), subtitle_rect=subtitle, face_rects=[face_inside]
    )
    assert report.check("subtitle_safe_area").status == PASS
    assert report.check("face_0_safe_area").status == PASS

    face_in_rail = {"x": 900, "y": 500, "width": 100, "height": 100}
    blocked = validate_delivery(_metadata(), face_rects=[face_in_rail])
    assert blocked.status == FAIL
    assert blocked.check("face_0_safe_area").status == FAIL


# --- playbook (LOC-02 / style) --------------------------------------------


def test_playbook_parses_and_safe_area_matches_runtime_preset():
    playbook = load_drama_playbook(PLAYBOOK)
    assert playbook["delivery"]["resolution"] == "1080x1920"
    assert playbook["delivery"]["aspect_ratio"] == "9:16"
    assert playbook["audio"]["loudness_lufs"] == -14
    assert playbook["safe_area"] == {
        "preset": TIKTOK_SAFE_AREA["preset"],
        "version": TIKTOK_SAFE_AREA["version"],
        "calibrated": TIKTOK_SAFE_AREA["calibrated"],
        "canvas": TIKTOK_SAFE_AREA["canvas"],
        "content_rect": TIKTOK_SAFE_AREA["content_rect"],
        "subtitle_rect": TIKTOK_SAFE_AREA["subtitle_rect"],
        "blocked_regions": TIKTOK_SAFE_AREA["blocked_regions"],
    }


def test_playbook_subtitle_style_follows_overseas_short_video_habit():
    playbook = load_drama_playbook(PLAYBOOK)
    subtitles = playbook["subtitles"]
    # LOC-02: English cues use WP-N's 42-char/line rule, not the Chinese
    # <= 20-character habit.
    assert subtitles["max_chars_per_line"] == MAX_LINE_CHARS
    assert subtitles["max_lines_per_cue"] == MAX_LINES
    assert subtitles["max_words_per_cue"] == MAX_WORDS_PER_CUE
    assert subtitles["max_cue_seconds"] == MAX_CUE_DURATION_S
    assert subtitles["style"]["anchor"] == "subtitle_rect"
    assert "clause_boundary" in subtitles["break_on"]


def test_playbook_requires_safe_area_and_short_video_pacing():
    playbook = load_drama_playbook(PLAYBOOK)
    interval = playbook["visual"]["visual_change_interval_seconds"]
    assert interval["min"] >= 1
    assert interval["max"] <= 3
    assert playbook["visual"]["hook_window_seconds"] <= 3


def test_playbook_missing_section_is_rejected(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("identity: {name: x}\n", encoding="utf-8")
    with pytest.raises(PlaybookError):
        load_drama_playbook(broken)


def test_playbook_validator_flags_chinese_subtitle_limits():
    playbook = load_drama_playbook(PLAYBOOK)
    playbook["subtitles"]["max_chars_per_line"] = 25
    errors = validate_drama_playbook(playbook)
    assert any("max_chars_per_line" in error for error in errors)


# --- DEL-07a publish records ----------------------------------------------


def test_publish_record_valid_with_known_metrics():
    result = validate_publish_record(_record())
    assert result.ok
    assert result.normalized["three_sec_retention"] == 0.62


def test_publish_record_missing_required_field_is_invalid():
    result = validate_publish_record(_record(account=""))
    assert not result.ok
    assert any("account" in issue for issue in result.issues)


def test_publish_record_unavailable_metrics_become_unknown():
    result = validate_publish_record(
        _record(views="UNKNOWN", three_sec_retention=None, completion_rate="UNKNOWN")
    )
    assert result.ok
    assert result.normalized["views"] == UNKNOWN
    assert result.normalized["three_sec_retention"] == UNKNOWN
    assert result.normalized["completion_rate"] == UNKNOWN


def test_publish_record_omitting_metric_is_flagged_but_stored_unknown():
    record = _record()
    del record["views"]
    result = validate_publish_record(record)
    assert not result.ok
    assert any("views" in issue for issue in result.issues)
    assert result.normalized["views"] == UNKNOWN


def test_percent_retention_is_normalized_to_ratio():
    result = validate_publish_record(
        _record(three_sec_retention=62, completion_rate=21, rate_unit="percent")
    )
    assert result.ok
    assert result.normalized["three_sec_retention"] == pytest.approx(0.62)
    assert result.normalized["completion_rate"] == pytest.approx(0.21)


def test_mixed_naive_and_aware_timestamps_still_compare():
    result = validate_publish_record(
        _record(
            published_at="2026-11-01T10:00:00",
            collected_at="2026-11-03T10:00:00+00:00",
        )
    )
    assert result.ok


def test_raw_platform_and_blind_test_records_are_saved_separately(tmp_path):
    blind = {
        "account": "@drama.en",
        "episode": "ep01",
        "version": "v1",
        "published_at": "2026-11-01T10:00:00+00:00",
        "collected_at": "2026-11-05T10:00:00+00:00",
        "source": "blind_test_survey",
        "metric_kind": "blind_test",
        "views": "UNKNOWN",
        "three_sec_retention": 0.5,
        "completion_rate": 0.4,
        "sample_size": 5,
    }
    path = tmp_path / "publish_records.json"
    payload = save_publish_records(path, [_record(), blind])
    assert len(payload["platform_raw"]) == 1
    assert len(payload["blind_test"]) == 1
    loaded = load_publish_records(path)
    assert loaded["platform_raw"][0]["source"] == "platform_backend"
    assert loaded["blind_test"][0]["sample_size"] == 5


def test_invalid_record_batch_is_not_saved(tmp_path):
    path = tmp_path / "publish_records.json"
    with pytest.raises(PublishRecordError):
        save_publish_records(path, [_record(metric_kind="mixed")])
    assert not path.exists()


# --- DEL-08 render_runtime governance -------------------------------------


_BOTH = {"remotion": True, "hyperframes": True}


def test_runtime_selection_is_recorded_and_approved(runtime_log):
    record = record_runtime_selection(
        runtime_log,
        options=[
            {"option_id": "remotion", "label": "Remotion"},
            {"option_id": "hyperframes", "label": "HyperFrames"},
            {"option_id": "ffmpeg", "label": "FFmpeg"},
        ],
        selected="hyperframes",
        reason="User picked the HTML runtime for this episode.",
        availability={**_BOTH, "ffmpeg": True},
    )
    assert record["decision"] == "hyperframes"
    assert record["metadata"]["user_approved"] is True
    assert approved_runtime(runtime_log) == "hyperframes"
    check = verify_delivery_runtime("HyperFrames", decision_log=runtime_log)
    assert check.status == PASS
    assert check.approved == "hyperframes"


def test_recorded_decision_matches_shortdrama_schema(runtime_log):
    record = record_runtime_selection(
        runtime_log,
        options=["remotion", "hyperframes"],
        selected="remotion",
        reason="Remotion was the approved baseline.",
        availability=_BOTH,
    )
    jsonschema.Draft202012Validator(RECORD_SCHEMA).validate(record)
    # The append-only JSONL is the persistent artifact, not a rewriteable doc.
    assert runtime_log.path.name == "decision_log.jsonl"
    assert runtime_log.entries() == [record]


def test_missing_approved_runtime_blocks_delivery(runtime_log):
    check = verify_delivery_runtime("remotion", decision_log=runtime_log)
    assert check.status == BLOCKED
    assert check.approved is None
    with pytest.raises(RuntimeBlockedError):
        require_approved_runtime("remotion", decision_log=runtime_log)


def test_runtime_mismatch_blocks_delivery(runtime_log):
    record_runtime_selection(
        runtime_log,
        options=["remotion", "hyperframes"],
        selected="hyperframes",
        reason="HyperFrames approved.",
        availability=_BOTH,
    )
    check = verify_delivery_runtime("remotion", decision_log=runtime_log)
    assert check.status == BLOCKED
    assert "approved runtime" in check.reason


def test_proposal_must_consider_every_available_runtime():
    with pytest.raises(RuntimeGovernanceError):
        build_runtime_proposal(
            "drama",
            ["remotion"],
            "remotion",
            "Only one runtime considered.",
            availability=_BOTH,
            user_approved=True,
        )


def test_unavailable_runtime_must_also_be_recorded():
    with pytest.raises(RuntimeGovernanceError):
        build_runtime_proposal(
            "drama",
            ["remotion"],
            "remotion",
            "HyperFrames was not recorded at all.",
            availability={"remotion": True, "hyperframes": False},
            user_approved=True,
        )


def test_unavailable_runtime_is_stored_as_rejected(runtime_log):
    record = record_runtime_selection(
        runtime_log,
        options=[
            {"option_id": "remotion", "label": "Remotion"},
            {"option_id": "hyperframes", "label": "HyperFrames", "available": False},
        ],
        selected="remotion",
        reason="HyperFrames is not installed; recorded as unavailable.",
        availability={"remotion": True, "hyperframes": False},
    )
    rejected = {
        alternative["option_id"]: alternative
        for alternative in record["alternatives"]
    }["hyperframes"]
    assert "not available" in rejected["reason"]
    assert record["metadata"]["availability"]["hyperframes"] is False


def test_unapproved_change_does_not_replace_approved_runtime(runtime_log):
    record_runtime_selection(
        runtime_log,
        options=["remotion", "hyperframes"],
        selected="remotion",
        reason="User approved Remotion.",
        availability=_BOTH,
    )
    record_runtime_selection(
        runtime_log,
        options=["remotion", "hyperframes"],
        selected="hyperframes",
        reason="Agent wanted to try HyperFrames but nobody approved.",
        user_approved=False,
        availability=_BOTH,
    )
    assert approved_runtime(runtime_log) == "remotion"
    assert (
        verify_delivery_runtime("hyperframes", decision_log=runtime_log).status
        == BLOCKED
    )


def test_approved_runtime_change_is_detectable(runtime_log):
    options = ["remotion", "hyperframes"]
    record_runtime_selection(
        runtime_log, options, "remotion", "Baseline approved.", availability=_BOTH
    )
    record_runtime_change(
        runtime_log,
        options,
        "hyperframes",
        "User approved switching after comparing renders.",
    )
    assert approved_runtime(runtime_log) == "hyperframes"
    changes = runtime_changes(runtime_log)
    assert len(changes) == 1
    assert changes[0]["from"] == "remotion"
    assert changes[0]["to"] == "hyperframes"
    assert verify_delivery_runtime("hyperframes", decision_log=runtime_log).status == PASS


def test_runtime_change_requires_a_prior_approval(runtime_log):
    with pytest.raises(RuntimeGovernanceError):
        record_runtime_change(
            runtime_log, ["remotion", "hyperframes"], "hyperframes", "Switch."
        )


def test_legacy_cumulative_decision_log_is_still_readable(tmp_path):
    legacy = tmp_path / "decision_log.json"
    legacy.write_text(
        json.dumps(
            {
                "version": "1.0",
                "project_id": "drama",
                "decisions": [
                    {
                        "decision_id": "d-001",
                        "stage": "proposal",
                        "category": "render_runtime_selection",
                        "subject": "render runtime for drama",
                        "options_considered": [
                            {"option_id": "remotion", "label": "Remotion", "score": 0.5, "reason": "x"},
                            {"option_id": "hyperframes", "label": "HyperFrames", "score": 0.6, "reason": "y"},
                        ],
                        "selected": "hyperframes",
                        "reason": "approved earlier",
                        "user_approved": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert approved_runtime(legacy) == "hyperframes"
    assert verify_delivery_runtime("hyperframes", decision_log=legacy).status == PASS


# --- DEL-09 pre-publish gate ----------------------------------------------


def test_pre_publish_passes_with_complete_evidence():
    report = evaluate_pre_publish(_evidence())
    assert report.status == PASS
    assert report.blockers == []


def test_missing_rights_evidence_blocks():
    evidence = _evidence()
    evidence.pop("rights_source")
    report = evaluate_pre_publish(evidence)
    assert report.status == "BLOCKED"
    assert any("rights_source" in blocker for blocker in report.blockers)


def test_unconfirmed_content_policy_blocks():
    evidence = _evidence()
    evidence["content_policy"]["status"] = "unconfirmed"
    report = evaluate_pre_publish(evidence)
    assert report.status == "BLOCKED"
    assert any("content_policy" in blocker for blocker in report.blockers)


def test_rule_item_without_source_date_blocks():
    evidence = _evidence()
    evidence["ai_disclosure"].pop("source")
    report = evaluate_pre_publish(evidence)
    assert report.status == "BLOCKED"
    assert any("ai_disclosure" in blocker for blocker in report.blockers)


def test_membership_gateway_terms_are_an_independent_gate():
    blocked = evaluate_pre_publish(_evidence(), membership_gateway=True)
    assert blocked.status == "BLOCKED"
    assert any("membership_gateway_terms" in blocker for blocker in blocked.blockers)

    evidence = _evidence()
    evidence["membership_gateway_terms"] = {
        "status": "confirmed",
        "evidence": "gateway ToS + output-rights clause saved",
        "source": "gateway terms page",
        "checked_at": "2026-11-01T09:00:00+00:00",
    }
    assert evaluate_pre_publish(evidence, membership_gateway=True).status == PASS


def test_pre_publish_can_raise_for_callers_that_require_pass():
    evidence = _evidence()
    evidence["format_av_integrity"] = {"status": "missing"}
    with pytest.raises(PrePublishBlocked):
        evaluate_pre_publish(evidence, require_pass=True)


def test_degraded_delivery_still_satisfies_av_integrity_evidence():
    report = validate_delivery(_metadata(source_resolution="854x480"))
    item = format_av_evidence(report)
    assert item["status"] == "confirmed"
    assert "DEGRADED" in item["evidence"]

    failed = format_av_evidence(validate_delivery(_metadata(width=720, height=1280)))
    assert failed["status"] == "missing"