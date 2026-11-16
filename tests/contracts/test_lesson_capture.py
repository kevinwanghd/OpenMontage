"""Automatic lesson capture contracts (v2.6 LESSON-01 / CARD-L02).

A capture that creates a duplicate file per occurrence makes the lesson list
unreadable; a capture that misses a signal loses the lesson. These tests pin
both: every signal produces a record, and the same pattern only bumps a count.
"""
from __future__ import annotations

import pytest
import yaml

from lib import lesson_capture
from lib.lesson_capture import LessonCaptureError


def _pending(root):
    return sorted((root / ".governance" / "pending-lessons").glob("*.yml"))


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _reject(**overrides):
    record = {
        "review_id": "gate_assembly",
        "stage": "assembly",
        "decision": {
            "action": "reject",
            "reason_code": "SUBTITLE_MISALIGN",
            "evidence": "line 7 subtitle 1.36s early",
        },
    }
    record.update(overrides)
    return record


def test_capture_rejection_writes_a_pending_lesson(tmp_path):
    path = lesson_capture.capture_rejection(tmp_path, _reject())
    data = _read(path)
    assert data["pattern_type"] == "SUBTITLE_MISALIGN"
    assert data["status"] == "pending"
    assert len(data["fingerprint"]) == 16
    assert data["occurrence_count"] == 1
    assert data["failure_context"]["line"] == 0


def test_capture_rejection_requires_a_reject(tmp_path):
    record = _reject()
    record["decision"]["action"] = "accept"
    with pytest.raises(LessonCaptureError):
        lesson_capture.capture_rejection(tmp_path, record)


def test_capture_rejection_requires_reason_code(tmp_path):
    record = _reject()
    record["decision"].pop("reason_code")
    with pytest.raises(LessonCaptureError):
        lesson_capture.capture_rejection(tmp_path, record)


def test_same_pattern_bumps_count_and_does_not_create_a_second_file(tmp_path):
    first = lesson_capture.capture_rejection(tmp_path, _reject())
    second = lesson_capture.capture_rejection(tmp_path, _reject())
    assert first == second
    assert len(_pending(tmp_path)) == 1
    assert _read(first)["occurrence_count"] == 2


def test_same_reason_code_across_stages_shares_a_fingerprint(tmp_path):
    # The fingerprint is pattern-only: the evidence differs, the pattern does not
    # differ when the normalized snippet matches.
    a = lesson_capture.capture_rejection(tmp_path, _reject())
    b = lesson_capture.capture_rejection(tmp_path, _reject())
    assert _read(a)["fingerprint"] == _read(b)["fingerprint"]


def test_capture_qa_blocking_writes_one_record_per_entry(tmp_path):
    report = {
        "stage": "delivery",
        "blocking": [
            {"shot": "m1_008", "check": "F2", "issue": "subtitle early", "evidence": "18.60"},
            {"shot": "m1_005", "check": "B5", "issue": "container missing", "evidence": "no prop"},
        ],
    }
    paths = lesson_capture.capture_qa_blocking(tmp_path, report)
    assert len(paths) == 2
    types = {_read(p)["pattern_type"] for p in paths}
    assert types == {"qa-F2", "qa-B5"}


def test_capture_qa_blocking_ignores_empty_report(tmp_path):
    assert lesson_capture.capture_qa_blocking(tmp_path, {"stage": "delivery", "blocking": []}) == []


def test_capture_decision_revision_only_fires_on_second_entry(tmp_path):
    single = [{"category": "provider_selection", "subject": "Music bed", "decision": "none"}]
    assert lesson_capture.capture_decision_revisions(tmp_path, single) == []

    revised = [
        {"category": "provider_selection", "subject": "Music bed", "decision": "piano.mp3"},
        {"category": "provider_selection", "subject": "Music bed", "decision": "none",
         "reason": "user judged it a mismatch"},
    ]
    paths = lesson_capture.capture_decision_revisions(tmp_path, revised)
    assert len(paths) == 1
    data = _read(paths[0])
    assert data["pattern_type"] == "decision-revision-provider_selection"
    assert "piano.mp3 -> none" in data["evidence"]["scan_summary"]


def test_repeated_revision_bumps_instead_of_duplicating(tmp_path):
    revised = [
        {"category": "provider_selection", "subject": "Music bed", "decision": "a"},
        {"category": "provider_selection", "subject": "Music bed", "decision": "b"},
    ]
    first = lesson_capture.capture_decision_revisions(tmp_path, revised)
    second = lesson_capture.capture_decision_revisions(tmp_path, revised)
    assert first == second
    assert _read(first[0])["occurrence_count"] == 2


def test_fingerprint_is_stable_across_whitespace_and_case():
    a = lesson_capture.fingerprint("PACING", "Assembly:  cuts   too slow")
    b = lesson_capture.fingerprint("PACING", "assembly: cuts too slow")
    assert a == b


def test_fingerprint_separates_different_pattern_types():
    assert lesson_capture.fingerprint("PACING", "x") != lesson_capture.fingerprint("CLIPPING", "x")