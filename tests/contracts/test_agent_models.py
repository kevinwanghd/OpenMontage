"""Contract tests for lib/agent_models.py (v2.5 DUAL-02/09, D19).

Guards the dual-agent model configuration: the repo default file is valid, the
P0 assignment is deepseek producer + GLM critic, and a same-family pair is
rejected at startup (BLOCKED) instead of running a fake adversarial loop.
"""
import copy

import pytest
import yaml

from lib import agent_models as am

_PRODUCER = {"model": "deepseek-v4.1-flash-1017", "family": "deepseek"}
_CRITIC = {"model": "glm-5.3", "family": "zhipu"}


def _roles() -> dict:
    return {
        role: {"producer": copy.deepcopy(_PRODUCER), "critic": copy.deepcopy(_CRITIC)}
        for role in am.P0_ROLES
    }


def _write(tmp_path, roles: dict) -> str:
    path = tmp_path / "agent_models.yaml"
    path.write_text(
        yaml.safe_dump({"version": "1.0", "roles": roles}, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def test_repo_config_is_valid_and_matches_p0_defaults():
    config = am.ensure_valid()
    assert set(config.roles) == set(am.P0_ROLES)
    for role in am.P0_ROLES:
        pair = config.pair(role)
        assert pair.producer.family != pair.critic.family
    script = config.pair("script")
    assert script.producer.model == "deepseek-v4.1-flash-1017"
    assert script.producer.family == "deepseek"
    assert script.critic.model == "glm-5.3"
    assert script.critic.family == "zhipu"


def test_same_family_pair_is_rejected(tmp_path):
    roles = _roles()
    roles["visual"]["critic"]["family"] = "deepseek"
    problems = am.validate(am.load(_write(tmp_path, roles)))
    assert len(problems) == 1
    assert "'visual'" in problems[0] and "deepseek" in problems[0]
    with pytest.raises(am.AgentModelConfigError, match="D19"):
        am.ensure_valid(_write(tmp_path, roles))


def test_missing_required_role_is_rejected(tmp_path):
    roles = _roles()
    del roles["director"]
    with pytest.raises(am.AgentModelConfigError, match="missing required role"):
        am.load(_write(tmp_path, roles))


def test_unknown_role_is_rejected(tmp_path):
    roles = _roles()
    roles["editor"] = {"producer": _PRODUCER, "critic": _CRITIC}
    with pytest.raises(am.AgentModelConfigError, match="unknown role"):
        am.load(_write(tmp_path, roles))


def test_missing_model_field_is_rejected(tmp_path):
    roles = _roles()
    del roles["shot"]["producer"]["family"]
    with pytest.raises(am.AgentModelConfigError, match="producer.family"):
        am.load(_write(tmp_path, roles))


def test_missing_config_file_is_rejected(tmp_path):
    with pytest.raises(am.AgentModelConfigError, match="not found"):
        am.load(tmp_path / "does-not-exist.yaml")


def test_unknown_role_lookup_raises():
    with pytest.raises(am.AgentModelConfigError, match="unknown role 'nope'"):
        am.ensure_valid().pair("nope")