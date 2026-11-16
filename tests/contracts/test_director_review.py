"""Contract tests for lib/director_review.py (v2.5 DIR-07/DUAL-07).

The director's critic must be able to confirm or overrule a peer, the review
must be auditable, and the director pair must obey the D19 family split.
"""
import json

import pytest
import yaml

from lib import agent_models as am
from lib import director_review as dr


class FakeTransport:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def complete(self, model, system, prompt):
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        return self.answer


def _verdict(verdict="PASS", findings=()):
    return json.dumps({"verdict": verdict, "findings": list(findings)})


def test_peer_review_is_recorded(tmp_path):
    transport = FakeTransport(_verdict())
    verdict = dr.review_peer(
        tmp_path / "drama" / "ep01",
        "r1",
        "ScriptCritic accepted the script.",
        "Does the accepted script land the hook?",
        transport=transport,
    )
    assert verdict.passed
    # The model comes from the D19-validated config, not a hardcoded name.
    expected_model = am.load().pair("director").critic.model
    assert transport.calls[0]["model"] == expected_model
    # The instruction lives in the skill layer, not in Python source.
    assert "DirectorCritic" in transport.calls[0]["system"]
    rows = dr.read_reviews(tmp_path / "drama" / "ep01")
    assert len(rows) == 1
    assert rows[0]["review_id"] == "r1"
    assert rows[0]["verdict"]["verdict"] == "PASS"


def test_record_peer_review_parses_without_calling_a_model(tmp_path):
    """The parse+persist path is tool-only: no transport, no review decision."""
    verdict = dr.record_peer_review(
        tmp_path, "r9", "Peer accepted.", "Should it stand?",
        _verdict(), model="any-model",
    )
    assert verdict.passed
    rows = dr.read_reviews(tmp_path)
    assert rows[0]["review_id"] == "r9"


def test_peer_review_can_overrule(tmp_path):
    finding = {"title": "HOOK_WEAK", "text": "Accepted script opens on exposition.", "severity": "blocking"}
    verdict = dr.review_peer(
        tmp_path, "r1", "Peer accepted.", "Should it stand?",
        transport=FakeTransport(_verdict("REJECT", [finding])),
    )
    assert not verdict.passed
    assert verdict.blocking[0].title == "HOOK_WEAK"


def test_missing_inputs_are_rejected(tmp_path):
    transport = FakeTransport(_verdict())
    with pytest.raises(ValueError, match="review_id"):
        dr.review_peer(tmp_path, " ", "peer", "q", transport=transport)
    with pytest.raises(ValueError, match="peer summary"):
        dr.review_peer(tmp_path, "r1", " ", "q", transport=transport)


def test_same_family_director_pair_refuses_to_start(tmp_path):
    roles = {
        role: {
            "producer": {"model": "m-a", "family": "zhipu"},
            "critic": {"model": "m-b", "family": "zhipu"},
        }
        for role in am.P0_ROLES
    }
    path = tmp_path / "agent_models.yaml"
    path.write_text(yaml.safe_dump({"version": "1.0", "roles": roles}), encoding="utf-8")
    with pytest.raises(am.AgentModelConfigError, match="D19"):
        dr.review_peer(tmp_path, "r1", "peer", "q", transport=FakeTransport("{}"), config=am.load(path))