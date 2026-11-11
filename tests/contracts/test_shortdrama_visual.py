"""WP-R contract tests: contact sheets (VIS-03), safe area (VIS-06), layers (VIS-07).

Guards:
- Contact sheets are composited server-side, paginated by shot/candidate, and
  every cell in the JSON index resolves to a clickable sheet rectangle and the
  full-resolution L2 original (D-10). A missing image or a duplicate id fails
  before any sheet is written.
- The safe-area gate is fail-closed: an unconfirmed TikTok preset blocks
  publishing, subtitle burn rectangles must sit in the subtitle zone, and every
  face box comes from a face_tracker result, is mapped from normalised
  coordinates onto the delivery canvas, and must clear the interaction rail
  (VIS-06 / F18).
- The prompt builder returns five separate neutral layers (camera / movement /
  subject / lighting / style) for dialects to render; provider wording never
  leaks into the structure (VIS-07 / MOD-04).
- Pillow absence degrades to L2 originals with an explicit error, not an empty
  sheet.

M0: local files and fixtures only, zero external API calls.
"""

from __future__ import annotations

import copy
import json

import pytest

import lib.contact_sheet as cs
import lib.safe_area as sa
from lib.prompt_dialects import InvalidShotLanguageError, render as render_dialect
from lib.safe_area import Rect
from lib.shot_prompt_builder import (
    LAYER_NAMES,
    build_batch_prompt_layers,
    build_shot_prompt,
    build_shot_prompt_layers,
)


# ---------------------------------------------------------------------------
# Contact sheets (VIS-03 / D-10)
# ---------------------------------------------------------------------------


def _write_image(path, color, size=(360, 600)):
    pil = pytest.importorskip("PIL.Image")
    pil.new("RGB", size, color).save(path)


@pytest.fixture()
def four_candidates(tmp_path):
    colors = [(220, 30, 30), (30, 200, 60), (40, 70, 220), (230, 210, 30)]
    candidates = []
    for index, color in enumerate(colors, start=1):
        path = tmp_path / f"cand{index}.png"
        _write_image(path, color)
        candidates.append({"candidate_id": f"S1-c{index}", "path": str(path)})
    return candidates, colors


def test_sheets_paginate_by_shot_and_keep_a_shot_together(tmp_path, four_candidates):
    candidates, _ = four_candidates
    shots = [
        {"shot_id": "S1", "candidates": candidates[:2]},
        {"shot_id": "S2", "candidates": candidates[2:]},
        {
            "shot_id": "S3",
            "candidates": [
                {"candidate_id": "S3-c1", "path": candidates[0]["path"]}
            ],
        },
    ]

    index = cs.build_contact_sheets(
        shots, tmp_path / "sheet", columns=2, rows=1, label=False
    )

    assert index["page_count"] == 3
    assert index["layout"]["capacity"] == 2
    assert [page["shot_ids"] for page in index["pages"]] == [["S1"], ["S2"], ["S3"]]
    for page in index["pages"]:
        assert (tmp_path / "sheet" / page["path"]).is_file()


def test_sheets_pack_multiple_shots_onto_one_page_when_they_fit(tmp_path, four_candidates):
    candidates, _ = four_candidates
    shots = [
        {"shot_id": "S1", "candidates": candidates[:2]},
        {"shot_id": "S2", "candidates": candidates[2:]},
        {
            "shot_id": "S3",
            "candidates": [
                {"candidate_id": "S3-c1", "path": candidates[0]["path"]}
            ],
        },
    ]

    index = cs.build_contact_sheets(shots, tmp_path / "sheet", columns=2, rows=2)

    assert index["page_count"] == 2
    assert index["pages"][0]["shot_ids"] == ["S1", "S2"]
    assert index["pages"][1]["shot_ids"] == ["S3"]


def test_oversized_shot_continues_on_the_next_page(tmp_path, four_candidates):
    candidates, _ = four_candidates
    extra = tmp_path / "cand5.png"
    _write_image(extra, (120, 40, 200))
    shots = [{
        "shot_id": "S1",
        "candidates": candidates + [{"candidate_id": "S1-c5", "path": str(extra)}],
    }]

    index = cs.build_contact_sheets(shots, tmp_path / "sheet", columns=2, rows=2)

    assert index["page_count"] == 2
    assert [page["shot_ids"] for page in index["pages"]] == [["S1"], ["S1"]]
    assert len(index["pages"][0]["cells"]) == 4
    assert len(index["pages"][1]["cells"]) == 1


