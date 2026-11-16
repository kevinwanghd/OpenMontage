"""DirectorCritic peer review (v2.5 DIR-07, DUAL-07).

A peer critic's verdict is not final: the director's own critic can confirm or
overrule it, and the review is appended to ``director_reviews.jsonl`` so the
escalation is auditable instead of living in a chat log.

This module only records an advisory peer-review verdict. It does not close
stage gates, approve deliverables, or bypass any human approval: disputes still
escalate to a human (D15), and the stage checkpoint remains the authority.
The instruction template lives in the skill layer
(``skills/pipelines/short-drama/dual-agent-prompts.yaml``), not in Python.

The director pair obeys D19 like every other role: ``agent_models`` validates
that the director producer and critic belong to different model families, and
this module refuses to start otherwise.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from lib import agent_models
from lib.dual_agent import CriticVerdict, GatewayTransport, Transport, parse_verdict

AUDIT_FILENAME = "director_reviews.jsonl"
DEFAULT_PROMPTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills" / "pipelines" / "short-drama" / "dual-agent-prompts.yaml"
)
PROMPTS_PATH_ENV = "DUAL_AGENT_PROMPTS_PATH"


def _load_peer_system(path: Optional[Path | str] = None) -> str:
    """Load the DirectorCritic instruction from the skill layer."""
    prompt_path = Path(path) if path else Path(
        os.environ.get(PROMPTS_PATH_ENV, DEFAULT_PROMPTS_PATH)
    )
    if not prompt_path.is_file():
        raise ValueError(f"dual-agent prompts not found: {prompt_path}")
    data = yaml.safe_load(prompt_path.read_text(encoding="utf-8")) or {}
    system = str(data.get("director_critic_system") or "").strip()
    if not system:
        raise ValueError(f"director_critic_system missing in {prompt_path}")
    return system


def record_peer_review(
    episode_dir: Path | str,
    review_id: str,
    peer_summary: str,
    question: str,
    raw_verdict: str,
    model: str,
) -> CriticVerdict:
    """Parse a critic answer and append the audit record (no model call)."""
    if not review_id.strip():
        raise ValueError("director peer review needs a review_id")
    if not peer_summary.strip() or not question.strip():
        raise ValueError("director peer review needs both the peer summary and the question")
    verdict = parse_verdict(raw_verdict)
    record: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "review_id": review_id,
        "model": model,
        "peer_summary": peer_summary,
        "question": question,
        "verdict": verdict.as_dict(),
    }
    path = Path(episode_dir) / AUDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return verdict


def review_peer(
    episode_dir: Path | str,
    review_id: str,
    peer_summary: str,
    question: str,
    *,
    transport: Optional[Transport] = None,
    config: Optional[agent_models.AgentModelConfig] = None,
) -> CriticVerdict:
    """Let the DirectorCritic confirm/overrule a peer decision (DUAL-07).

    Convenience composition: one critic-model call, then
    :func:`record_peer_review` parses and persists it. The verdict is the
    model's; this module makes no stage/checkpoint decision.
    """
    if not review_id.strip():
        raise ValueError("director peer review needs a review_id")
    if not peer_summary.strip() or not question.strip():
        raise ValueError("director peer review needs both the peer summary and the question")
    config = config or agent_models.load()
    problems = agent_models.validate(config)
    if problems:
        raise agent_models.AgentModelConfigError(
            "agent model configuration is unusable (D19): " + "; ".join(problems)
        )
    pair = config.pair("director")
    transport = transport or GatewayTransport()

    prompt = (
        "PEER DECISION:\n"
        f"{peer_summary.strip()}\n\n"
        "QUESTION FOR THE DIRECTOR CRITIC:\n"
        f"{question.strip()}\n\n"
        "Reply with the JSON verdict."
    )
    raw = transport.complete(pair.critic.model, _load_peer_system(), prompt)
    return record_peer_review(
        episode_dir, review_id, peer_summary, question, raw, pair.critic.model
    )


def read_reviews(episode_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(episode_dir) / AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]