"""Contract tests for lib/dual_agent.py (v2.5 D18/D19, DUAL-03/05).

The dual-agent loop must route producer and critic to different model families,
require a structured critic verdict, and fail closed on malformed or
self-contradictory answers instead of silently passing.
"""
import copy
import json

import pytest
import yaml

from lib import agent_models as am
from lib import dual_agent as da


class FakeTransport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, model, system, prompt):
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        if not self.answers:
            raise AssertionError("unexpected extra model call")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _verdict(verdict="PASS", findings=()):
    return json.dumps({"verdict": verdict, "findings": list(findings)})


def _blocking(title="HOOK_WEAK", text="The opening is exposition, not conflict."):
    return {"title": title, "text": text, "severity": "blocking"}


def test_pass_routes_producer_and_critic_to_their_models():
    transport = FakeTransport(["INT. KITCHEN - NIGHT\nMOTHER: I made too much soup.", _verdict()])
    result = da.DualAgent("script", transport=transport).run("Write the episode script.")

    assert result.verdict.passed
    assert result.producer_output.startswith("INT. KITCHEN")
    assert transport.calls[0]["model"] == "deepseek-v4.1-flash-1017"
    assert transport.calls[1]["model"] == "glm-5.3"
    assert transport.calls[1]["model"] != transport.calls[0]["model"]


def test_critic_prompt_carries_task_and_work():
    transport = FakeTransport(["script text", _verdict()])
    da.DualAgent("script", transport=transport).run("TASK-MARKER")
    prompt = transport.calls[1]["prompt"]
    assert "TASK-MARKER" in prompt and "script text" in prompt


def test_reject_with_blocking_finding_blocks_the_gate():
    transport = FakeTransport(
        ["script text", _verdict("REJECT", [_blocking()])]
    )
    result = da.DualAgent("script", transport=transport).run("Write.")
    assert not result.verdict.passed
    assert result.verdict.blocking[0].title == "HOOK_WEAK"


def test_prompts_live_in_the_skill_layer():
    """Review instructions belong to skills, not Python (AGENT_GUIDE:80)."""
    prompts = da.load_prompts()
    assert "{role}" in prompts.producer_system
    assert "{role}" in prompts.critic_system
    assert "JSON" in prompts.critic_system


def test_missing_prompts_file_fails_loudly(tmp_path):
    with pytest.raises(da.DualAgentError, match="prompts not found"):
        da.load_prompts(tmp_path / "missing.yaml")


def test_gateway_transport_refuses_plaintext_http():
    """Credentials must not leave over an unencrypted connection (review finding 4)."""
    transport = da.GatewayTransport(base_url="http://gateway.example", api_key="k")
    with pytest.raises(da.DualAgentError, match="https"):
        transport.complete("model-x", "system", "prompt")


def test_reject_without_blocking_finding_is_rejected_as_malformed():
    warning_only = _verdict("REJECT", [{"title": "x", "text": "y", "severity": "warning"}])
    transport = FakeTransport(["script text", warning_only, warning_only])
    with pytest.raises(da.DualAgentError, match="no valid verdict"):
        da.DualAgent("script", transport=transport).run("Write.")


def test_malformed_critic_answer_is_retried_once_then_fails_closed():
    transport = FakeTransport(["script text", "not json", "still not json"])
    with pytest.raises(da.DualAgentError, match="no valid verdict"):
        da.DualAgent("script", transport=transport).run("Write.")
    assert len(transport.calls) == 3  # producer + two critic attempts
    assert "not valid JSON" in transport.calls[2]["prompt"]


def test_fenced_json_verdict_is_accepted():
    fenced = "```json\n" + _verdict() + "\n```"
    result = da.DualAgent("script", transport=FakeTransport(["script", fenced])).run("Write.")
    assert result.verdict.passed


def test_empty_producer_output_fails_closed():
    transport = FakeTransport(["   "])
    with pytest.raises(da.DualAgentError, match="empty output"):
        da.DualAgent("script", transport=transport).run("Write.")
    assert len(transport.calls) == 1  # critic never runs on empty work


def test_same_family_config_refuses_to_start(tmp_path):
    roles = {
        role: {
            "producer": {"model": "m-a", "family": "deepseek"},
            "critic": {"model": "m-b", "family": "deepseek"},
        }
        for role in am.P0_ROLES
    }
    path = tmp_path / "agent_models.yaml"
    path.write_text(yaml.safe_dump({"version": "1.0", "roles": roles}), encoding="utf-8")
    with pytest.raises(am.AgentModelConfigError, match="D19"):
        da.DualAgent("script", config=am.load(path), transport=FakeTransport([]))


def test_unknown_role_refuses_to_start():
    with pytest.raises(am.AgentModelConfigError, match="unknown role"):
        da.DualAgent("editor", transport=FakeTransport([]))


def test_parse_verdict_rejects_pass_with_blocking_finding():
    with pytest.raises(da.DualAgentError, match="contradicts"):
        da.parse_verdict(_verdict("PASS", [_blocking()]))


def test_parse_verdict_rejects_unknown_severity():
    bad = _verdict("REJECT", [{"title": "t", "text": "t", "severity": "fatal"}])
    with pytest.raises(da.DualAgentError, match="severity"):
        da.parse_verdict(bad)