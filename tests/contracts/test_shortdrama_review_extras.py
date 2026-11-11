"""Contract tests for the review extensions (REV-05/06/10/13/14).

Covers the reason-code regeneration map, the per-shot retry circuit breaker and
its reset semantics, sample-ratio changes that never regenerate, append-only
undo/supersede + stale listing, and review throughput timing. Builds on
``test_shortdrama_review.py`` (which stays untouched) and must not regress it.
"""
import json

import pytest

from lib.review import (
    ReviewStore, ReviewItem, ReviewDecision, ReviewPolicy,
    ReviewValidationError, UnknownReviewError, CircuitBreakerOpenError,
    REASON_CODES,
)
from lib.review_strategies import (
    STRATEGIES, RetryPolicy, strategy_for, strategy_path, validate_table,
)


@pytest.fixture
def store(tmp_path):
    return ReviewStore("drama", "ep01", projects_dir=tmp_path)


def _item(store, rid="r1", stage="shot_gen", priority=0, candidates=None,
          version="v1", created_at=None):
    kwargs = {}
    if created_at:
        kwargs["created_at"] = created_at
    it = ReviewItem(review_id=rid, project="drama", episode="ep01", stage=stage,
                    slot=f"sh_{rid}", candidates=candidates or ["a.mp4", "b.mp4"],
                    priority=priority, candidate_version=version, **kwargs)
    store.add_item(it)
    return it


def _reject(store, rid="r1", code="ARTIFACT"):
    return store.decide(rid, ReviewDecision(action="reject", reason_code=code))


# --- REV-05: reason code -> regeneration strategy ---------------------------


def test_every_reason_code_has_a_strategy():
    assert validate_table(REASON_CODES) == []
    assert set(STRATEGIES) == REASON_CODES


def test_every_strategy_has_a_regeneration_path():
    for code, strategy in STRATEGIES.items():
        assert strategy.strategy in ("regenerate", "send_back"), code
        if strategy.strategy == "send_back":
            assert strategy.target_stage, code
        assert (strategy.change_seed or strategy.add_reference
                or strategy.change_dialect or strategy.target_stage), code
        assert len(strategy.rationale) >= 10, code


def test_strategy_table_is_documented_in_module_docstring():
    from lib import review_strategies
    doc = review_strategies.__doc__ or ""
    for code in REASON_CODES:
        assert code in doc, f"{code} missing from the documented mapping table"


def test_strategy_query_resolves_review_stage():
    assert strategy_path("ARTIFACT", "keyframe")["target_stage"] == "keyframe"
    assert strategy_path("FACE_MISMATCH")["target_stage"] == "keyframe"
    assert strategy_path("PACING")["target_stage"] == "beat_map"
    identity = strategy_path("IDENTITY_UNKNOWN")
    assert identity["target_stage"] == "character_cast"
    assert identity["requires_human"] is True


def test_strategy_query_unknown_code_raises():
    with pytest.raises(KeyError):
        strategy_for("NOT_A_CODE")


# --- REV-06: retry circuit breaker + reset semantics -----------------------


def test_breaker_trips_after_three_consecutive_rejects(store):
    _item(store)
    for _ in range(3):
        _reject(store)
    state = store.breaker_state("r1")
    assert state["tripped"] is True
    assert state["consecutive_rejects"] == 3
    assert state["limit"] == 3

    # The fourth reject is refused: only send_back / human override remain.
    with pytest.raises(CircuitBreakerOpenError):
        _reject(store)
    store.decide("r1", ReviewDecision(action="send_back"))
    assert store.breaker_state("r1")["tripped"] is False


def test_new_candidate_version_resets_streak(store):
    _item(store)
    for _ in range(3):
        _reject(store)
    assert store.breaker_state("r1")["tripped"] is True

    _item(store, version="v2")  # new render appended as the latest item snapshot
    state = store.breaker_state("r1")
    assert state["tripped"] is False and state["consecutive_rejects"] == 0
    _reject(store)  # first rejection of the new version is allowed
    assert store.breaker_state("r1")["consecutive_rejects"] == 1


