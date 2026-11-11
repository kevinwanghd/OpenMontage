"""WP-Q contract tests: character identity layer (CHAR-01/04/05/06/07/08/09).

Every test asserts the *intent* of the requirement, not merely that a function
runs:

- CHAR-01: a character is a real library asset whose card validates against the
  frozen schema, face vectors live in the independent embedder-keyed store, and
  lineage/query work.
- CHAR-04/05: reference images travel as verifiable parameters; prompt text is
  never the reference channel; unsupported bindings block instead of silently
  degrading.
- CHAR-06: coverage is the fraction of sampled frames with >= 1 detection; the
  detector and identity matcher are separate; two faces prove the UNKNOWN path.
- CHAR-07: the same character under two bindings gets two thresholds.
- CHAR-08: no accessible wardrobe/approved shot blocks review.
- CHAR-09: reusing a character in a new episode performs no registration or
  generation call.

M0/fixture only: no external API, no media decoding, no image generation.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from lib.asset_library import AssetLibrary
from lib.character import (
    DEFAULT_SCHEMA_PATH,
    MATCH,
    MISMATCH,
    UNKNOWN,
    CharacterNotFoundError,
    CharacterRegistry,
    FaceThresholdConfig,
    InvalidCharacterError,
    ReferencePassingBlockedError,
    ReferenceUnavailableError,
    UnknownThresholdError,
    assess_character_identity,
    coverage_from_trackings,
    keyframe_reference_request,
    validate_binding_declaration,
    validate_card,
    video_binding_declaration,
)

EMBEDDER = "facenet512"
SEEDANCE = "seedance/doubao-seedance-2-0"
HOLOPIX = "holopix/seedance-2-0"
KLING_TEXT_ONLY = "kling/kling-2-5"


@pytest.fixture
def wardrobe_files(tmp_path):
    paths = []
    for angle in ("front", "three_quarter", "profile"):
        path = tmp_path / "ref" / f"ling_{angle}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        paths.append(path)
    return paths


@pytest.fixture
def registry(tmp_path):
    return CharacterRegistry(tmp_path / "library")


def _register_ling(registry, wardrobe_files, **kwargs):
    angles = ["front", "three_quarter", "profile"]
    wardrobe = [{"path": str(p), "angle": angle}
                for p, angle in zip(wardrobe_files, angles)]
    return registry.register(
        "ch_ling",
        "Ling",
        "26yo woman, oval face, ear-length black hair",
        wardrobe=wardrobe,
        voice_id="en_f_calm",
        face_embedding=[1.0, 0.0, 0.0, 0.0],
        embedder_model=EMBEDDER,
        **kwargs,
    )


def _tracking(sampled, detected_frames, *, shot_id="sh_001", applicable=True):
    """Build a native face_tracker-shaped payload."""
    return {
        "shot_id": shot_id,
        "applicable": applicable,
        "frame_count": sampled,
        "faces": [{"frame_index": idx, "bbox": {"x": 0.1, "y": 0.1,
                                                "width": 0.2, "height": 0.2}}
                  for idx in detected_frames],
    }


class _CountingMatcher:
    def __init__(self, score=0.90, model_version="mock-facenet-v1"):
        self.score = score
        self.model_version = model_version
        self.calls = 0

    def __call__(self, card, detections):
        self.calls += 1
        return {"score": self.score, "model_version": self.model_version}


# --- CHAR-01: first-class entity + independent identity store --------------


def test_register_card_matches_frozen_schema(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    validate_card(card, schema_path=DEFAULT_SCHEMA_PATH)  # machine check
    assert card["character_id"] == "ch_ling"
    assert card["canonical_description"].startswith("26yo")
    assert [w["angle"] for w in card["wardrobe"]] == ["front", "three_quarter", "profile"]
    assert card["voice_id"] == "en_f_calm"
    # Reference only -- the vector is never inlined in the card.
    assert card["face_embedding_ref"].startswith(f"identity/{EMBEDDER}/")
    assert "face_embedding" not in card
    assert len(json.dumps(card)) < 1500


def test_register_rejects_card_that_violates_schema(registry):
    with pytest.raises(InvalidCharacterError):
        # Character_id is not a string -> schema violation.
        registry.register(123, "Ling", "desc")


def test_register_rejects_bad_wardrobe_shape(registry):
    with pytest.raises(InvalidCharacterError):
        registry.register("ch_x", "X", "d", wardrobe=[{"path": "a.png", "color": "red"}])


def test_face_embedding_is_stored_in_separate_versioned_store(registry, wardrobe_files):
    _register_ling(registry, wardrobe_files)
    record = registry.get("ch_ling")
    assert record["identity"]["embedder_model"] == EMBEDDER
    assert record["identity"]["store_version"] == "v1"
    assert record["identity"]["path"].endswith("embeddings.npy")
    store_dir = registry.library_dir / "identity" / EMBEDDER / "v1"
    manifest = json.loads((store_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dim"] == 4
    # The card itself carries no raw vector.
    assert "embedding" not in record["metadata"]["card"]


def test_metadata_and_vector_queries_hit(registry, wardrobe_files):
    _register_ling(registry, wardrobe_files)
    registry.register(
        "ch_chen", "Chen", "31yo man, short hair",
        wardrobe=[{"path": "chen.png", "angle": "front"}],
        face_embedding=[0.0, 1.0, 0.0, 0.0], embedder_model=EMBEDDER)

    assert {hit["character_id"] for hit in registry.query(tags=["character"])} == {
        "ch_ling", "ch_chen"}
    assert [hit["character_id"]
            for hit in registry.query(tags=["character", "ch_ling"])] == ["ch_ling"]
    by_text = registry.query(text="Ling black hair")
    assert by_text and by_text[0]["character_id"] == "ch_ling"

    by_vector = registry.query(
        vector=[1.0, 0.0, 0.0, 0.0], embedder_model=EMBEDDER, min_sim=0.5)
    assert [hit["character_id"] for hit in by_vector] == ["ch_ling"]
    assert by_vector[0]["score"] == pytest.approx(1.0, abs=1e-5)


def test_lineage_and_unknown_character(registry, wardrobe_files):
    _register_ling(registry, wardrobe_files)
    lineage = registry.lineage("ch_ling")
    assert lineage["kind"] == "image"
    assert lineage["asset_id"] == "ch_ling"
    assert lineage["derived_from"] == []
    with pytest.raises(CharacterNotFoundError):
        registry.card("ch_missing")


# --- CHAR-04: keyframe reference group travels as parameters ---------------


def test_keyframe_reference_group_is_parameter_channel(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    request = keyframe_reference_request(card, prompt="same woman, kitchen at night")
    assert request["mode"] == "reference_group_edit"
    assert request["reference_channel"] == "parameters"
    assert request["reference_images"] == [str(p) for p in wardrobe_files]
    assert all(__import__("pathlib").Path(p).is_file()
               for p in request["reference_images"])
    # Prompt is a separate field: it never contains the reference paths.
    assert request["prompt"] == "same woman, kitchen at night"
    assert all(str(p) not in request["prompt"] for p in wardrobe_files)


def test_keyframe_without_wardrobe_blocks(tmp_path, registry):
    card = registry.register("ch_bare", "Bare", "no refs")
    with pytest.raises(ReferenceUnavailableError):
        keyframe_reference_request(card)
    with pytest.raises(ReferenceUnavailableError):
        keyframe_reference_request(card, reference_images=[str(tmp_path / "gone.png")])


# --- CHAR-05: binding declaration carries reference / first frame ----------


def test_video_binding_declaration_carries_reference_parameters(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    declaration = video_binding_declaration(card, SEEDANCE)
    assert declaration["status"] == "OK"
    assert declaration["reference_channel"] == "parameters"
    assert declaration["prompt_only_reference"] is False
    assert declaration["reference_images"] == [str(p) for p in wardrobe_files]
    assert declaration["capabilities"]["reference_images_max"] == 9

    verdict = validate_binding_declaration(declaration)
    assert verdict["status"] == "OK"
    assert verdict["reference_images"] == [str(p) for p in wardrobe_files]


def test_binding_that_cannot_take_references_blocks(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    # kling/kling-2-5 text_to_video declares reference_images_max: 0.
    with pytest.raises(ReferencePassingBlockedError):
        video_binding_declaration(card, KLING_TEXT_ONLY, operation="text_to_video")


def test_unapproved_prompt_only_reference_is_rejected(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    declaration = video_binding_declaration(card, SEEDANCE)
    declaration["prompt_only_reference"] = True
    with pytest.raises(ReferencePassingBlockedError):
        validate_binding_declaration(declaration)

    declaration["prompt_only_reference"] = False
    declaration["reference_channel"] = "prompt"
    with pytest.raises(ReferencePassingBlockedError):
        validate_binding_declaration(declaration)


def test_missing_reference_path_fails_verification(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    declaration = video_binding_declaration(card, SEEDANCE)
    declaration["reference_images"] = [str(wardrobe_files[0].with_name("gone.png"))]
    with pytest.raises(ReferenceUnavailableError):
        validate_binding_declaration(declaration)


def test_approved_downgrade_chain_marks_identity_unknown(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    approval = {
        "approved_by": "owner",
        "approved_at": "2026-11-11T00:00:00Z",
        "reason": "provider cannot accept reference images this month",
    }
    declaration = video_binding_declaration(
        card, KLING_TEXT_ONLY, operation="text_to_video", approved_downgrade=approval)
    assert declaration["status"] == "DEGRADED"
    assert declaration["identity_status"] == UNKNOWN
    assert declaration["blockers"]

    verdict = validate_binding_declaration(
        declaration, approved_downgrade=approval)
    assert verdict["status"] == "DEGRADED"
    assert verdict["identity_status"] == UNKNOWN


def test_first_frame_requires_binding_support(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    first_frame = wardrobe_files[0]
    # text_to_video on kling: first_frame_conditioning is false.
    with pytest.raises(ReferencePassingBlockedError):
        video_binding_declaration(
            card, KLING_TEXT_ONLY, operation="text_to_video",
            reference_images=[], first_frame=str(first_frame))
    # image_to_video on seedance: first_frame_conditioning is true.
    declaration = video_binding_declaration(
        card, SEEDANCE, reference_images=[], first_frame=str(first_frame))
    assert declaration["first_frame"] == str(first_frame)
    assert declaration["reference_images"] == []


# --- CHAR-06: coverage definition, detector/identity separation, UNKNOWN ---


def test_coverage_is_detected_over_sampled_in_applicable_shots():
    payload = coverage_from_trackings([
        _tracking(10, [0, 2, 4, 6, 8]),                 # 5/10
        _tracking(10, [1, 3, 5, 7, 9], shot_id="sh_002"),  # 5/10
        _tracking(20, [0] * 20, shot_id="ignored", applicable=False),
    ])
    assert payload["sampled_frames"] == 20
    assert payload["detected_frames"] == 10
    assert payload["coverage"] == pytest.approx(0.5)
    assert payload["status"] == "OK"


def test_face_tracker_payload_has_no_identity_output():
    """CHAR-06: face_tracker is detection-only; identity must be separate."""
    from tools.analysis.face_tracker import FaceTracker

    output_props = set(FaceTracker.output_schema["properties"])
    assert not output_props & {"embedding", "face_embedding", "identity", "similarity"}
    assert "face_embedding" not in " ".join(FaceTracker.capabilities)


def test_missing_identity_tool_is_unknown_not_faked(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    result = assess_character_identity(
        card, [_tracking(10, [0, 2, 4])],   # 0.3 coverage < 0.5 -> low coverage first
        identity_matcher=None, embedder_model=EMBEDDER, binding=SEEDANCE,
        thresholds=registry.thresholds)
    assert result["status"] == UNKNOWN
    assert result["reason"] == "low_coverage"

    result = assess_character_identity(
        card, [_tracking(10, [0, 1, 2, 3, 4, 5])],
        identity_matcher=None, embedder_model=EMBEDDER, binding=SEEDANCE,
        thresholds=registry.thresholds)
    assert result["status"] == UNKNOWN
    assert result["reason"] == "identity_tool_unavailable"
    assert result["score"] is None


def test_two_face_fixture_forces_unknown_without_calling_matcher(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    matcher = _CountingMatcher()
    two_faces = {
        "shot_id": "sh_001",
        "frames": [
            {"frame_index": 0, "face_count": 1},
            {"frame_index": 1, "face_count": 2},  # ambiguous: which one is Ling?
            {"frame_index": 2, "face_count": 1},
        ],
    }
    result = assess_character_identity(
        card, [two_faces], identity_matcher=matcher, embedder_model=EMBEDDER,
        binding=SEEDANCE, thresholds=registry.thresholds)
    assert result["status"] == UNKNOWN
    assert result["reason"] == "multiple_faces"
    assert result["multi_face_frames"][0]["face_count"] == 2
    assert matcher.calls == 0  # no score is fabricated for an ambiguous frame


def test_no_detection_and_no_applicable_frames_are_unknown(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    matcher = _CountingMatcher()
    no_face = assess_character_identity(
        card, [_tracking(8, [])], identity_matcher=matcher,
        embedder_model=EMBEDDER, binding=SEEDANCE, thresholds=registry.thresholds)
    assert (no_face["status"], no_face["reason"]) == (UNKNOWN, "no_face_detected")

    skipped = assess_character_identity(
        card, [_tracking(8, [0], applicable=False)], identity_matcher=matcher,
        embedder_model=EMBEDDER, binding=SEEDANCE, thresholds=registry.thresholds)
    assert (skipped["status"], skipped["reason"]) == (UNKNOWN, "no_applicable_frames")
    assert matcher.calls == 0


def test_identity_match_uses_calibrated_threshold(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    matcher = _CountingMatcher(score=0.80)
    result = assess_character_identity(
        card, [_tracking(10, range(10))], identity_matcher=matcher,
        embedder_model=EMBEDDER, binding=SEEDANCE, thresholds=registry.thresholds)
    assert result["status"] == MATCH  # 0.80 >= seedance/facenet512 0.78
    assert result["coverage"] == pytest.approx(1.0)
    assert result["coverage_ok"] is True
    assert result["threshold"] == pytest.approx(0.78)
    assert result["threshold_source"] == "binding"
    assert result["model_version"] == "mock-facenet-v1"
    assert matcher.calls == 1

    low = assess_character_identity(
        card, [_tracking(10, range(10))], identity_matcher=_CountingMatcher(score=0.60),
        embedder_model=EMBEDDER, binding=SEEDANCE, thresholds=registry.thresholds)
    assert low["status"] == MISMATCH


def test_uncalibrated_embedder_is_unknown(registry, wardrobe_files):
    card = _register_ling(registry, wardrobe_files)
    result = assess_character_identity(
        card, [_tracking(10, range(10))], identity_matcher=_CountingMatcher(),
        embedder_model="mystery-embedder", binding=SEEDANCE,
        thresholds=registry.thresholds)
    assert (result["status"], result["reason"]) == (UNKNOWN, "threshold_unconfigured")


# --- CHAR-07: per-binding / per-embedder thresholds ------------------------


def test_switching_binding_switches_threshold(registry, wardrobe_files):
    _register_ling(registry, wardrobe_files)
    seedance = registry.threshold_for(
        "ch_ling", binding=SEEDANCE, embedder_model=EMBEDDER)
    holopix = registry.threshold_for(
        "ch_ling", binding=HOLOPIX, embedder_model=EMBEDDER)
    assert seedance["threshold"] == pytest.approx(0.78)
    assert holopix["threshold"] == pytest.approx(0.74)
    assert seedance["threshold"] != holopix["threshold"]
    assert seedance["binding"] != holopix["binding"]


def test_persisted_calibration_overrides_builtin(tmp_path, registry, wardrobe_files):
    _register_ling(registry, wardrobe_files)
    registry.calibrate_threshold(
        SEEDANCE, EMBEDDER, 0.83, evidence="M1 fixture calibration")
    resolved = registry.threshold_for(
        "ch_ling", binding=SEEDANCE, embedder_model=EMBEDDER)
    assert resolved["threshold"] == pytest.approx(0.83)
    assert resolved["source"] == "calibrated"
    # Persisted, not in-memory only.
    on_disk = json.loads(
        (registry.library_dir / "characters" / "face_thresholds.json")
        .read_text(encoding="utf-8"))
    assert on_disk["calibrations"][0]["threshold"] == pytest.approx(0.83)


def test_character_card_threshold_override_wins(registry, wardrobe_files):
    _register_ling(registry, wardrobe_files,
                   metadata={"face_thresholds": {EMBEDDER: 0.61}})
    resolved = registry.threshold_for(
        "ch_ling", binding=SEEDANCE, embedder_model=EMBEDDER)
    assert resolved["threshold"] == pytest.approx(0.61)
    assert resolved["source"] == "character_card"


def test_unknown_pair_refuses_to_use_a_global_constant(tmp_path):
    config = FaceThresholdConfig(tmp_path / "t.json")
    with pytest.raises(UnknownThresholdError):
        config.resolve("unknown/binding", "unknown-embedder")
    # An embedder-level calibration is not the same as one global constant.
    assert config.resolve(None, "facenet512")["source"] == "embedder"


# --- CHAR-08: ref_context with accessible paths or a blocker ---------------


def test_ref_context_contains_accessible_wardrobe_and_history(
        registry, wardrobe_files, tmp_path):
    _register_ling(registry, wardrobe_files)
    approved = tmp_path / "shots" / "ep01_sh004.png"
    approved.parent.mkdir()
    approved.write_bytes(b"\x89PNG\r\n\x1a\n")
    registry.register_shot_reference(
        "ch_ling", "ep01_sh004", str(approved), approved=True)

    context = registry.assert_ref_context_ready("ch_ling")
    assert context["accessible"] is True
    assert context["blocked"] is False
    assert context["first_shot_reference"]["path"] == str(wardrobe_files[0])
    assert context["first_shot_reference"]["accessible"] is True
    assert all(w["accessible"] for w in context["wardrobe"])
    historical = [s for s in context["historical_shots"] if s["approved"]]
    assert historical and historical[0]["resolved_path"] == str(approved)
    assert historical[0]["accessible"] is True


def test_ref_context_without_any_reference_blocks_review(
        registry, wardrobe_files, tmp_path):
    _register_ling(registry, wardrobe_files)
    broken = [
        {"path": str(tmp_path / "missing" / "front.png"), "angle": "front"},
    ]
    card = registry.register("ch_broken", "Broken", "desc", wardrobe=broken)
    context = registry.build_ref_context("ch_broken")
    assert context["accessible"] is False
    assert context["blocked"] is True
    assert "no_accessible_wardrobe" in context["blockers"]
    assert "no_reference_for_review" in context["blockers"]
    assert context["first_shot_reference"] is None
    with pytest.raises(ReferenceUnavailableError):
        registry.assert_ref_context_ready("ch_broken")
    assert card["character_id"] == "ch_broken"


# --- CHAR-09: cross-episode reuse without regeneration ---------------------


def test_cross_episode_reuse_registers_and_generates_nothing(
        registry, wardrobe_files, monkeypatch):
    _register_ling(registry, wardrobe_files)
    library = AssetLibrary(registry.library_dir)

    def _explode(*args, **kwargs):
        raise AssertionError("reuse must not register or generate a new wardrobe")

    monkeypatch.setattr(registry.library, "register", _explode)

    result = registry.reuse_character("ch_ling", "ep02")
    assert result["reused"] is True
    assert result["wardrobe_generation_calls"] == 0
    assert result["card"]["character_id"] == "ch_ling"
    assert result["card"]["used_in_episodes"] == ["ep02"]
    # Same card bytes, same asset id -- nothing regenerated.
    assert library.get("ch_ling")["asset_id"] == "ch_ling"

    registry.reuse_character("ep02" and "ch_ling", "ep02")  # idempotent
    assert registry.card("ch_ling")["used_in_episodes"] == ["ep02"]
    registry.reuse_character("ch_ling", "ep03")
    assert registry.card("ch_ling")["used_in_episodes"] == ["ep02", "ep03"]
    assert len(registry._usage_records()) == 2