"""v2.7 GATE-03 — a timeout must never become a second paid submission.

Derived from the 2026-11-17 investigation, where several identical generation
requests were submitted while probing providers. Nothing recorded "this exact
request may already be running", so the only recovery path was to submit again.
"""

from __future__ import annotations

import pytest

from lib.generation_jobs import (
    DuplicateSubmission,
    JobLedger,
    JobLedgerError,
    JobRecord,
    UnresolvedUnknown,
    idempotency_key,
    request_fingerprint,
)

PROMPT = "A woman lifts a duffel bag in a warm kitchen."


def _fingerprint(prompt: str = PROMPT, params: dict | None = None) -> str:
    return request_fingerprint("pixverse", "v6", prompt, params or {"duration": 5})


def _record(fp: str, job_id: str = "job-1", *, state: str = "planned") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        request_fingerprint=fp,
        idempotency_key=idempotency_key(fp),
        provider="pixverse",
        state=state,
    )


@pytest.fixture()
def ledger(tmp_path) -> JobLedger:
    return JobLedger(tmp_path / "artifacts" / "generation_jobs.json")


class TestFingerprint:
    def test_same_content_same_fingerprint(self):
        assert _fingerprint() == _fingerprint()

    def test_prompt_change_changes_fingerprint(self):
        assert _fingerprint() != _fingerprint("different prompt")

    def test_param_order_does_not_matter(self):
        a = _fingerprint(params={"duration": 5, "seed": 1})
        b = _fingerprint(params={"seed": 1, "duration": 5})
        assert a == b

    def test_seed_change_is_a_different_request(self):
        assert _fingerprint(params={"seed": 1}) != _fingerprint(params={"seed": 2})


class TestDuplicateProtection:
    def test_second_submission_of_live_job_is_refused(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        with pytest.raises(DuplicateSubmission):
            ledger.submit(_record(fp, "job-2"))

    def test_completed_job_still_blocks_duplicate(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.transition("job-1", "processing")
        ledger.transition("job-1", "completed")
        with pytest.raises(DuplicateSubmission):
            ledger.submit(_record(fp, "job-2"))

    def test_failed_job_allows_retry(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.transition("job-1", "failed")
        assert ledger.resubmission_allowed(fp) is True
        retry = _record(fp, "job-2")
        retry.parent_job_id = "job-1"
        retry.changed_variable = "seed"
        ledger.submit(retry)
        assert ledger.latest(fp).job_id == "job-2"


class TestUnknownState:
    """The whole point: a timeout is not a failure."""

    def test_timeout_marks_unknown_not_failed(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        record = ledger.mark_unknown("job-1", "socket timeout after 900s")
        assert record.state == "unknown"

    def test_unknown_blocks_resubmission(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.mark_unknown("job-1", "network drop")
        assert ledger.resubmission_allowed(fp) is False
        with pytest.raises(UnresolvedUnknown):
            ledger.submit(_record(fp, "job-2"))

    def test_recovery_finds_the_task(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.mark_unknown("job-1")
        record = ledger.recover("job-1", found=True, provider_task_id="464243751518798")
        assert record.state == "processing"
        assert record.provider_task_id == "464243751518798"
        # The work exists, so it must not be submitted again.
        with pytest.raises(DuplicateSubmission):
            ledger.submit(_record(fp, "job-2"))

    def test_recovery_confirms_absence_then_retry_is_legal(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.mark_unknown("job-1")
        record = ledger.recover("job-1", found=False)
        assert record.state == "failed"
        assert ledger.resubmission_allowed(fp) is True
        ledger.submit(_record(fp, "job-2"))

    def test_mark_unknown_requires_a_live_job(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.transition("job-1", "failed")
        with pytest.raises(JobLedgerError):
            ledger.mark_unknown("job-1")


class TestLedgerIntegrity:
    def test_ledger_survives_reload(self, tmp_path):
        path = tmp_path / "artifacts" / "generation_jobs.json"
        first = JobLedger(path)
        fp = _fingerprint()
        first.submit(_record(fp, "job-1"))
        second = JobLedger(path)
        assert second.latest(fp).job_id == "job-1"

    def test_unknown_job_id_is_an_error(self, ledger):
        with pytest.raises(JobLedgerError):
            ledger.transition("nope", "processing")

    def test_invalid_state_is_rejected(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        with pytest.raises(JobLedgerError):
            ledger.transition("job-1", "banana")

    def test_retry_records_parent_and_changed_variable(self, ledger):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.transition("job-1", "failed")
        retry = _record(fp, "job-2")
        retry.parent_job_id = "job-1"
        retry.changed_variable = "seed"
        ledger.submit(retry)
        stored = ledger.latest(fp)
        assert stored.parent_job_id == "job-1"
        assert stored.changed_variable == "seed"

class TestQueryMatchesEnforcement:
    """The query and the gate must never disagree about the same state."""

    @pytest.mark.parametrize("state", ["cancelled", "failed", "rejected", "repair"])
    def test_settled_states_allow_both(self, ledger, state):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        ledger.transition("job-1", state)
        assert ledger.resubmission_allowed(fp) is True
        ledger.submit(_record(fp, "job-2"))

    @pytest.mark.parametrize("state", ["submitted", "processing", "completed", "accepted"])
    def test_live_states_block_both(self, ledger, state):
        fp = _fingerprint()
        ledger.submit(_record(fp, "job-1"))
        if state != "submitted":
            ledger.transition("job-1", state)
        assert ledger.resubmission_allowed(fp) is False
        with pytest.raises(DuplicateSubmission):
            ledger.submit(_record(fp, "job-2"))