def test_index_locates_every_candidate_on_the_sheet_and_its_original(
    tmp_path, four_candidates
):
    from PIL import Image

    candidates, colors = four_candidates
    index = cs.build_contact_sheets(
        [{"shot_id": "S1", "candidates": candidates}],
        tmp_path / "sheet",
        columns=2,
        rows=2,
        label=False,
    )

    assert index["shots"][0]["shot_id"] == "S1"
    assert len(index["shots"][0]["candidates"]) == len(candidates)

    index_file = tmp_path / "sheet" / cs.INDEX_FILENAME
    assert json.loads(index_file.read_text(encoding="utf-8")) == index

    for entry, expected in zip(index["shots"][0]["candidates"], colors):
        assert entry["candidate_id"].startswith("S1-c")
        assert entry["l1"]["kind"] == "contact_sheet"
        assert entry["l2"]["kind"] == "original_media"
        assert entry["click_region"] == entry["image_rect"]

        page_file = tmp_path / "sheet" / entry["page_path"]
        with Image.open(page_file) as sheet:
            assert 0 <= entry["click_region"]["x"] < sheet.width
            assert 0 <= entry["click_region"]["y"] < sheet.height
            assert (
                entry["click_region"]["x"] + entry["click_region"]["width"]
                <= sheet.width
            )
            assert (
                entry["click_region"]["y"] + entry["click_region"]["height"]
                <= sheet.height
            )

            rect = entry["image_rect"]
            inset = 4
            crop = sheet.crop((
                int(rect["x"]) + inset,
                int(rect["y"]) + inset,
                int(rect["x"] + rect["width"]) - inset,
                int(rect["y"] + rect["height"]) - inset,
            )).resize((1, 1))
            sampled = crop.getpixel((0, 0))

        assert all(abs(a - b) <= 20 for a, b in zip(sampled, expected)), entry
        assert entry["l2"]["path"] == entry["source_path"]


def test_missing_candidate_fails_before_writing_any_sheet(tmp_path, four_candidates):
    candidates, _ = four_candidates
    missing = {"candidate_id": "S9-c1", "path": str(tmp_path / "nope.png")}

    with pytest.raises(FileNotFoundError, match="nope.png"):
        cs.build_contact_sheets(
            [{"shot_id": "S1", "candidates": candidates[:1] + [missing]}],
            tmp_path / "sheet",
        )

    assert list((tmp_path / "sheet").glob("*.png")) == []


def test_duplicate_candidate_ids_are_rejected(tmp_path, four_candidates):
    candidates, _ = four_candidates
    duplicate = dict(candidates[0])

    with pytest.raises(cs.ContactSheetError, match="duplicate candidate_id"):
        cs.build_contact_sheets(
            [{"shot_id": "S1", "candidates": [candidates[0], duplicate]}],
            tmp_path / "sheet",
        )


def test_pillow_unavailable_degrades_to_l2_with_an_explicit_error(
    tmp_path, four_candidates, monkeypatch
):
    candidates, _ = four_candidates
    monkeypatch.setattr(cs, "PILLOW_AVAILABLE", False)

    with pytest.raises(cs.PillowUnavailableError, match="L2 original media"):
        cs.build_contact_sheets(
            [{"shot_id": "S1", "candidates": candidates[:1]}],
            tmp_path / "sheet",
        )


# ---------------------------------------------------------------------------
# Safe area (VIS-06 / F18)
# ---------------------------------------------------------------------------


def _tracking(boxes, *, video_width=1080, video_height=1920):
    """Build a face_tracker-shaped result from (x, y, w, h) normalised boxes."""
    return {
        "video_width": video_width,
        "video_height": video_height,
        "fps": 5,
        "duration_seconds": 4.0,
        "frame_count": 20,
        "face_detected_count": len(boxes),
        "faces": [
            {
                "frame_index": 10 + index,
                "timestamp_seconds": 2.0 + index * 0.2,
                "confidence": 0.9,
                "bbox": {
                    "x": x,
                    "y": y,
                    "width": w,
                    "height": h,
                },
            }
            for index, (x, y, w, h) in enumerate(boxes)
        ],
    }


@pytest.fixture()
def confirmed_tiktok():
    return sa.confirm_safe_area_preset(
        sa.TIKTOK_SAFE_AREA_V1,
        source="calibrated against live TikTok UI by producer",
        calibrated_on="2026-11-11",
        ui_surface="TikTok 1080x1920 vertical feed",
    )


def test_subtitle_rect_inside_the_preset_passes(confirmed_tiktok):
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=[Rect(180, 1400, 720, 200)],
        face_tracking=_tracking([(0.3, 0.3, 0.15, 0.2)]),
    )

    assert report["publish_ready"] is True
    assert report["status"] == "pass"
    assert report["checks"]["subtitles"]["status"] == "pass"
    assert report["checks"]["faces"]["status"] == "pass"
    assert report["manual_review_required"] is True


