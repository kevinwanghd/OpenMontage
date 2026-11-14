"""Content-chain contract tests (M1).

These pin the bridge between agent-authored artifacts and provider calls:

- one work item per character / shot / script section, with stable item ids;
- the shot prompt carries the shot's action, visual evidence and character
  canon, and never falls back to a generic "make a video" prompt;
- shot_gen refuses to run without an approved keyframe for every shot
  (VIS-04: silent text-only substitution is forbidden);
- the submission input hash changes when the content changes (ORC-03);
- creative stages without an approved checkpoint stop the driver before any
  paid call (the script/screenplay review gate).

Fully offline: no sockets, no registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortdrama import content
from shortdrama.content import ContentError, WorkItem, build_work_items


def _scene_plan(shots: int = 2) -> dict:
    return {
        "version": "1.0",
        "scenes": [
            {
                "id": "sc1",
                "type": "character_scene",
                "description": "dim apartment",
                "start_seconds": 0,
                "end_seconds": 5 * shots,
            }
        ],
        "shots": [
            {
                "shot_id": f"sh_{index:03d}",
                "duration_s": 5,
                "single_action": "She turns away",
                "visual_evidence": "tears catch the light",
                "shot_language": {"shot_size": "close_up", "camera_movement": "static"},
                "reference_character_ids": ["ch_lin"],
                "candidates": 3,
            }
            for index in range(shots)
        ],
    }


def _card(character_id: str = "ch_lin") -> dict:
    return {
        "version": "1.0",
        "character_id": character_id,
        "name": "Lin",
        "canonical_description": "woman in her late twenties, black hair, grey coat",
        "voice_id": "voice_lin",
    }


def _manifest(shot_ids: list[str]) -> dict:
    return {
        "version": "1.0",
        "assets": [
            {
                "id": f"kf_{shot_id}",
                "type": "image",
                "path": f"assets/keyframes/{shot_id}_v1.png",
                "source_tool": "img_ref",
                "scene_id": shot_id,
            }
            for shot_id in shot_ids
        ],
    }


# ---------------------------------------------------------------------------
# keyframe
# ---------------------------------------------------------------------------


def test_keyframe_one_item_per_shot_with_canon_in_prompt(tmp_path: Path):
    items = build_work_items(
        "keyframe",
        "image_reference",
        {"scene_plan": _scene_plan(2), "drama_character": _card()},
        projects_dir=tmp_path,
        project="p",
        episode="ep01",
    )

    assert [item.item_id for item in items] == ["sh_000", "sh_001"]
    first = items[0].inputs
    assert first["n"] == 3
    assert first["size"] == content.DRAFT_IMAGE_SIZE
    assert "She turns away" in first["prompt"]
    assert "tears catch the light" in first["prompt"]
    assert "black hair" in first["prompt"]


def test_keyframe_requires_scene_plan():
    with pytest.raises(ContentError, match="scene_plan"):
        build_work_items("keyframe", "image_reference", {})


def test_keyframe_rejects_scene_plan_without_shots():
    with pytest.raises(ContentError, match="shots"):
        build_work_items(
            "keyframe", "image_reference", {"scene_plan": {"version": "1.0", "scenes": []}}
        )


# ---------------------------------------------------------------------------
# shot_gen
# ---------------------------------------------------------------------------


def test_shot_gen_conditions_on_approved_keyframe():
    items = build_work_items(
        "shot_gen",
        "video_primary",
        {
            "scene_plan": _scene_plan(2),
            "asset_manifest": _manifest(["sh_000", "sh_001"]),
        },
    )

    assert [item.item_id for item in items] == ["sh_000", "sh_001"]
    first = items[0].inputs
    assert first["operation"] == "first_frame"
    assert first["reference_image_path"] == "assets/keyframes/sh_000_v1.png"
    assert first["duration_seconds"] == 5.0
    assert "She turns away" in first["prompt"]


def test_shot_gen_refuses_missing_keyframe():
    with pytest.raises(ContentError, match="sh_001"):
        build_work_items(
            "shot_gen",
            "video_primary",
            {
                "scene_plan": _scene_plan(2),
                "asset_manifest": _manifest(["sh_000"]),
            },
        )


def test_shot_duration_is_clamped_to_model_support():
    plan = _scene_plan(1)
    plan["shots"][0]["duration_s"] = 30
    items = build_work_items(
        "shot_gen", "video_primary",
        {"scene_plan": plan, "asset_manifest": _manifest(["sh_000"])},
    )
    assert items[0].inputs["duration_seconds"] == 10.0


# ---------------------------------------------------------------------------
# character_cast / tts
# ---------------------------------------------------------------------------


def test_character_items_read_episode_character_cards(tmp_path: Path):
    card_dir = content.episode_artifacts_dir(tmp_path, "p", "ep01") / "characters"
    card_dir.mkdir(parents=True)
    (card_dir / "ch_lin.json").write_text(json.dumps(_card()), encoding="utf-8")

    items = build_work_items(
        "character_cast", "image_character_key", {},
        projects_dir=tmp_path, project="p", episode="ep01",
    )

    assert [item.item_id for item in items] == ["ch_lin"]
    assert "black hair" in items[0].inputs["prompt"]


def test_character_items_read_project_level_series_roster(tmp_path: Path):
    card_dir = content.project_artifacts_dir(tmp_path, "p") / "characters"
    card_dir.mkdir(parents=True)
    (card_dir / "ch_lin.json").write_text(json.dumps(_card()), encoding="utf-8")

    items = build_work_items(
        "character_cast", "image_character_key", {},
        projects_dir=tmp_path, project="p", episode="ep01",
    )

    assert [item.item_id for item in items] == ["ch_lin"]


def test_tts_one_item_per_script_section():
    script = {
        "version": "1.0",
        "title": "x",
        "total_duration_seconds": 10,
        "sections": [
            {"id": "s1", "text": "You promised.", "start_seconds": 0, "end_seconds": 5,
             "speaker_directions": "quiet"},
            {"id": "s2", "text": "I lied.", "start_seconds": 5, "end_seconds": 10},
        ],
    }
    items = build_work_items("assembly", "tts_voice", {"script": script})
    assert [item.item_id for item in items] == ["s1", "s2"]
    assert items[0].inputs == {"text": "You promised.", "speaker_directions": "quiet"}


def test_unwired_slots_keep_a_single_generic_item():
    items = build_work_items("assembly", "music", {})
    assert items == [WorkItem("music_default", {})]


# ---------------------------------------------------------------------------
# artifact lookup
# ---------------------------------------------------------------------------


def test_episode_artifact_wins_over_project_artifact(tmp_path: Path):
    project_artifact = content.project_artifacts_dir(tmp_path, "p") / "brief.json"
    episode_artifact = content.episode_artifacts_dir(tmp_path, "p", "ep01") / "brief.json"
    project_artifact.parent.mkdir(parents=True)
    episode_artifact.parent.mkdir(parents=True)
    project_artifact.write_text('{"where": "project"}', encoding="utf-8")
    episode_artifact.write_text('{"where": "episode"}', encoding="utf-8")

    assert content.load_artifact(tmp_path, "p", "ep01", "brief") == {"where": "episode"}
    assert content.missing_artifacts(tmp_path, "p", "ep01", ["brief", "script"]) == ["script"]


# ---------------------------------------------------------------------------
# input hash
# ---------------------------------------------------------------------------


def test_input_hash_changes_with_content():
    from shortdrama.driver import Driver

    driver = Driver("p", "ep01", executor=lambda *_: {})
    first = driver._input_hash(
        "shot_gen", "video_primary", "dashscope_video",
        WorkItem("sh_000", {"prompt": "She turns away"}),
    )
    second = driver._input_hash(
        "shot_gen", "video_primary", "dashscope_video",
        WorkItem("sh_000", {"prompt": "She turns back"}),
    )

    assert first != second
    assert first == driver._input_hash(
        "shot_gen", "video_primary", "dashscope_video",
        WorkItem("sh_000", {"prompt": "She turns away"}),
    )

def test_shot_gen_without_keyframes_animates_from_text():
    """Declared fallback: no image slot -> text_to_video per shot, still 9:16."""
    items = build_work_items(
        "shot_gen",
        "video_primary",
        {"scene_plan": _scene_plan(2)},
        keyframes_required=False,
    )

    assert [item.item_id for item in items] == ["sh_000", "sh_001"]
    first = items[0].inputs
    assert first["operation"] == "text_to_video"
    assert "reference_image_path" not in first
    assert first["ratio"] == "9:16"
    assert "She turns away" in first["prompt"]


# ---------------------------------------------------------------------------
# continuity gates + assembly wiring (M1 review fixes)
# ---------------------------------------------------------------------------


def test_shot_tts_items_one_per_dialogue_shot():
    plan = _scene_plan(2)
    plan["shots"][0]["dialogue"] = {"text": "hello", "speaker": "ch_lin"}
    items = build_work_items("shot_gen", "tts_voice", {"scene_plan": plan})
    assert [i.item_id for i in items] == ["sh_000"]
    assert items[0].inputs["text"] == "hello"


def test_shot_video_attaches_dialogue_audio_when_present(tmp_path):
    plan = _scene_plan(1)
    plan["shots"][0]["dialogue"] = {"text": "hi", "speaker": "ch_lin"}
    assets = tmp_path / "demo" / "ep01" / "assets"
    assets.mkdir(parents=True)
    (assets / "shot_gen_tts_voice_sh_000.mp3").write_bytes(b"x")
    items = build_work_items(
        "shot_gen", "video_primary",
        {"scene_plan": plan, "asset_manifest": _manifest(["sh_000"])},
        projects_dir=tmp_path, project="demo", episode="ep01",
    )
    assert items[0].inputs["audio_path"].endswith("shot_gen_tts_voice_sh_000.mp3")


def test_subtitle_segments_capped_to_real_audio():
    from shortdrama import continuity
    script = {"sections": [
        {"id": "s1", "text": "a long line", "start_seconds": 5, "end_seconds": 10},
    ]}
    segs = continuity.subtitle_segments(script, lambda sid: 1.5)
    assert segs[0]["start"] == 5
    assert segs[0]["end"] <= 5 + 2.5
    assert segs[0]["end"] - segs[0]["start"] <= 2.5


def test_continuity_rejects_unknown_character():
    from shortdrama import continuity
    plan = _scene_plan(1)
    errs = continuity.validate_scene_plan(plan, {"ch_other"})
    assert any("ch_lin" in e for e in errs)


def test_continuity_requires_keyframe_per_shot():
    from shortdrama import continuity
    plan = _scene_plan(2)
    errs = continuity.validate_keyframes(plan, _manifest(["sh_000"]))
    assert any("sh_001" in e for e in errs)


def test_continuity_dialogue_needs_audio():
    from shortdrama import continuity
    plan = _scene_plan(1)
    plan["shots"][0]["dialogue"] = {"text": "hi"}
    errs = continuity.validate_dialogue_audio(plan, lambda sid: None)
    assert errs and "sh_000" in errs[0]
    assert continuity.validate_dialogue_audio(plan, lambda sid: "x.mp3") == []
