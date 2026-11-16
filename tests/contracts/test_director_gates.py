"""Contract tests for lib/director_gates.py (v2.5 DIR-03/DIR-05).

The four director gates must be auditable from reviews.jsonl: only an effective
creative-gate approval closes a gate, and the delivery full_watch is bound to
the exact render version so a re-render reopens it.
"""
import pytest

from lib.director_gates import (
    DIRECTOR_GATE_STAGES,
    full_watch_ok,
    missing_director_gates,
    record_full_watch,
    require_director_gates,
    stage_has_director_pass,
)
from lib.review import ReviewDecision, ReviewItem, ReviewStore


@pytest.fixture
def store(tmp_path):
    return ReviewStore("drama", "ep01", projects_dir=tmp_path)


def _item(store, rid, stage):
    store.add_item(
        ReviewItem(
            review_id=rid,
            project="drama",
            episode="ep01",
            stage=stage,
            slot=f"sl_{rid}",
            candidates=["a.mp4", "b.mp4"],
        )
    )


def test_four_p0_gates_are_declared():
    assert set(DIRECTOR_GATE_STAGES) == {"brief", "script_shot", "assembly", "delivery"}
    assert DIRECTOR_GATE_STAGES["script_shot"] == ("script", "shot_gen")


def test_gate_requires_effective_creative_approval(store):
    _item(store, "r1", "shot_gen")
    assert not stage_has_director_pass(store.records(), ("script", "shot_gen"))
    store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
    assert not stage_has_director_pass(store.records(), ("script", "shot_gen"))
    store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))
    assert stage_has_director_pass(store.records(), ("script", "shot_gen"))


def test_creative_reject_does_not_close_the_gate(store):
    _item(store, "r1", "assembly")
    store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
    store.decide(
        "r1",
        ReviewDecision(action="reject", reason_code="NARRATIVE_WEAK", gate_type="creative"),
    )
    assert not stage_has_director_pass(store.records(), ("assembly",))


def test_undo_reopens_the_gate(store):
    _item(store, "r1", "assembly")
    store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
    store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))
    store.undo("r1", reason="director changed mind")
    assert not stage_has_director_pass(store.records(), ("assembly",))


def test_missing_gates_lists_unclosed_ones(store):
    _item(store, "r1", "idea")
    store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
    store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))
    assert missing_director_gates(store.records()) == ["script_shot", "assembly", "delivery"]
    with pytest.raises(ValueError, match="script_shot"):
        require_director_gates(store.records())


def test_full_watch_is_bound_to_the_render_version(store):
    _item(store, "d1", "delivery")
    store.decide("d1", ReviewDecision(action="accept", gate_type="technical"))
    record_full_watch(
        store,
        "d1",
        "sha-aaa",
        hook="opens on the unanswered call",
        emotion="guilt lands on the last line",
        pacing="two-beat hold before the cut",
    )
    assert full_watch_ok(store.records(), "d1", "sha-aaa")
    assert not full_watch_ok(store.records(), "d1", "sha-bbb")


def test_full_watch_requires_all_three_judgments(store):
    _item(store, "d1", "delivery")
    store.decide("d1", ReviewDecision(action="accept", gate_type="technical"))
    with pytest.raises(ValueError, match="emotion"):
        record_full_watch(store, "d1", "sha-aaa", hook="h", emotion=" ", pacing="p")


def test_full_watch_requires_version(store):
    _item(store, "d1", "delivery")
    with pytest.raises(ValueError, match="version"):
        record_full_watch(store, "d1", "", hook="h", emotion="e", pacing="p")