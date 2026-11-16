"""Dual-agent runner (v2.5 D18/D19, DUAL-05).

Each production role runs as a producer that does the work and a critic that
adversarially reviews it. The pair comes from ``bindings/agent_models.yaml``
and is validated at construction time: a same-family pair raises instead of
running a fake review.

Transport is injectable so tests never touch the network. The default
``GatewayTransport`` posts to ``{ANTHROPIC_BASE_URL}/v1/messages`` in the
Anthropic messages format (v2.5 §4.2: the Agent tool's model enum only knows
Claude names, so dual agents call the gateway directly with an explicit
model id).

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
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

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
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete(self, model: str, system: str, prompt: str) -> str:
        if not self.base_url:
            raise DualAgentError("ANTHROPIC_BASE_URL is not set")
        if not self.api_key:
            raise DualAgentError("ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY is not set")
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
                "x-api-key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise DualAgentError(f"gateway HTTP {error.code} for model {model}") from error
        except urllib.error.URLError as error:
            raise DualAgentError(f"gateway unreachable for model {model}: {error}") from error
        return _message_text(payload, model)


def _message_text(payload: Mapping[str, Any], model: str) -> str:
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


PRODUCER_SYSTEM = (
    "You are the {role} producer on an English-language vertical short drama. "
    "Do the assigned work directly and completely: produce the deliverable, not "
    "a plan, commentary, or apology. Keep every line speakable out loud and "
    "under 20 words. The audience watches the first 2-3 seconds before deciding."
)

CRITIC_SYSTEM = (
    "You are the {role} critic: an English-native story editor reviewing the "
    "producer's work adversarially. Your job is to find what is flat, "
    "unearned, out of character, or unclear to a stranger - not to praise it. "
    "Check the hook (visible conflict in the first 2-3 seconds), causality "
    "(each turn pushed by prior events), dialogue (spoken language, actable, "
    "under 20 words per line), and cultural fit for the target platform. "
    "Reply with ONLY a JSON object, no prose and no code fence: "
    '{"verdict": "PASS" or "REJECT", "findings": [{"title": "...", '
    '"text": "...", "severity": "blocking" or "warning"}]}. '
    "Use REJECT with at least one blocking finding when the work cannot ship; "
    "use PASS with zero blocking findings only when it can."
)

CRITIC_RETRY_SUFFIX = (
    "\n\nYour previous answer was not valid JSON. Reply with ONLY the JSON "
    "object described above: no prose, no code fence."
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
        self.producer_system = (producer_system or PRODUCER_SYSTEM).replace("{role}", role)
        self.critic_system = (critic_system or CRITIC_SYSTEM).replace("{role}", role)
        self.critic_attempts = max(1, critic_attempts)

    def run(self, task: str) -> DualAgentResult:
        producer_output = self.transport.complete(
            self.pair.producer.model, self.producer_system, task
        )
        if not producer_output.strip():
            raise DualAgentError(f"{self.role} producer returned empty output")

        critic_prompt = build_critic_prompt(task, producer_output)
        last_error: Optional[Exception] = None
        for attempt in range(self.critic_attempts):
            suffix = CRITIC_RETRY_SUFFIX if attempt else ""
            raw = self.transport.complete(
                self.pair.critic.model, self.critic_system, critic_prompt + suffix
            )
            try:
                verdict = parse_verdict(raw)
            except DualAgentError as error:
                last_error = error
                continue
            return DualAgentResult(
                role=self.role,
                producer_output=producer_output,
                verdict=verdict,
                critic_raw=raw,
            )
        raise DualAgentError(
            f"{self.role} critic produced no valid verdict after "
            f"{self.critic_attempts} attempts: {last_error}"
        )

    @staticmethod
    def require_pass(result: DualAgentResult, stage: str = "script") -> None:
        """Gate helper: a REJECT blocks the stage; findings are actionable."""
        if result.verdict.passed:
            return
        details = "; ".join(
            f"{finding.title} ({finding.severity})" for finding in result.verdict.findings
        )
        raise DualAgentError(f"{stage} gate REJECT: {details}")