def test_pick_variant_resets_streak(store):
    _item(store)
    _reject(store)
    _reject(store)
    assert store.breaker_state("r1")["consecutive_rejects"] == 2
    store.decide("r1", ReviewDecision(action="pick_variant", chosen_candidate="b.mp4"))
    assert store.breaker_state("r1")["consecutive_rejects"] == 0
    _reject(store)
    assert store.breaker_state("r1")["consecutive_rejects"] == 1


def test_edit_prompt_resets_streak(store):
    _item(store)
    _reject(store)
    _reject(store)
    store.decide("r1", ReviewDecision(action="edit_prompt", prompt_patch="closer framing"))
    assert store.breaker_state("r1")["consecutive_rejects"] == 0


def test_undo_clears_streak(store):
    _item(store)
    for _ in range(3):
        _reject(store)
    assert store.breaker_state("r1")["tripped"] is True
    store.undo("r1", reason="rejections were a misfiled batch")
    assert store.breaker_state("r1")["tripped"] is False
    _reject(store)  # allowed again after compensation
    assert store.breaker_state("r1")["consecutive_rejects"] == 1


def test_human_override_with_evidence_bypasses_open_breaker(store):
    _item(store)
    for _ in range(3):
        _reject(store)
    with pytest.raises(CircuitBreakerOpenError):
        _reject(store)
    store.decide("r1", ReviewDecision(action="reject", reason_code="OTHER",
                                      override=True, evidence="human watched all 3 renders"))
    assert store.breaker_state("r1")["consecutive_rejects"] == 4


def test_custom_retry_limit(store):
    _item(store)
    _reject(store)
    with pytest.raises(CircuitBreakerOpenError):
        store.decide("r1", ReviewDecision(action="reject", reason_code="OTHER"),
                     retry_policy=RetryPolicy(max_consecutive_rejects=1))
    assert store.breaker_state("r1", RetryPolicy(max_consecutive_rejects=1))["tripped"] is True


# --- REV-10: sampled ratio changes never regenerate ------------------------


def test_policy_validation_rejects_invalid_values():
    with pytest.raises(ReviewValidationError):
        ReviewPolicy(mode="sometimes").validate()
    with pytest.raises(ReviewValidationError):
        ReviewPolicy(detail_ratio=1.5).validate()


def test_policy_roundtrip(store):
    store.set_policy(ReviewPolicy(mode="all", detail_ratio=0.8,
                                  always_detail=("hook", "climax", "twist")))
    loaded = store.get_policy()
    assert loaded.mode == "all" and loaded.detail_ratio == 0.8
    assert loaded.always_detail == ("hook", "climax", "twist")
    assert store.get_policy() == loaded


def test_ratio_change_updates_queue_without_regenerating_or_losing_decisions(store):
    for n in range(20):
        _item(store, rid=f"r{n:02d}", priority=1)
    for n in range(3):
        store.decide(f"r{n:02d}", ReviewDecision(action="accept"))

    lines_before = store.path.read_text(encoding="utf-8").strip().splitlines()
    items_before = {i["review_id"] for i in store.items()}
    pending_before = len(store.pending(ReviewPolicy(detail_ratio=0.2, sampling_salt="ep01")))

    store.set_policy(ReviewPolicy(mode="sampled", detail_ratio=0.9, sampling_salt="ep01"))

    # No log writes: nothing was regenerated and no decision was dropped.
    assert store.path.read_text(encoding="utf-8").strip().splitlines() == lines_before
    assert {i["review_id"] for i in store.items()} == items_before
    assert all(store.get(f"r{n:02d}")["decision"] is not None for n in range(3))

    pending_after = len(store.pending(store.get_policy()))
    assert pending_after >= pending_before
    assert len(store.items()) == 20


def test_mode_all_returns_full_queue_and_keeps_decisions(store):
    for n in range(6):
        _item(store, rid=f"r{n}", priority=2)
    store.decide("r0", ReviewDecision(action="accept"))
    assert len(store.pending(ReviewPolicy(mode="all"))) == 5


