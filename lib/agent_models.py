"""Agent model configuration and D19 family validation (v2.5 DUAL-02/09).

Each short-drama production role runs as a producer (does the work) and a
critic (adversarially reviews it). D19 makes model-family diversity a hard
constraint: a same-family pair shares training blind spots and degenerates
into rubber-stamping, so :func:`ensure_valid` refuses to start instead of
running a fake adversarial loop.

The configuration lives in ``bindings/agent_models.yaml``. This module is the
single validation authority: structural checks (required roles, model/family
present) plus the cross-family constraint. A violation raises
:class:`AgentModelConfigError`, which callers surface as BLOCKED (MOD-05),
never as a silent fallback.

Pure local logic: no network calls, no spend.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

# PyYAML is already a declared dependency (requirements.txt: pyyaml>=6.0,
# setup.py install_requires); this module adds no new dependency.
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "bindings" / "agent_models.yaml"
CONFIG_PATH_ENV = "AGENT_MODELS_PATH"

# P0 merges the seven functional roles into five (D16).
P0_ROLES = ("script", "visual", "shot", "edit_sound", "director")


class AgentModelConfigError(ValueError):
    """The agent model configuration is unusable; the pipeline must BLOCK."""


@dataclass(frozen=True)
class AgentModel:
    """One concrete model with the family used for the D19 check."""

    model: str
    family: str

    def as_dict(self) -> dict[str, str]:
        return {"model": self.model, "family": self.family}


@dataclass(frozen=True)
class RoleModels:
    """The producer/critic pair for one production role."""

    role: str
    producer: AgentModel
    critic: AgentModel

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "producer": self.producer.as_dict(),
            "critic": self.critic.as_dict(),
        }


@dataclass(frozen=True)
class AgentModelConfig:
    version: str
    roles: dict[str, RoleModels]

    def pair(self, role: str) -> RoleModels:
        try:
            return self.roles[role]
        except KeyError:
            raise AgentModelConfigError(
                f"unknown role '{role}' (P0 roles: {', '.join(P0_ROLES)})"
            ) from None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "roles": {role: models.as_dict() for role, models in self.roles.items()},
        }


def _config_path(path: Optional[os.PathLike[str] | str] = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get(CONFIG_PATH_ENV)
    return Path(env).expanduser() if env else DEFAULT_CONFIG_PATH


def _parse_model(role: str, side: str, payload: Any) -> AgentModel:
    if not isinstance(payload, Mapping):
        raise AgentModelConfigError(f"role '{role}' {side} must be a mapping")
    values = {}
    for field in ("model", "family"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AgentModelConfigError(
                f"role '{role}' {side}.{field} must be a non-empty string"
            )
        values[field] = value.strip()
    return AgentModel(**values)


def load(path: Optional[os.PathLike[str] | str] = None) -> AgentModelConfig:
    """Read and structurally parse the agent model configuration."""
    config_path = _config_path(path)
    if not config_path.is_file():
        raise AgentModelConfigError(f"agent model config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise AgentModelConfigError(f"agent model config is not valid YAML: {error}") from error
    if not isinstance(raw, Mapping):
        raise AgentModelConfigError("agent model config must be a mapping")

    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise AgentModelConfigError("agent model config needs a non-empty version")

    raw_roles = raw.get("roles")
    if not isinstance(raw_roles, Mapping) or not raw_roles:
        raise AgentModelConfigError("agent model config needs a non-empty roles mapping")

    unknown = sorted(set(raw_roles) - set(P0_ROLES))
    if unknown:
        raise AgentModelConfigError(
            f"unknown role(s) {', '.join(map(repr, unknown))} "
            f"(P0 roles: {', '.join(P0_ROLES)})"
        )
    missing = sorted(set(P0_ROLES) - set(raw_roles))
    if missing:
        raise AgentModelConfigError(
            f"missing required role(s) {', '.join(map(repr, missing))}"
        )

    roles: dict[str, RoleModels] = {}
    for role in P0_ROLES:
        entry = raw_roles[role]
        if not isinstance(entry, Mapping):
            raise AgentModelConfigError(f"role '{role}' must be a mapping")
        if "producer" not in entry or "critic" not in entry:
            raise AgentModelConfigError(
                f"role '{role}' needs both producer and critic"
            )
        roles[role] = RoleModels(
            role=role,
            producer=_parse_model(role, "producer", entry["producer"]),
            critic=_parse_model(role, "critic", entry["critic"]),
        )
    return AgentModelConfig(version=version.strip(), roles=roles)


def validate(config: AgentModelConfig) -> list[str]:
    """Return D19 violations; an empty list means the config is usable."""
    problems: list[str] = []
    for role in P0_ROLES:
        pair = config.roles.get(role)
        if pair is None:
            problems.append(f"missing required role '{role}'")
            continue
        if pair.producer.family.casefold() == pair.critic.family.casefold():
            problems.append(
                f"role '{role}': producer and critic share model family "
                f"'{pair.producer.family}' (D19 requires different families)"
            )
    return problems


def ensure_valid(
    path: Optional[os.PathLike[str] | str] = None,
) -> AgentModelConfig:
    """Startup gate: load, validate, and BLOCK on any violation."""
    config = load(path)
    problems = validate(config)
    if problems:
        raise AgentModelConfigError(
            "agent model configuration is unusable (D19): " + "; ".join(problems)
        )
    return config