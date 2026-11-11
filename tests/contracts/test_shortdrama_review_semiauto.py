"""Contract tests for REV-12 semi-automatic release.

Pins the P1 rule: a machine ``PASS`` on a non-must-view item is auto-accepted,
while every REV-02 must-view item and every ``UNKNOWN`` stays human. Also pins
the audit trail (machine check -> auto decision -> rollback) and the append-only
rollback path, without touching ``lib/review.py`` behaviour.
"""
import json

import pytest

from lib.review import (
    ReviewStore, ReviewItem, ReviewDecision, UnknownReviewError,
)
from lib.review_semiauto import (
    SemiAutoRelease, NotAutoDecisionError, AUTO_DECIDED_BY,
    PASS, FAIL, UNKNOWN, MUST_VIEW_REASONS,
    SKIP_ALREADY_DECIDED, SKIP_NO_MACHINE_CHECK, SKIP_MACHINE_UNKNOWN,
    SKIP_MACHINE_FAIL, SKIP_MUST_VIEW,
)


@pytest.fixture
def store(tmp_path):
    return ReviewStore("drama", "ep01", projects_dir=tmp_path)


@pytest.fixture
def semiauto(store):
    return SemiAutoRelease(store)


def _item(store, rid="r1", priority=2, reasons=None, stage="shot_gen", version="v1"):
    item = ReviewItem(
        review_id=rid, project="drama", episode="ep01", stage=stage,
        slot=f"sh_{rid}", candidates=["a.mp4", "b.mp4"], priority=priority,
        priority_reasons=reasons or [], candidate_version=version,
    )
    store.add_item(item)
    return item


# --- auto-accept: PASS + non-must-view only ---------------------------------


def test_pass_non_must_view_is_auto_accepted(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS, machine="face_sim", detail="sim=0.91")

    result = semiauto.auto_accept(batch_id="b1")

    assert result["accepted"] == ["r1"]
    assert result["skipped"] == []
    decision = store.get("r1")["decision"]
    assert decision["action"] == "accept"
    assert decision["decided_by"] == AUTO_DECIDED_BY
    assert decision["override"] is False and decision["evidence"] == ""
    assert "REV-12" in decision["note"] and PASS in decision["note"]
    assert store.pending() == []


@pytest.mark.parametrize("reasons", sorted(MUST_VIEW_REASONS))
def test_must_view_reason_is_never_auto_accepted(store, semiauto, reasons):
    _item(store, "r1", priority=2, reasons=[reasons])
    semiauto.record_check("r1", PASS)

    result = semiauto.auto_accept(batch_id="b1")

    assert result["accepted"] == []
    assert result["skipped"] == [
        {"review_id": "r1", "reason": SKIP_MUST_VIEW, "machine_result": PASS}]
    assert store.get("r1")["decision"] is None
    assert [i["review_id"] for i in store.pending()] == ["r1"]


def test_priority_zero_is_must_view_even_with_pass(store, semiauto):
    _item(store, "r1", priority=0)
    semiauto.record_check("r1", PASS)

    result = semiauto.auto_accept(batch_id="b1")

    assert result["accepted"] == []
    assert result["skipped"][0]["reason"] == SKIP_MUST_VIEW
    assert store.get("r1")["decision"] is None


@pytest.mark.parametrize("result_value,reason", [
    (UNKNOWN, SKIP_MACHINE_UNKNOWN),
    (FAIL, SKIP_MACHINE_FAIL),
])
def test_unknown_and_fail_never_auto_accepted(store, semiauto, result_value, reason):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", result_value)

    result = semiauto.auto_accept(batch_id="b1")

    assert result["accepted"] == []
    assert result["skipped"] == [
        {"review_id": "r1", "reason": reason, "machine_result": result_value}]
    assert store.get("r1")["decision"] is None


def test_unknown_on_must_view_still_skipped(store, semiauto):
    _item(store, "r1", priority=0)
    semiauto.record_check("r1", UNKNOWN)

    assert semiauto.auto_accept(batch_id="b1")["accepted"] == []
    assert store.get("r1")["decision"] is None


