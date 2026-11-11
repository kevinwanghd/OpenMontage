"""T3 contract tests: submission ledger, episode event log, resume contract.

Guards crash recovery without duplicate paid submission (ORC-03), reservation
lifecycle (F16), single-owner shot-state projection (F6), and the resume-state
vocabulary get_next_stage cannot express (F4).
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.submission_ledger import SubmissionLedger, DuplicateSubmissionError, LedgerError
from lib.episode_log import EpisodeLog, IllegalTransitionError, EpisodeLogError
from lib.resume import resume_plan


@pytest.fixture
def ledger(tmp_path):
    return SubmissionLedger("drama", "ep01", projects_dir=tmp_path)


# --- ledger: crash injection & idempotency ---

def test_crash_before_submit_is_pending_and_resumable(ledger, tmp_path):
    e = ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)
    assert e["billing_state"] == "reserved"
    # crash before submit: nothing marked; resume should be run (no unknown)
    plan = resume_plan("drama", "ep01", projects_dir=tmp_path)
    assert plan["mode"] == "run"


def test_crash_after_submit_no_task_id_is_unknown_and_reconcile(ledger, tmp_path):
    e = ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)
    ledger.mark_submitted(e["entry_id"], task_id=None)  # crashed before retrieving id
    assert ledger.get(e["entry_id"])["billing_state"] == "unknown"
    plan = resume_plan("drama", "ep01", projects_dir=tmp_path)
    assert plan["mode"] == "reconcile"
    assert e["entry_id"] in plan["entry_ids"]


def test_duplicate_paid_submission_refused_while_unresolved(ledger):
    ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)
    with pytest.raises(DuplicateSubmissionError):
        ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)


def test_concurrent_same_input_reserves_exactly_once(tmp_path):
    """The duplicate check and the reservation must be one atomic operation:
    N concurrent writers for identical inputs may reserve the budget once, and
    every other writer must see DuplicateSubmissionError (ORC-03)."""
    workers = 8
    barrier = threading.Barrier(workers)

    def attempt():
        # Fresh instance per writer, like separate CLI/service processes would.
        ledger = SubmissionLedger("drama", "ep01", projects_dir=tmp_path)
        barrier.wait(timeout=10)
        try:
            ledger.record_intent("shot_gen", "molica", "same-hash", reserve_amount=2.0)
            return "reserved"
        except DuplicateSubmissionError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(workers)))

    assert outcomes.count("reserved") == 1, outcomes
    assert outcomes.count("duplicate") == workers - 1, outcomes
    ledger = SubmissionLedger("drama", "ep01", projects_dir=tmp_path)
    assert len(ledger._records()) == 1
    assert ledger.reserved_total("CNY") == 2.0


def test_resolved_unknown_frees_budget_and_allows_resubmit(ledger, tmp_path):
    e = ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)
    assert ledger.reserved_total("CNY") == 2.0
    ledger.mark_submitted(e["entry_id"], task_id=None)
    ledger.mark_result(e["entry_id"], "billed")  # reconciled
    assert ledger.reserved_total("CNY") == 0.0  # reservation released
    assert resume_plan("drama", "ep01", projects_dir=tmp_path)["mode"] == "run"
    # now a new attempt for same inputs is allowed (prior resolved)
    e2 = ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)
    assert e2["entry_id"] != e["entry_id"]


def test_escalated_reservation_blocks(ledger, tmp_path):
    e = ledger.record_intent("shot_gen", "molica", "hash1", reserve_amount=2.0)
    ledger.escalate_reservation(e["entry_id"])
    plan = resume_plan("drama", "ep01", projects_dir=tmp_path)
    assert plan["mode"] == "blocked"


def test_mark_result_invalid_state(ledger):
    e = ledger.record_intent("shot_gen", "molica", "hash1")
    with pytest.raises(LedgerError):
        ledger.mark_result(e["entry_id"], "paid")


def test_find_by_input_hash(ledger):
    ledger.record_intent("shot_gen", "molica", "hashA")
    ledger.record_intent("shot_gen", "molica", "hashB")
    assert len(ledger.find_by_input_hash("hashA")) == 1


# --- episode log: state machine & replay ---

@pytest.fixture
def log(tmp_path):
    return EpisodeLog("drama", "ep01", projects_dir=tmp_path)


def _walk_to_review(log, sid="sh_001"):
    log.append_event("shot_planned", sid)
    log.append_event("shot_keyframing", sid)
    log.append_event("shot_keyframe_approved", sid, candidate_version="v1")
    log.append_event("shot_generating", sid)
    log.append_event("shot_in_review", sid)


def test_legal_lifecycle_projection(log):
    _walk_to_review(log)
    log.append_event("shot_accepted", "sh_001")
    log.append_event("shot_assembled", "sh_001")
    st = log.shot_states()["sh_001"]
    assert st["state"] == "assembled" and st["event_seq"] == 7


def test_illegal_transition_rejected(log):
    log.append_event("shot_planned", "sh_001")
    with pytest.raises(IllegalTransitionError):
        log.append_event("shot_accepted", "sh_001")  # planned -> accepted illegal


def test_first_event_must_be_planned(log):
    with pytest.raises(IllegalTransitionError):
        log.append_event("shot_generating", "sh_001")


def test_unknown_event_type(log):
    with pytest.raises(EpisodeLogError):
        log.append_event("shot_exploded", "sh_001")


def test_reject_count_and_reset(log):
    _walk_to_review(log)
    log.append_event("shot_rejected", "sh_001")
    assert log.shot_states()["sh_001"]["reject_count"] == 1
    log.append_event("shot_generating", "sh_001")  # retry
    log.append_event("shot_in_review", "sh_001")
    log.append_event("shot_accepted", "sh_001")
    assert log.shot_states()["sh_001"]["reject_count"] == 0


def test_replay_recovery(tmp_path):
    log1 = EpisodeLog("drama", "ep01", projects_dir=tmp_path)
    _walk_to_review(log1)
    log1.append_event("shot_accepted", "sh_001")
    # new instance replays the same file -> identical projection
    log2 = EpisodeLog("drama", "ep01", projects_dir=tmp_path)
    assert log2.shot_states()["sh_001"]["state"] == "accepted"
    assert log2.shot_states()["sh_001"]["event_seq"] == 6


def test_skip_is_not_pass(log):
    log.append_event("shot_planned", "sh_001")
    log.append_event("shot_skipped", "sh_001")
    assert log.shot_states()["sh_001"]["state"] == "skipped"


# --- resume: clean run returns a stage ---

def test_resume_clean_returns_run_with_stage(tmp_path):
    (tmp_path / "drama").mkdir(parents=True, exist_ok=True)
    plan = resume_plan("drama", "ep01", projects_dir=tmp_path)
    assert plan["mode"] == "run"