# --- REV-13: undo / supersede + stale -------------------------------------


def test_undo_is_compensating_append_with_reason(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept"))
    store.undo("r1", reason="wrong shot opened")

    records = [json.loads(line) for line in
               store.path.read_text(encoding="utf-8").strip().splitlines()]
    assert [r["event_type"] for r in records] == ["item", "decision", "undo"]
    undo = records[-1]
    assert undo["supersedes"] == "r1"
    assert undo["metadata"]["reason"] == "wrong shot opened"
    assert store.get("r1")["decision"] is None


def test_effective_decision_resolves_after_supersede(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept"))
    store.undo("r1", reason="mis-keyed")
    store.decide("r1", ReviewDecision(action="send_back"))
    assert store.get("r1")["decision"]["action"] == "send_back"
    assert store.stats()["decided"] == 1


def test_stale_lists_decisions_made_on_old_candidate_version(store):
    _item(store, version="v1")
    store.decide("r1", ReviewDecision(action="accept"))
    assert store.stale() == []

    _item(store, version="v2")  # a new render superseded the accepted one
    stale = store.stale()
    assert [s["review_id"] for s in stale] == ["r1"]
    assert stale[0]["decided_on_candidate_version"] == "v1"
    assert stale[0]["current_candidate_version"] == "v2"

    store.undo("r1", reason="decided on outdated render")
    assert store.stale() == []


def test_undo_unknown_review_raises(store):
    with pytest.raises(UnknownReviewError):
        store.undo("nope", reason="x")


# --- REV-14: review throughput timing -------------------------------------


def test_timing_stats_exposes_throughput_and_layers(store):
    _item(store, rid="l1", priority=0, created_at="2026-01-01T00:00:00+00:00")
    _item(store, rid="l2", priority=1, created_at="2026-01-01T00:02:00+00:00")
    _item(store, rid="todo", priority=2, created_at="2026-01-01T00:03:00+00:00")

    store.decide("l1", ReviewDecision(action="accept",
                                      decided_at="2026-01-01T00:01:00+00:00"))
    store.decide("l2", ReviewDecision(action="send_back",
                                      decided_at="2026-01-01T00:06:00+00:00"))

    timing = store.stats()["timing"]
    assert timing["opened_at"].startswith("2026-01-01T00:00:00")
    assert timing["last_decided_at"].startswith("2026-01-01T00:06:00")
    assert timing["elapsed_minutes"] == 6.0
    assert timing["review_minutes"] == 5.0
    assert timing["items_per_minute"] == pytest.approx(2 / 6.0, abs=1e-3)
    assert timing["median_review_seconds"] == 150.0
    assert timing["l1_items"] == 1 and timing["l1_minutes"] == 1.0
    assert timing["l2_items"] == 1 and timing["l2_minutes"] == 4.0
    assert timing["l3_items"] == 0 and timing["l3_minutes"] == 0.0
    assert timing["privacy_scope"].startswith("local")


def test_timings_per_item_marks_pending_as_open(store):
    _item(store, rid="done", priority=0, created_at="2026-01-01T00:00:00+00:00")
    _item(store, rid="open", priority=1, created_at="2026-01-01T00:00:30+00:00")
    store.decide("done", ReviewDecision(action="accept",
                                        decided_at="2026-01-01T00:00:45+00:00"))

    timings = {t["review_id"]: t for t in store.timings()}
    assert timings["done"]["seconds"] == 45.0
    assert timings["done"]["layer"] == "L1"
    assert timings["open"]["seconds"] is None
    assert timings["open"]["decided_at"] is None


def test_stats_keeps_legacy_contract(store):
    _item(store)
    store.decide("r1", ReviewDecision(action="accept"))
    stats = store.stats()
    for key in ("total_items", "decided", "pending", "action_distribution",
                "reason_code_distribution", "first_pass_accept_rate"):
        assert key in stats
    assert stats["total_items"] == 1 and stats["decided"] == 1
    assert stats["first_pass_accept_rate"] == 1.0