def test_item_without_machine_check_is_left_pending(store, semiauto):
    _item(store, "r1", priority=2)

    result = semiauto.auto_accept(batch_id="b1")

    assert result["accepted"] == []
    assert result["skipped"][0]["reason"] == SKIP_NO_MACHINE_CHECK
    assert store.get("r1")["decision"] is None


def test_already_decided_item_is_not_reaccepted(store, semiauto):
    _item(store, "r1", priority=2)
    store.decide("r1", ReviewDecision(action="accept", decided_by="owner"))
    semiauto.record_check("r1", PASS)
    before = store.get("r1")["decision"]

    result = semiauto.auto_accept(batch_id="b1")

    assert result["accepted"] == []
    assert result["skipped"][0]["reason"] == SKIP_ALREADY_DECIDED
    assert store.get("r1")["decision"] == before


def test_second_run_is_idempotent(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS)

    first = semiauto.auto_accept(batch_id="b1")
    second = semiauto.auto_accept(batch_id="b2")

    assert first["accepted"] == ["r1"]
    assert second["accepted"] == []
    assert second["skipped"][0]["reason"] == SKIP_ALREADY_DECIDED


def test_invalid_machine_result_is_rejected(store, semiauto):
    _item(store, "r1", priority=2)
    with pytest.raises(ValueError):
        semiauto.record_check("r1", "MAYBE")
    assert semiauto.checks() == {}


def test_machine_check_for_unknown_item_fails_loudly(store, semiauto):
    with pytest.raises(UnknownReviewError):
        semiauto.record_check("nope", PASS)


