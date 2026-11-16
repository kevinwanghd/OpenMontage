"""Reason-vocabulary minimum (v2.6 LESSON-03 revision / CARD-L08).

`REASON_CODES` exists to drive "reject -> how to redo" strategies, so only
failure classes that actually appear in review decisions belong there. A lesson
whose failure never shows up as a review decision (e.g. a checkpoint write
check) must not force a phantom code into the vocabulary.
"""
from __future__ import annotations

from lib.review import REASON_CODES, STAGE_REASON_CODES
from lib.review_strategies import STRATEGIES


def test_music_mismatch_is_a_registered_reason_code():
    assert "MUSIC_MISMATCH" in REASON_CODES


def test_music_mismatch_has_a_strategy_that_escalates_to_a_human():
    strategy = STRATEGIES["MUSIC_MISMATCH"]
    assert strategy.requires_human is True
    assert strategy.target_stage == "assembly"


def test_music_mismatch_is_accepted_at_assembly_and_delivery():
    assert "MUSIC_MISMATCH" in STAGE_REASON_CODES["assembly"]
    assert "MUSIC_MISMATCH" in STAGE_REASON_CODES["delivery"]


def test_music_mismatch_is_not_accepted_before_audio_exists():
    for stage in ("brief", "beat_map", "script", "scene_plan", "keyframe", "shot_gen"):
        assert "MUSIC_MISMATCH" not in STAGE_REASON_CODES[stage]


def test_approval_basis_missing_stays_out_of_the_review_vocabulary():
    # It is a checkpoint write rule, never a review rejection; registering it
    # would require inventing a meaningless regeneration strategy.
    assert "APPROVAL_BASIS_MISSING" not in REASON_CODES
    assert "APPROVAL_BASIS_MISSING" not in STRATEGIES