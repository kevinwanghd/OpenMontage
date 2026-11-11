"""T2 contract tests for lib/review.py (REV-01/02/03/13, ORC-04).

Guards the single mutation authority, action/reason validation, stale-version
conflict, append-only undo, and deterministic sampling that never regenerates.
"""
import pytest

from lib.review import (
    ReviewStore, ReviewItem, ReviewDecision, ReviewPolicy,
    ReviewValidationError, StaleReviewError, UnknownReviewError,
    REASON_CODES, ACTIONS,
)


@pytest.fixture
def store(tmp_path):
    return ReviewStore("drama", "ep01", projects_dir=tmp_path)


def _item(store, rid="r1", stage="shot_gen", priority=0, candidates=None, version="v1"):
    it = ReviewItem(review_id=rid, project="drama", episode="ep01", stage=stage,
                    slot=f"sh_{rid}", candidates=candidates or ["a.mp4", "b.mp4"],
                    priority=priority, candidate_version=version)
    store.add_item(it)
    return it


def test_accept_records_and_stats(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept"))
    s = store.stats()
    assert s["decided"] == 1 and s["pending"] == 0
    assert s["action_distribution"]["accept"] == 1
    assert s["first_pass_accept_rate"] == 1.0


def test_reject_requires_reason(store):
    _item(store)
    with pytest.raises(ReviewValidationError):
        store.decide("r1", ReviewDecision(action="reject"))


def test_reject_unknown_code(store):
    _item(store)
    with pytest.raises(ReviewValidationError):
        store.decide("r1", ReviewDecision(action="reject", reason_code="NOT_A_CODE"))


def test_reject_stage_inapplicable_code(store):
    _item(store, stage="shot_gen")
    with pytest.raises(ReviewValidationError):
        store.decide("r1", ReviewDecision(action="reject", reason_code="PACING"))
    # FACE_MISMATCH is applicable to shot_gen
    store.decide("r1", ReviewDecision(action="reject", reason_code="FACE_MISMATCH"))
    assert store.stats()["reason_code_distribution"]["FACE_MISMATCH"] == 1


def test_pacing_applicable_to_beat_map(store):
    _item(store, stage="beat_map")
    store.decide("r1", ReviewDecision(action="reject", reason_code="PACING"))
    assert store.stats()["reason_code_distribution"]["PACING"] == 1


def test_pick_variant_requires_candidate(store):
    _item(store, candidates=["a.mp4", "b.mp4"])
    with pytest.raises(ReviewValidationError):
        store.decide("r1", ReviewDecision(action="pick_variant", chosen_candidate="zzz.mp4"))
    store.decide("r1", ReviewDecision(action="pick_variant", chosen_candidate="b.mp4"))
    assert store.stats()["action_distribution"]["pick_variant"] == 1


def test_override_requires_evidence(store):
    _item(store)
    with pytest.raises(ReviewValidationError):
        store.decide("r1", ReviewDecision(action="accept", override=True))
    store.decide("r1", ReviewDecision(action="accept", override=True, evidence="visible frame check"))
    assert store.stats()["decided"] == 1


def test_stale_version_raises(store):
    _item(store, version="v2")
    with pytest.raises(StaleReviewError):
        store.decide("r1", ReviewDecision(action="accept"), candidate_version="v1")


def test_undo_nullifies_decision(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept"))
    assert store.stats()["pending"] == 0
    store.undo("r1", reason="mis-keyed")
    assert store.stats()["pending"] == 1  # back in queue
    assert store.get("r1")["decision"] is None


def test_unknown_review_raises(store):
    with pytest.raises(UnknownReviewError):
        store.decide("nope", ReviewDecision(action="accept"))


def test_pending_must_view_always_included(store):
    _item(store, rid="must", priority=0)
    _item(store, rid="opt", priority=2)
    q = store.pending(ReviewPolicy(mode="sampled", detail_ratio=0.0))
    ids = {i["review_id"] for i in q}
    assert "must" in ids  # must-view never dropped even at ratio 0


def test_pending_deterministic(store):
    for n in range(20):
        _item(store, rid=f"r{n}", priority=1)
    pol = ReviewPolicy(mode="sampled", detail_ratio=0.35, sampling_salt="ep01")
    q1 = [i["review_id"] for i in store.pending(pol)]
    q2 = [i["review_id"] for i in store.pending(pol)]
    assert q1 == q2  # stable across calls


def test_ratio_change_changes_queue_not_items(store):
    for n in range(20):
        _item(store, rid=f"r{n}", priority=1)
    before = {i["review_id"] for i in store.items()}
    low = store.pending(ReviewPolicy(detail_ratio=0.2, sampling_salt="s"))
    high = store.pending(ReviewPolicy(detail_ratio=0.9, sampling_salt="s"))
    assert len(high) >= len(low)
    assert {i["review_id"] for i in store.items()} == before  # nothing regenerated/removed


def test_mode_all_returns_every_undecided(store):
    for n in range(5):
        _item(store, rid=f"r{n}", priority=2)
    q = store.pending(ReviewPolicy(mode="all"))
    assert len(q) == 5


def test_append_only_accumulates(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept"))
    store.undo("r1", reason="x")
    store.decide("r1", ReviewDecision(action="reject", reason_code="ARTIFACT"))
    lines = store.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4  # item + decision + undo + decision, never rewritten
    assert store.get("r1")["decision"]["action"] == "reject"


def test_reason_codes_and_actions_constants():
    assert "MOTION_UNNATURAL" in REASON_CODES and "MOTION_NATURAL" not in REASON_CODES
    assert "NARRATIVE_WEAK" in REASON_CODES and "IDENTITY_UNKNOWN" in REASON_CODES
    assert ACTIONS == {"accept", "reject", "pick_variant", "edit_prompt", "send_back", "skip"}