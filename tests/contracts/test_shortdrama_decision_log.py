"""WP-J contract tests for lib/decision_log.py (ORC-04, F32).

Guards the closed category mapping, append-only storage, query surface and the
truth-source split with reviews.jsonl: shot accept/reject decisions stay in
reviews, while provider/downgrade/budget/runtime decisions go to decision_log.
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.decision_log import (
    DecisionLog, DecisionEntry, DecisionValidationError,
    UnknownDecisionError, DECISION_CATEGORIES,
)
from lib.review import ReviewStore, ReviewItem, ReviewDecision

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "decision_log.schema.json"


def _load_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _entry(category="provider_selection", **kw):
    base = dict(
        category=category,
        subject="TTS provider for narration",
        decision="azure_tts",
        reason="studio voice quality within M0 budget",
        alternatives=[
            {"option_id": "azure_tts", "label": "Azure neural TTS", "reason": "best prosody"},
            {"option_id": "piper_tts", "label": "Local piper", "reason": "free but flatter"},
        ],
    )
    base.update(kw)
    return DecisionEntry(**base)


@pytest.fixture
def log(tmp_path):
    return DecisionLog("drama", projects_dir=tmp_path)


# --- closed category enum -------------------------------------------------

def test_closed_category_set_is_exactly_the_requirement_mapping():
    assert DECISION_CATEGORIES == {
        "provider_selection", "downgrade_approval",
        "budget_tradeoff", "render_runtime_selection",
    }


@pytest.mark.parametrize("category", sorted(DECISION_CATEGORIES))
def test_all_four_categories_append(log, category):
    rec = log.append(_entry(category))
    assert rec["category"] == category
    assert log.entries() == [rec]


def test_unknown_category_rejected_without_writing(log):
    with pytest.raises(DecisionValidationError):
        log.append(_entry("pipeline_selection"))  # real elsewhere, not ORC-04
    with pytest.raises(DecisionValidationError):
        log.append(_entry("shot_review_accept"))  # shot decisions belong to reviews
    assert log.entries() == []


def test_schema_category_enum_matches_code():
    record = _load_schema()["$defs"]["shortdrama_decision_record"]
    assert set(record["properties"]["category"]["enum"]) == DECISION_CATEGORIES
    assert record["additionalProperties"] is False


def test_record_has_required_audit_fields(log):
    rec = log.append(_entry(episode_id="ep01", shot_id="sh_012", operator="kevin"))
    for key in ("created_at", "category", "decision", "alternatives", "reason",
                "operator", "episode_id", "shot_id"):
        assert key in rec
    assert rec["operator"] == "kevin"
    assert rec["created_at"]
    assert [a["option_id"] for a in rec["alternatives"]] == ["azure_tts", "piper_tts"]


# --- schema validation ----------------------------------------------------

def test_positive_record_validates_against_schema(log):
    record = _load_schema()["$defs"]["shortdrama_decision_record"]
    Draft202012Validator(record).validate(log.append(_entry()))


def test_schema_rejects_unknown_category_and_extra_fields(log):
    record = _load_schema()["$defs"]["shortdrama_decision_record"]
    validator = Draft202012Validator(record)
    valid = log.append(_entry())
    assert not validator.is_valid({**valid, "category": "music_source"})
    assert not validator.is_valid({**valid, "action": "accept"})  # review field leaked in


def test_additive_schema_keeps_project_level_contract():
    """The pre-existing project-level decision_log artifact must still validate."""
    schema = _load_schema()
    Draft202012Validator.check_schema(schema)
    doc = {
        "version": "1.0",
        "project_id": "p",
        "decisions": [{
            "decision_id": "d-001", "stage": "proposal",
            "category": "render_runtime_selection", "subject": "runtime",
            "options_considered": [
                {"option_id": "remotion", "label": "Remotion", "score": 0.6, "reason": "fits"},
            ],
            "selected": "remotion", "reason": "fits the brief",
        }],
    }
    Draft202012Validator(schema).validate(doc)


# --- append-only ----------------------------------------------------------

def test_append_only_accumulates_never_rewrites(log):
    first = log.append(_entry(episode_id="ep01"))
    second = log.append(_entry(category="budget_tradeoff", episode_id="ep01"))
    lines = log.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == first
    assert json.loads(lines[1]) == second
    assert log.get(first["decision_id"]) == first


def test_unknown_decision_id_raises(log):
    with pytest.raises(UnknownDecisionError):
        log.get("nope")


# --- queries --------------------------------------------------------------

def test_query_by_category_episode_shot_and_time(log):
    a = log.append(_entry(category="provider_selection", episode_id="ep01", shot_id="sh_001"))
    b = log.append(_entry(category="budget_tradeoff", episode_id="ep01", shot_id="sh_002"))
    c = log.append(_entry(category="render_runtime_selection", episode_id="ep02", shot_id=None))

    assert [r["decision_id"] for r in log.query(episode_id="ep01")] == [a["decision_id"], b["decision_id"]]
    assert [r["decision_id"] for r in log.query(category="budget_tradeoff")] == [b["decision_id"]]
    assert [r["decision_id"] for r in log.query(shot_id="sh_001")] == [a["decision_id"]]

    # time bounds are inclusive and lexicographic on ISO-8601
    assert [r["decision_id"] for r in log.query(since=a["created_at"])] == [
        a["decision_id"], b["decision_id"], c["decision_id"]]
    assert [r["decision_id"] for r in log.query(until=c["created_at"])] == [
        a["decision_id"], b["decision_id"], c["decision_id"]]
    assert log.query(since="9999-01-01") == []


def test_counts_by_category(log):
    log.append(_entry(category="provider_selection"))
    log.append(_entry(category="provider_selection"))
    log.append(_entry(category="downgrade_approval"))
    assert log.counts_by_category() == {"provider_selection": 2, "downgrade_approval": 1}


# --- truth-source split with reviews.jsonl --------------------------------

def test_decision_log_and_reviews_do_not_mix(tmp_path):
    decisions = DecisionLog("drama", projects_dir=tmp_path)
    reviews = ReviewStore("drama", "ep01", projects_dir=tmp_path)

    d = decisions.append(_entry(episode_id="ep01", shot_id="sh_001"))

    reviews.add_item(ReviewItem(
        review_id="r1", project="drama", episode="ep01", stage="shot_gen",
        slot="sh_001", candidates=["a.mp4"], priority=0))
    reviews.decide("r1", ReviewDecision(action="accept"))

    assert decisions.path != reviews.path
    decision_recs = decisions.entries()
    review_lines = [json.loads(l) for l in reviews.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    # decision_log carries no review action/reason_code; reviews carry no category
    assert [r["decision_id"] for r in decision_recs] == [d["decision_id"]]
    assert all("action" not in r and "reason_code" not in r for r in decision_recs)
    assert all("category" not in r for r in review_lines)
    # the shot decision lives only in reviews.jsonl
    assert any(r.get("decision", {}).get("action") == "accept" for r in review_lines if r.get("event_type") == "decision")
    assert decisions.query(category="provider_selection")[0]["shot_id"] == "sh_001"