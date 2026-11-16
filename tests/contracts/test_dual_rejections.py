"""Contract tests for lib/dual_rejections.py (v2.5 DUAL-06/08, LIB-06).

A critic REJECT must survive the turn (findings + reason code + model), the
producer must read the documented regeneration strategy, and dual-agent spend
is accounted for both sides.
"""
import json

import pytest

from lib import dual_rejections as dr


def _finding(title="HOOK_WEAK", text="The opening is exposition."):
    return {"title": title, "text": text, "severity": "blocking"}


def test_rejection_is_appended_and_queryable(tmp_path):
    ep = tmp_path / "drama" / "ep01"
    dr.record_rejection(
        ep,
        role="script",
        reason_code="HOOK_WEAK",
        findings=[_finding()],
        model="glm-5.3",
        prompt="Write the episode",
        media=["script_v1.md"],
    )
    rows = dr.read_rejections(ep)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "HOOK_WEAK"
    assert rows[0]["findings"][0]["title"] == "HOOK_WEAK"
    assert rows[0]["model"] == "glm-5.3"
    assert dr.query_rejections(ep, reason_code="HOOK_WEAK")
    assert not dr.query_rejections(ep, reason_code="PACING")


def test_query_filters_by_role(tmp_path):
    ep = tmp_path / "drama" / "ep02"
    dr.record_rejection(ep, role="script", reason_code="HOOK_WEAK", findings=[_finding()], model="m")
    dr.record_rejection(ep, role="visual", reason_code="FRAMING_BAD", findings=[_finding()], model="m")
    assert len(dr.query_rejections(ep, role="script")) == 1
    assert len(dr.query_rejections(ep, role="visual")) == 1


def test_rejection_requires_findings_and_codes(tmp_path):
    ep = tmp_path / "drama" / "ep03"
    with pytest.raises(ValueError, match="finding"):
        dr.record_rejection(ep, role="script", reason_code="HOOK_WEAK", findings=[], model="m")
    with pytest.raises(ValueError, match="reason_code"):
        dr.record_rejection(ep, role="script", reason_code=" ", findings=[_finding()], model="m")


def test_regeneration_hint_reads_the_documented_strategy():
    hint = dr.regeneration_hint("HOOK_WEAK")
    assert hint.strategy in {"regenerate", "send_back"}
    assert hint.target_stage == "brief"
    with pytest.raises(KeyError):
        dr.regeneration_hint("NOT_A_CODE")


def test_pair_cost_records_each_side_without_double_counting(tmp_path):
    ep = tmp_path / "drama" / "ep04"
    dr.record_pair_cost(
        ep,
        role="script",
        producer_model="deepseek-v4.1-flash-1017",
        critic_model="glm-5.3",
        producer_tokens=1200,
        critic_tokens=800,
        producer_cost_usd=0.003,
        critic_cost_usd=0.001,
    )
    rows = [json.loads(line) for line in (ep / dr.COST_FILENAME).read_text().splitlines()]
    assert [row["actor"] for row in rows] == ["producer", "critic"]
    assert {row["model"] for row in rows} == {"deepseek-v4.1-flash-1017", "glm-5.3"}
    assert sum(row["cost_usd"] for row in rows) == pytest.approx(0.004)
    assert len({row["pair_id"] for row in rows}) == 1


def test_negative_cost_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="negative"):
        dr.record_pair_cost(
            tmp_path, role="script", producer_model="a", critic_model="b",
            producer_tokens=1, critic_tokens=1,
            producer_cost_usd=-1, critic_cost_usd=0,
        )