def test_latest_machine_check_wins(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", FAIL)
    semiauto.record_check("r1", PASS)

    assert semiauto.check_for("r1")["result"] == PASS
    assert semiauto.auto_accept(batch_id="b1")["accepted"] == ["r1"]


def test_auto_accept_only_writes_accept_decision(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS)
    semiauto.auto_accept(batch_id="b1")

    decision = store.get("r1")["decision"]
    assert decision["action"] == "accept"
    assert decision["override"] is False
    assert decision["evidence"] == ""
    assert decision["reason_code"] is None
    assert decision["chosen_candidate"] is None
    assert "identity" not in decision and "lip_sync" not in decision


# --- audit trail ------------------------------------------------------------


def test_audit_trail_is_complete_for_auto_accept(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS, machine="face_sim", detail="sim=0.91",
                          checked_at="2026-11-11T10:00:00+00:00")
    semiauto.auto_accept(batch_id="b1")

    audit = semiauto.audit(batch_id="b1")
    assert audit["batch_id"] == "b1"
    assert len(audit["entries"]) == 1
    entry = audit["entries"][0]
    assert entry["review_id"] == "r1"
    assert entry["batch_id"] == "b1"
    assert entry["machine_check"] == {
        "result": PASS, "machine": "face_sim", "detail": "sim=0.91",
        "checked_at": "2026-11-11T10:00:00+00:00"}
    assert entry["decision"]["decided_by"] == AUTO_DECIDED_BY
    assert entry["auto_accepted"] is True
    assert entry["rolled_back"] is False

    events = [json.loads(line) for line in semiauto.path.read_text(
        encoding="utf-8").splitlines()]
    kinds = [e["event_type"] for e in events]
    assert kinds == ["machine_check", "auto_accept"]
    accept_event = events[1]
    assert accept_event["machine_result"] == PASS
    assert accept_event["checked_at"] == "2026-11-11T10:00:00+00:00"
    assert accept_event["candidate_version"] == "v1"
    assert accept_event["decided_at"] == entry["decision"]["decided_at"]


def test_audit_includes_skipped_items_machine_check(store, semiauto):
    _item(store, "r1", priority=2)
    _item(store, "r2", priority=0)
    semiauto.record_check("r1", PASS)
    semiauto.record_check("r2", UNKNOWN)
    semiauto.auto_accept(batch_id="b1")

    audit = semiauto.audit(batch_id="b1")
    assert [e["review_id"] for e in audit["entries"]] == ["r1", "r2"]
    r2 = audit["entries"][1]
    assert r2["machine_check"]["result"] == UNKNOWN
    assert r2["decision"] is None
    assert r2["auto_accepted"] is False
    assert r2["skipped_reason"] == SKIP_MACHINE_UNKNOWN
    assert store.get("r2")["decision"] is None


def test_audit_survives_new_instance(store, tmp_path):
    _item(store, "r1", priority=2)
    semiauto = SemiAutoRelease(store)
    semiauto.record_check("r1", PASS)
    semiauto.auto_accept(batch_id="b1")

    reloaded = SemiAutoRelease(
        ReviewStore("drama", "ep01", projects_dir=tmp_path))
    assert reloaded.audit(batch_id="b1")["entries"][0]["auto_accepted"] is True


# --- rollback ---------------------------------------------------------------


def test_auto_decision_can_be_rolled_back(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS)
    semiauto.auto_accept(batch_id="b1")

    out = semiauto.rollback("r1", reason="sampling changed")

    assert out == {"review_id": "r1", "batch_id": "b1",
                   "reason": "sampling changed"}
    assert store.get("r1")["decision"] is None
    assert [i["review_id"] for i in store.pending()] == ["r1"]
    assert semiauto.auto_decisions() == []

    entry = semiauto.audit(batch_id="b1")["entries"][0]
    assert entry["rolled_back"] is True
    assert entry["auto_accepted"] is False
    assert entry["rolled_back_reason"] == "sampling changed"


def test_rollback_is_append_only(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS)
    semiauto.auto_accept(batch_id="b1")
    original = store.path.read_text(encoding="utf-8")
    ledger_before = semiauto.path.read_text(encoding="utf-8")

    semiauto.rollback("r1")

    assert store.path.read_text(encoding="utf-8").startswith(original)
    assert semiauto.path.read_text(encoding="utf-8").startswith(ledger_before)
    assert store.get("r1")["decision"] is None


def test_rollback_refuses_manual_decision(store, semiauto):
    _item(store, "r1", priority=2)
    store.decide("r1", ReviewDecision(action="accept", decided_by="owner"))

    with pytest.raises(NotAutoDecisionError):
        semiauto.rollback("r1")
    assert store.get("r1")["decision"]["decided_by"] == "owner"


def test_rollback_refuses_item_without_decision(store, semiauto):
    _item(store, "r1", priority=2)
    with pytest.raises(NotAutoDecisionError):
        semiauto.rollback("r1")


def test_rollback_batch_undoes_only_that_batch(store, semiauto):
    for rid in ("r1", "r2"):
        _item(store, rid, priority=2)
        semiauto.record_check(rid, PASS)
    semiauto.auto_accept(batch_id="b1")

    _item(store, "r3", priority=2)
    semiauto.record_check("r3", PASS)
    semiauto.auto_accept(batch_id="b2")

    rolled = semiauto.rollback_batch("b1")

    assert [r["review_id"] for r in rolled] == ["r1", "r2"]
    assert [d["review_id"] for d in semiauto.auto_decisions()] == ["r3"]
    assert [d["batch_id"] for d in semiauto.auto_decisions()] == ["b2"]
    # empty batch is a no-op
    assert semiauto.rollback_batch("b1") == []


def test_rolled_back_item_can_be_auto_accepted_again(store, semiauto):
    _item(store, "r1", priority=2)
    semiauto.record_check("r1", PASS)
    semiauto.auto_accept(batch_id="b1")
    semiauto.rollback("r1")

    again = semiauto.auto_accept(batch_id="b2")

    assert again["accepted"] == ["r1"]
    assert semiauto.auto_decisions(batch_id="b2")[0]["review_id"] == "r1"


def test_eligibility_reports_must_view_even_without_check(store, semiauto):
    _item(store, "r1", priority=0)
    el = semiauto.eligibility("r1")
    assert el["eligible"] is False
    assert el["must_view"] is True
    assert el["reason"] == SKIP_NO_MACHINE_CHECK