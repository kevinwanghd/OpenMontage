"""Contract tests for lib/creative_sampling.py (v2.5 DIR-04).

The human sample of creative-gate decisions must be drawn by deterministic
code with a recorded seed (an agent picking its own audit sample proves
nothing), and it must not touch the technical sampling queue.
"""
import json

import pytest

from lib import creative_sampling as cs
from lib.review import ReviewPolicy


def _ids(n=20):
    return [f"r{i:02d}" for i in range(n)]


def test_same_seed_same_selection():
    first = cs.select(_ids(), ratio=0.2, seed=7)
    second = cs.select(_ids(), ratio=0.2, seed=7)
    assert first.chosen == second.chosen
    assert cs.replay(first)


def test_different_seed_changes_the_draw():
    assert cs.select(_ids(), ratio=0.3, seed=1).chosen != cs.select(_ids(), ratio=0.3, seed=2).chosen


def test_ratio_uses_ceiling_and_bounds():
    assert len(cs.select(["a", "b", "c", "d", "e"], ratio=0.2, seed=1).chosen) == 1
    assert len(cs.select(_ids(10), ratio=0.2, seed=1).chosen) == 2
    assert cs.select(_ids(10), ratio=0.0, seed=1).chosen == ()
    assert len(cs.select(_ids(10), ratio=1.0, seed=1).chosen) == 10


def test_generated_seed_is_returned_and_replayable():
    draw = cs.select(_ids(), ratio=0.2)
    assert isinstance(draw.seed, int)
    assert cs.replay(draw)


def test_invalid_ratio_is_rejected():
    with pytest.raises(ValueError, match="outside 0..1"):
        cs.select(_ids(), ratio=1.5)


def test_duplicate_ids_are_deduped():
    draw = cs.select(["a", "a", "b"], ratio=1.0, seed=3)
    assert draw.candidates == ("a", "b")


def test_audit_log_records_the_draw(tmp_path):
    draw = cs.select(_ids(5), ratio=0.2, seed=11)
    path = cs.write_audit(tmp_path / "drama" / "ep01", draw)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["seed"] == 11
    assert record["ratio"] == 0.2
    assert record["chosen"] == list(draw.chosen)
    assert record["created_at"]


def test_policy_defaults_and_validation():
    policy = ReviewPolicy()
    assert policy.creative_ratio == 0.2
    assert policy.creative_seed is None
    policy.validate()
    with pytest.raises(Exception):
        ReviewPolicy(creative_ratio=1.2).validate()


def test_creative_queue_is_independent_of_technical_ratio():
    import dataclasses

    assert dataclasses.is_dataclass(ReviewPolicy)
    policy = ReviewPolicy(detail_ratio=0.35, creative_ratio=0.2)
    assert policy.detail_ratio != policy.creative_ratio
    assert "sampling_salt" in {f.name for f in dataclasses.fields(ReviewPolicy)}