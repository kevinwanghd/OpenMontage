"""Dual-agent model calls and verdict parsing (v2.5 D18/D19, DUAL-05).

This module is a tool layer, not an orchestrator (AGENT_GUIDE: Python = tools
+ persistence; creative decisions, review logic and checkpoint policy stay in
the skill/agent layer). It offers three things:

- ``GatewayTransport``: one model call to ``{ANTHROPIC_BASE_URL}/v1/messages``
  (v2.5 §4.2: the Agent tool's model enum only knows Claude names, so dual
  agents call the gateway directly with an explicit model id);
- ``parse_verdict``: validate the critic's JSON contract;
- ``DualAgent``: a convenience that makes the producer call and the critic
  call in sequence. The verdict comes from the critic model; this module
  never decides whether a stage ships (no checkpoint/human-approval logic).

The instruction templates live in the skill layer
(``skills/pipelines/short-drama/dual-agent-prompts.yaml``) and are loaded
here, so editing review criteria does not touch Python.

Critic contract (DUAL-03): the critic answers with a JSON object

    {"verdict": "PASS"|"REJECT",
     "findings": [{"title": ..., "text": ..., "severity": "blocking"|"warning"}]}

A REJECT must carry at least one blocking finding; a PASS must not. A
malformed answer is retried once with a stricter instruction, then raises
:class:`DualAgentError` (fail closed, never a silent pass).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

import yaml

from lib import agent_models

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 180.0


class DualAgentError(RuntimeError):
    """The dual-agent loop could not produce a trustworthy verdict."""


@dataclass(frozen=True)
class Finding:
    title: str
    text: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "text": self.text, "severity": self.severity}


@dataclass(frozen=True)
class CriticVerdict:
    verdict: str
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "blocking")

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class DualAgentResult:
    role: str
    producer_output: str
    verdict: CriticVerdict
    critic_raw: str


class Transport(Protocol):
    """One call: send a system + user prompt to a model, return its text."""

    def complete(self, model: str, system: str, prompt: str) -> str: ...


class GatewayTransport:
    """Anthropic-messages call against the MaaS gateway."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
        ).rstrip("/")
        # Two credential styles: an auth token goes in Authorization: Bearer
        # (Anthropic convention for gateway tokens), an API key goes in
        # x-api-key. Sending a token as x-api-key makes the gateway reject or
        # misroute it, so keep the two paths separate.
        self.auth_token = auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _auth_headers(self) -> dict[str, str]:
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        if self.api_key:
            return {"x-api-key": self.api_key}
        raise DualAgentError("ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY is not set")

    def complete(self, model: str, system: str, prompt: str) -> str:
        if not self.base_url:
            raise DualAgentError("ANTHROPIC_BASE_URL is not set")
        scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        if scheme != "https":
            # Credentials must never travel over a plaintext link.
            raise DualAgentError(
                f"ANTHROPIC_BASE_URL must use https, got {scheme or 'no scheme'}: "
                "refusing to send credentials over an unencrypted connection"
            )
        body = json.dumps(
            {
                "model": model,
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "anthropic-version": ANTHROPIC_VERSION,
                **self._auth_headers(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise DualAgentError(f"gateway HTTP {error.code} for model {model}") from error
        except urllib.error.URLError as error:
            raise DualAgentError(f"gateway unreachable for model {model}: {error}") from error
        # Usage is exposed for the cost ledger (DUAL-08); callers without a
        # gateway (fakes in tests) simply have no last_usage.
        self.last_usage = payload.get("usage") or {}
        return _message_text(payload, model)


def _message_text(payload: Mapping[str, Any], model: str) -> str:
    stop_reason = str(payload.get("stop_reason") or "")
    if stop_reason in {"max_tokens", "length"}:
        # A truncated producer/critic answer must not be treated as complete:
        # the critic would review half a deliverable and the producer would
        # ship a cut-off script.
        raise DualAgentError(
            f"model {model} output was truncated (stop_reason={stop_reason}); "
            "raise max_tokens or shorten the task"
        )
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise DualAgentError(f"gateway response for {model} has no content blocks")
    text = "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    if not text.strip():
        raise DualAgentError(f"gateway returned empty text for {model}")
    return text


_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS_PATH = (
    _REPO_ROOT / "skills" / "pipelines" / "short-drama" / "dual-agent-prompts.yaml"
)
PROMPTS_PATH_ENV = "DUAL_AGENT_PROMPTS_PATH"


@dataclass(frozen=True)
class PromptTemplates:
    """Instruction templates loaded from the skill layer."""

    producer_system: str
    critic_system: str
    critic_retry_suffix: str = ""


def load_prompts(path: Optional[Path | str] = None) -> PromptTemplates:
    """Load producer/critic instructions from the skill-owned YAML file."""
    prompt_path = Path(path) if path else Path(
        os.environ.get(PROMPTS_PATH_ENV, DEFAULT_PROMPTS_PATH)
    )
    if not prompt_path.is_file():
        raise DualAgentError(f"dual-agent prompts not found: {prompt_path}")
    try:
        data = yaml.safe_load(prompt_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise DualAgentError(f"dual-agent prompts are not valid YAML: {error}") from error
    producer = str(data.get("producer_system") or "").strip()
    critic = str(data.get("critic_system") or "").strip()
    if not producer or not critic:
        raise DualAgentError(
            f"dual-agent prompts need producer_system and critic_system: {prompt_path}"
        )
    return PromptTemplates(
        producer_system=producer,
        critic_system=critic,
        critic_retry_suffix=str(data.get("critic_retry_suffix") or ""),
    )


def build_critic_prompt(task: str, producer_output: str) -> str:
    return (
        "TASK GIVEN TO THE PRODUCER:\n"
        f"{task.strip()}\n\n"
        "PRODUCER'S WORK:\n"
        f"{producer_output.strip()}\n\n"
        "Review the work and answer with the JSON verdict."
    )


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _json_candidate(text: str) -> str:
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise DualAgentError("critic answer contains no JSON object")
    return text[start : end + 1]


def parse_verdict(text: str) -> CriticVerdict:
    """Parse the critic's structured verdict; malformed answers raise."""
    try:
        payload = json.loads(_json_candidate(text))
    except json.JSONDecodeError as error:
        raise DualAgentError(f"critic answer is not valid JSON: {error}") from error
    if not isinstance(payload, Mapping):
        raise DualAgentError("critic answer must be a JSON object")

    verdict = str(payload.get("verdict", "")).strip().upper()
    if verdict not in {"PASS", "REJECT"}:
        raise DualAgentError("critic verdict must be PASS or REJECT")

    raw_findings = payload.get("findings", [])
    if not isinstance(raw_findings, list):
        raise DualAgentError("critic findings must be a list")
    findings: list[Finding] = []
    for item in raw_findings:
        if not isinstance(item, Mapping):
            raise DualAgentError("each critic finding must be an object")
        title = str(item.get("title", "")).strip()
        body = str(item.get("text", "")).strip()
        severity = str(item.get("severity", "warning")).strip().lower()
        if not title or not body:
            raise DualAgentError("each critic finding needs title and text")
        if severity not in {"blocking", "warning"}:
            raise DualAgentError(f"unknown finding severity {severity!r}")
        findings.append(Finding(title=title, text=body, severity=severity))

    blocking = [f for f in findings if f.severity == "blocking"]
    if verdict == "REJECT" and not blocking:
        raise DualAgentError("REJECT requires at least one blocking finding")
    if verdict == "PASS" and blocking:
        raise DualAgentError("PASS contradicts a blocking finding")
    return CriticVerdict(verdict=verdict, findings=tuple(findings))


class DualAgent:
    """Run one producer pass and one adversarial critic pass for a role."""

    def __init__(
        self,
        role: str,
        *,
        config: Optional[agent_models.AgentModelConfig] = None,
        transport: Optional[Transport] = None,
        producer_system: Optional[str] = None,
        critic_system: Optional[str] = None,
        critic_attempts: int = 2,
        prompts: Optional[PromptTemplates] = None,
        episode_dir: Optional[Path | str] = None,
    ) -> None:
        config = config or agent_models.load()
        problems = agent_models.validate(config)
        if problems:
            # Validate even when the caller supplies a config object: D19 is a
            # startup gate, not a property of how the config was obtained.
            raise agent_models.AgentModelConfigError(
                "agent model configuration is unusable (D19): " + "; ".join(problems)
            )
        self.role = role
        self.pair = config.pair(role)  # raises for unknown roles
        self.transport = transport or GatewayTransport()
        templates = prompts or load_prompts()
        self.producer_system = (producer_system or templates.producer_system).replace("{role}", role)
        self.critic_system = (critic_system or templates.critic_system).replace("{role}", role)
        self.critic_retry_suffix = templates.critic_retry_suffix
        self.critic_attempts = max(1, critic_attempts)
        # DUAL-06/08: when set, REJECTs land in the rejection library and
        # every run appends both sides to the cost ledger.
        self.episode_dir = Path(episode_dir) if episode_dir else None

    def run(self, task: str) -> DualAgentResult:
        producer_output = self.transport.complete(
            self.pair.producer.model, self.producer_system, task
        )
        producer_tokens = self._usage_tokens()
        if not producer_output.strip():
            raise DualAgentError(f"{self.role} producer returned empty output")

        critic_prompt = build_critic_prompt(task, producer_output)
        last_error: Optional[Exception] = None
        for attempt in range(self.critic_attempts):
            suffix = self.critic_retry_suffix if attempt else ""
            raw = self.transport.complete(
                self.pair.critic.model, self.critic_system, critic_prompt + suffix
            )
            try:
                verdict = parse_verdict(raw)
            except DualAgentError as error:
                last_error = error
                continue
            result = DualAgentResult(
                role=self.role,
                producer_output=producer_output,
                verdict=verdict,
                critic_raw=raw,
            )
            if self.episode_dir is not None:
                self._record_side_effects(
                    result, producer_tokens, self._usage_tokens(), task
                )
            return result
        raise DualAgentError(
            f"{self.role} critic produced no valid verdict after "
            f"{self.critic_attempts} attempts: {last_error}"
        )

    def _usage_tokens(self) -> int:
        """Total tokens of the last transport call, when the gateway reports usage."""
        usage = getattr(self.transport, "last_usage", None) or {}
        return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)

    def _record_side_effects(
        self,
        result: DualAgentResult,
        producer_tokens: int,
        critic_tokens: int,
        task: str,
    ) -> None:
        """DUAL-06/08: persist the REJECT and the pair cost (best effort).

        The verdict is already trustworthy when we get here, so a ledger
        failure must not discard the run: warn and keep going.
        """
        import warnings

        from lib import dual_rejections

        try:
            if not result.verdict.passed:
                dual_rejections.record_rejection(
                    self.episode_dir,
                    role=self.role,
                    reason_code="OTHER",
                    findings=[finding.as_dict() for finding in result.verdict.findings],
                    model=self.pair.producer.model,
                    prompt=task,
                )
            dual_rejections.record_pair_cost(
                self.episode_dir,
                role=self.role,
                producer_model=self.pair.producer.model,
                critic_model=self.pair.critic.model,
                producer_tokens=producer_tokens,
                critic_tokens=critic_tokens,
                # Token counts are real when the gateway reports usage; the
                # cash figure stays with the billing-aware caller (DUAL-08).
                producer_cost_usd=0.0,
                critic_cost_usd=0.0,
            )
        except (OSError, ValueError) as error:
            warnings.warn(
                f"dual-agent ledger write failed for role '{self.role}': {error}; "
                "the verdict stands but rejections/cost are not recorded",
                stacklevel=2,
            )