def test_subtitle_rect_outside_the_safe_area_blocks(confirmed_tiktok):
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=[Rect(180, 1620, 720, 120)],
        face_tracking=_tracking([(0.3, 0.3, 0.15, 0.2)]),
    )

    assert report["publish_ready"] is False
    assert "subtitle_outside_safe_area" in report["blockers"]
    assert report["checks"]["subtitles"]["status"] == "fail"


def test_missing_subtitle_geometry_is_unknown_and_blocks(confirmed_tiktok):
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=None,
        face_tracking=_tracking([(0.3, 0.3, 0.15, 0.2)]),
    )

    assert report["publish_ready"] is False
    assert "subtitle_geometry_missing" in report["blockers"]


def test_face_boxes_come_from_normalised_face_tracker_detections():
    boxes = sa.face_bboxes_from_tracking(
        _tracking([(0.4, 0.3, 0.1, 0.2)], video_width=480, video_height=854),
        canvas_width=1080,
        canvas_height=1920,
    )

    assert len(boxes) == 1
    box = boxes[0]
    assert box.source == "face_tracker"
    assert box.frame_index == 10
    assert box.timestamp_seconds == 2.0
    assert box.bbox.as_dict() == {
        "x": 432.0,
        "y": 576.0,
        "width": 108.0,
        "height": 384.0,
    }
    assert box.video_width == 480
    assert box.video_height == 854


def test_face_outside_the_safe_area_blocks(confirmed_tiktok):
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=[Rect(180, 1400, 720, 200)],
        face_tracking=_tracking([(0.95, 0.3, 0.1, 0.2)]),
    )

    assert report["publish_ready"] is False
    assert "face_outside_safe_area" in report["blockers"]
    assert report["checks"]["faces"]["status"] == "fail"


def test_face_overlapping_the_interaction_rail_blocks_inside_the_safe_rect(
    confirmed_tiktok,
):
    # x=0.88..0.915 => 950.4..988.2 px, inside safe_rect (right edge 990) but
    # overlapping the 960..1080 right interaction rail.
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=[Rect(180, 1400, 720, 200)],
        face_tracking=_tracking([(0.88, 0.5, 0.035, 0.1)]),
    )

    faces = report["checks"]["faces"]
    assert report["publish_ready"] is False
    assert any(
        "overlaps_blocked_zone:right_interaction_rail" in item["violations"]
        for item in faces["boxes"]
    )


def test_unconfirmed_preset_blocks_publish_even_when_geometry_passes():
    report = sa.assess_safe_area(
        preset=sa.TIKTOK_SAFE_AREA_V1,
        subtitle_rects=[Rect(180, 1400, 720, 200)],
        face_tracking=_tracking([(0.3, 0.3, 0.15, 0.2)]),
    )

    assert sa.TIKTOK_SAFE_AREA_V1.confirmed is False
    assert report["publish_ready"] is False
    assert report["blockers"] == ["preset_unconfirmed"]
    assert report["checks"]["subtitles"]["status"] == "pass"
    assert report["checks"]["faces"]["status"] == "pass"


def test_no_detected_faces_is_unknown_and_blocks(confirmed_tiktok):
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=[Rect(180, 1400, 720, 200)],
        face_tracking=_tracking([]),
    )

    assert report["publish_ready"] is False
    assert "face_detection_unknown" in report["blockers"]
    assert report["checks"]["faces"]["status"] == "unknown"


def test_expect_faces_false_skips_the_face_check(confirmed_tiktok):
    report = sa.assess_safe_area(
        preset=confirmed_tiktok,
        subtitle_rects=[Rect(180, 1400, 720, 200)],
        face_tracking=None,
        expect_faces=False,
    )

    assert report["publish_ready"] is True
    assert report["checks"]["faces"]["status"] == "not_applicable"
    assert report["manual_review_required"] is True


def test_non_face_tracker_results_and_bad_boxes_are_rejected():
    with pytest.raises(sa.SafeAreaError, match="not a face_tracker result"):
        sa.face_bboxes_from_tracking(
            {"faces": []}, canvas_width=1080, canvas_height=1920
        )

    broken = _tracking([(0.3, 0.3, 0.0, 0.2)])
    with pytest.raises(sa.SafeAreaError, match="width/height must be positive"):
        sa.face_bboxes_from_tracking(
            broken, canvas_width=1080, canvas_height=1920
        )


def test_preset_provenance_is_recorded_for_the_publish_gate():
    preset = sa.get_safe_area_preset("tiktok", "1.0")
    record = preset.as_dict()

    assert record["version"] == "1.0"
    assert record["platform"] == "tiktok"
    assert record["confirmed"] is False
    assert record["source"]
    assert record["ui_surface"]
    assert record["blocked_zones"]["right_interaction_rail"]
    assert record["blocked_zones"]["bottom_caption_music"]


