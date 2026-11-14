"""Pre-spend contract tests: parameters must fit the model before we pay.

These pin the checks that used to happen only at the provider (or never):

- a duration outside the model's declared range is refused locally;
- a resolution or aspect ratio the model cannot render is refused locally;
- a dialogue track handed to a model without audio-driven lipsync is refused;
- missing/empty reference files are refused;
- a declared catalog model that has no catalog entry is a binding error;
- fallback models must also support lipsync before rotation is allowed;
- shot_gen will not start while the keyframe visual verdict is missing or
  rejecting.

Fully offline: catalog lookups and filesystem reads only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortdrama import preflight
from shortdrama.content import WorkItem

WAN30 = "dashscope/wan3.0-video"


def _video_item(**overrides) -> WorkItem:
    inputs = {
        "prompt": "Margaret wipes the same spot",
        "operation": "first_frame",
        "duration_seconds": 4,
        "ratio": "9:16",
        "resolution": "720p",
        "reference_image_path": None,
        "audio_path": None,
    }
    inputs.update(overrides)
    return WorkItem("m1_003", inputs)


def _image(tmp_path: Path, name: str = "frame.png") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return str(path)


def test_clean_item_passes(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path))],
        catalog_model=WAN30,
        model="wan3.0-video",
        resolution="720p",
    )
    assert errors == []


def test_duration_below_model_minimum_is_refused(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path), duration_seconds=1)],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert any("below the model minimum" in error for error in errors)


def test_duration_above_model_maximum_is_refused(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path), duration_seconds=45)],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert any("exceeds the model maximum" in error for error in errors)


def test_unsupported_resolution_is_refused(tmp_path: Path):
    # wan2.7 has no 480P tier.
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path), resolution="480p")],
        catalog_model="dashscope/wan2.7-i2v",
        resolution="480p",
    )
    assert any("resolution '480p' is not supported" in error for error in errors)


def test_unsupported_ratio_is_refused(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path), ratio="2:1")],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert any("ratio '2:1' is not supported" in error for error in errors)


def test_dialogue_on_a_model_without_lipsync_is_refused(tmp_path: Path):
    # happyhorse declares no audio-driven lipsync.
    errors = preflight.validate_items(
        [_video_item(
            reference_image_path=_image(tmp_path),
            audio_path=str(tmp_path / "voice.mp3"),
            duration_seconds=4,
        )],
        catalog_model="dashscope/happyhorse-1.1-i2v",
        resolution="720p",
    )
    assert any("no audio-driven lipsync" in error for error in errors)


def test_dialogue_on_wan30_passes_with_both_files(tmp_path: Path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"ID3fake")
    errors = preflight.validate_items(
        [_video_item(
            reference_image_path=_image(tmp_path),
            audio_path=str(voice),
        )],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert errors == []


def test_missing_reference_files_are_refused(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(reference_image_path=str(tmp_path / "nope.png"))],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert any("reference_image_path" in error and "missing or empty" in error
               for error in errors)


def test_empty_prompt_is_refused(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(prompt="   ", reference_image_path=_image(tmp_path))],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert any("prompt is empty" in error for error in errors)


def test_image_to_video_without_an_image_is_refused():
    errors = preflight.validate_items(
        [_video_item(reference_image_path=None)],
        catalog_model=WAN30,
        resolution="720p",
    )
    assert any("needs a reference image" in error for error in errors)


def test_declared_catalog_model_missing_from_catalog_is_refused(tmp_path: Path):
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path))],
        catalog_model="dashscope/not-a-real-model",
        resolution="720p",
    )
    assert any("catalog has no entry" in error for error in errors)


def test_binding_without_catalog_model_only_checks_files(tmp_path: Path):
    item = _video_item(reference_image_path=str(tmp_path / "nope.png"))
    errors = preflight.validate_items([item], catalog_model=None, resolution="480p")
    assert len(errors) == 1
    assert "reference_image_path" in errors[0]


def test_fallback_without_lipsync_is_refused(tmp_path: Path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"ID3fake")
    errors = preflight.validate_items(
        [_video_item(reference_image_path=_image(tmp_path), audio_path=str(voice))],
        catalog_model=WAN30,
        provider="dashscope",
        fallback_models=["happyhorse-1.1-i2v"],
        resolution="720p",
    )
    assert any("fallback model 'happyhorse-1.1-i2v' cannot drive lipsync" in error
               for error in errors)


def test_visual_gate_blocks_when_the_review_never_ran(tmp_path: Path):
    errors = preflight.visual_review_errors(tmp_path)
    assert errors and "visual review not run" in errors[0]


def test_visual_gate_blocks_on_reject(tmp_path: Path):
    (tmp_path / "qa_visual.json").write_text(
        json.dumps({"verdict": "reject", "blocking": [{"issue": "two pairs of glasses"}]}),
        encoding="utf-8",
    )
    errors = preflight.visual_review_errors(tmp_path)
    assert errors and "two pairs of glasses" in errors[0]


def test_visual_gate_blocks_on_a_non_pass_verdict(tmp_path: Path):
    (tmp_path / "qa_visual.json").write_text(
        json.dumps({"verdict": "needs-work", "blocking": []}), encoding="utf-8"
    )
    errors = preflight.visual_review_errors(tmp_path)
    assert errors and "not 'pass'" in errors[0]


def test_visual_gate_passes_clean_verdict(tmp_path: Path):
    (tmp_path / "qa_visual.json").write_text(
        json.dumps({"verdict": "pass", "blocking": [], "warnings": ["minor"]}),
        encoding="utf-8",
    )
    assert preflight.visual_review_errors(tmp_path) == []


def test_visual_gate_reports_unreadable_verdict(tmp_path: Path):
    (tmp_path / "qa_visual.json").write_text("{not json", encoding="utf-8")
    errors = preflight.visual_review_errors(tmp_path)
    assert errors and "unreadable" in errors[0]