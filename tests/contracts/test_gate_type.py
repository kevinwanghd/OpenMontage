"""Contract tests for creative/technical gate separation (v2.5 DIR-02).

A creative approval must never paper over an unverified technical defect: the
technical gate PASS is a hard prerequisite, and the gate_type is persisted in
reviews.jsonl so the two gates stay auditable.
"""
import pytest

from lib.review import (
    ReviewDecision,
    ReviewItem,
    ReviewStore,
    ReviewValidationError,
)


@pytest.fixture
def store(tmp_path):
    return ReviewStore("drama", "ep01", projects_dir=tmp_path)


def _item(store, rid="r1", stage="shot_gen"):
    store.add_item(
        ReviewItem(
            review_id=rid,
            project="drama",
            episode="ep01",
            stage=stage,
            slot=f"sh_{rid}",
            candidates=["a.mp4", "b.mp4"],
        )
    )


def test_creative_accept_requires_prior_technical_pass(store):
    _item(store)
    with pytest.raises(ReviewValidationError, match="technical PASS"):
        store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))


def test_creative_accept_after_technical_pass_is_recorded(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
    rec = store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))
    assert rec["decision"]["gate_type"] == "creative"


def test_creative_pick_variant_requires_prior_technical_pass(store):
    _item(store)
    with pytest.raises(ReviewValidationError, match="technical PASS"):
        store.decide(
            "r1",
            ReviewDecision(
                action="pick_variant", chosen_candidate="a.mp4", gate_type="creative"
            ),
        )


def test_creative_reject_needs_no_technical_pass(store):
    _item(store)
    rec = store.decide(
        "r1",
        ReviewDecision(action="reject", reason_code="LIP_SYNC_FAIL", gate_type="creative"),
    )
    assert rec["decision"]["gate_type"] == "creative"


def test_undo_of_technical_pass_reopens_the_creative_gate(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
    store.undo("r1", reason="technical recheck")
    with pytest.raises(ReviewValidationError, match="technical PASS"):
        store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))


def test_invalid_gate_type_is_rejected(store):
    _item(store)
    with pytest.raises(ReviewValidationError, match="invalid gate_type"):
        store.decide("r1", ReviewDecision(action="accept", gate_type="editorial"))


def test_pre_v25_decisions_stay_untyped(store):
    _item(store)
    rec = store.decide("r1", ReviewDecision(action="accept"))
    assert rec["decision"]["gate_type"] is None