def test_confirming_a_preset_requires_dated_surface_evidence():
    with pytest.raises(sa.SafeAreaError, match="YYYY-MM-DD"):
        sa.confirm_safe_area_preset(
            sa.TIKTOK_SAFE_AREA_V1,
            source="producer",
            calibrated_on="11/11/2026",
            ui_surface="TikTok",
        )
    with pytest.raises(sa.SafeAreaError, match="unknown platform"):
        sa.get_safe_area_preset("youtube")


# ---------------------------------------------------------------------------
# Five-layer prompt builder (VIS-07 / MOD-04)
# ---------------------------------------------------------------------------

SCENE = {
    "id": "S1",
    "description": "雨夜巷口的人影",
    "texture_keywords": ["grain", "anamorphic"],
    "shot_language": {
        "shot_size": "close_up",
        "camera_movement": "dolly_in",
        "lens_mm": 50,
        "lighting_key": "rim_lit",
        "depth_of_field": "shallow",
        "color_temperature": "warm",
    },
}

STYLE = {
    "mood": "tense",
    "visual_language": {"aesthetic": "realistic handheld drama"},
}


def test_builder_returns_exactly_the_five_layers_in_order():
    layers = build_shot_prompt_layers(SCENE, STYLE)

    assert tuple(layers) == LAYER_NAMES
    assert set(layers) == {"camera", "movement", "subject", "lighting", "style"}


def test_camera_movement_and_lighting_stay_in_separate_layers():
    layers = build_shot_prompt_layers(SCENE, STYLE)

    assert layers["camera"] == ["lens_mm=50", "depth_of_field=shallow"]
    assert layers["movement"] == [
        "shot_size=close_up",
        "camera_movement=dolly_in",
    ]
    assert layers["lighting"] == [
        "lighting_key=rim_lit",
        "color_temperature=warm",
    ]


def test_subject_layer_carries_description_and_texture():
    layers = build_shot_prompt_layers(SCENE, STYLE)

    assert layers["subject"] == [
        "subject=雨夜巷口的人影",
        "texture=grain",
        "texture=anamorphic",
    ]


def test_style_layer_is_built_from_playbook_context():
    layers = build_shot_prompt_layers(SCENE, STYLE)

    assert layers["style"] == ["style=realistic handheld drama", "mood=tense"]
    assert build_shot_prompt_layers(SCENE)["style"] == []


def test_invalid_shot_language_is_rejected_before_any_rendering():
    bad = {"id": "S2", "description": "x", "shot_language": {"shot_size": "bogus"}}

    with pytest.raises(InvalidShotLanguageError, match="unknown shot_size"):
        build_shot_prompt_layers(bad)


def test_flat_prompt_is_only_a_rendering_of_the_structured_layers():
    flat = build_shot_prompt(SCENE, STYLE)

    assert "50mm lens" in flat
    assert "shallow depth of field" in flat
    assert "close-up focusing on face or detail" in flat
    assert "slow dolly in toward subject" in flat
    assert "雨夜巷口的人影" in flat
    assert "rim lighting highlighting edges" in flat
    assert flat.endswith("Style: realistic handheld drama, tense")


def test_dialects_render_separately_from_the_neutral_structure():
    layers = build_shot_prompt_layers(SCENE, STYLE)
    shot_language = dict(SCENE["shot_language"])
    shot_language["subject"] = SCENE["description"]
    shot_language["texture_keywords"] = SCENE["texture_keywords"]

    mock_prompt = render_dialect(shot_language, {}, dialect="mock")
    dashscope_prompt = render_dialect(shot_language, {}, dialect="dashscope")

    # The builder's structure is neutral: no provider prose leaks in.
    for layer in layers.values():
        for token in layer:
            assert "特写" not in token
            assert "地下" not in token
    # Both dialects consume the same structured shot language, differently.
    assert "shot_size=close_up" in mock_prompt
    assert "特写" in dashscope_prompt
    assert mock_prompt != dashscope_prompt


def test_builder_is_deterministic_and_does_not_mutate_its_inputs():
    scene = copy.deepcopy(SCENE)
    style = copy.deepcopy(STYLE)

    first = build_shot_prompt_layers(scene, style)
    second = build_shot_prompt_layers(scene, style)

    assert first == second
    assert scene == SCENE
    assert style == STYLE


def test_batch_layer_builder_skips_transitions_and_keeps_hero_flag():
    scenes = [
        SCENE,
        {"id": "S2", "type": "transition", "description": "fade"},
        dict(SCENE, id="S3", hero_moment=True),
    ]

    batch = build_batch_prompt_layers(scenes, STYLE)

    assert [item["scene_id"] for item in batch] == ["S1", "S3"]
    assert batch[1]["hero_moment"] is True
    assert tuple(batch[0]["layers"]) == LAYER_NAMES