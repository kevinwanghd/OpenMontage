"""DirectorCritic peer review (v2.5 DIR-07, DUAL-07).

A peer critic's verdict is not final: the director's own critic can confirm or
overrule it, and the review is appended to ``director_reviews.jsonl`` so the
escalation is auditable instead of living in a chat log.

The director pair obeys D19 like every other role: ``agent_models`` validates
that the director producer and critic belong to different model families, and
this module refuses to start otherwise.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from lib import agent_models
from lib.dual_agent import CriticVerdict, GatewayTransport, Transport, parse_verdict

AUDIT_FILENAME = "director_reviews.jsonl"

PEER_SYSTEM = (
    "You are the DirectorCritic. A peer critic made the decision below. "
    "Confirm it only if the decision serves the director's brief and the "
    "episode's dramatic intent; overrule it when it rubber-stamps a flat cut, "
    "misses a broken hook, or enforces a rule that hurts the story. "
    "Reply with ONLY a JSON object, no prose and no code fence: "
    '{"verdict": "PASS" or "REJECT", "findings": [{"title": "...", '
    '"text": "...", "severity": "blocking" or "warning"}]}. '
    "Use REJECT with at least one blocking finding when the peer decision "
    "must not stand."
)


def review_peer(
    episode_dir: Path | str,
    review_id: str,
    peer_summary: str,
    question: str,
    *,
    transport: Optional[Transport] = None,
    config: Optional[agent_models.AgentModelConfig] = None,
) -> CriticVerdict:
    """Let the DirectorCritic confirm/overrule a peer decision (DUAL-07)."""
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
    raw = transport.complete(pair.critic.model, PEER_SYSTEM, prompt)
    verdict = parse_verdict(raw)

    record: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "review_id": review_id,
        "model": pair.critic.model,
        "peer_summary": peer_summary,
        "question": question,
        "verdict": verdict.as_dict(),
    }
    path = Path(episode_dir) / AUDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return verdict


def read_reviews(episode_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(episode_dir) / AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]