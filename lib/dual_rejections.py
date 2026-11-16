"""Critic rejections, regeneration hints and pair cost (v2.5 DUAL-06/08).

A critic REJECT is only useful if it survives the turn: the findings, the
reason code, the model that produced the work, and the media it judged are
appended to the episode's rejection library (LIB-06) so regeneration can read
the accumulated strategy (REV-05) instead of rerolling blind.

Dual-agent spend is accounted twice per run (producer + critic), because a
cost ledger that hides the critic under-reports the real cost of the gate.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# lib.review_strategies already exists on main (REV-05 canonical strategy
# table, used by lib/review.py and the review CLI); this module only imports
# it. Nothing in this change adds or moves that module.
from lib.review_strategies import STRATEGIES, RegenerationStrategy, strategy_for

REJECTIONS_FILENAME = "rejections.jsonl"
COST_FILENAME = "agent_cost.jsonl"


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def record_rejection(
    episode_dir: Path | str,
    *,
    role: str,
    reason_code: str,
    findings: Iterable[Mapping[str, Any]],
    model: str,
    prompt: str = "",
    media: Iterable[str] = (),
) -> Path:
    """Append one critic REJECT to the episode rejection library (LIB-06)."""
    if not role.strip():
        raise ValueError("rejection needs the role that was rejected")
    if reason_code not in STRATEGIES:
        raise ValueError(
            f"unknown reason_code {reason_code!r}; register it in the REV-03 "
            "table before persisting a rejection"
        )
    items = []
    for finding in findings:
        item = dict(finding)
        severity = str(item.get("severity", "warning")).lower()
        if severity not in {"blocking", "warning"}:
            raise ValueError(
                f"finding severity {severity!r} must be blocking or warning"
            )
        item["severity"] = severity
        items.append(item)
    if not items:
        raise ValueError("rejection needs at least one finding")
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "reason_code": reason_code,
        "findings": items,
        "model": model,
        "prompt": prompt,
        "media": list(media),
    }
    return _append_jsonl(Path(episode_dir) / REJECTIONS_FILENAME, record)


def read_rejections(episode_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(episode_dir) / REJECTIONS_FILENAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def query_rejections(
    episode_dir: Path | str, *, reason_code: Optional[str] = None, role: Optional[str] = None
) -> list[dict[str, Any]]:
    """Filter the rejection library by reason code and/or role (LIB-06)."""
    rows = read_rejections(episode_dir)
    if reason_code is not None:
        rows = [row for row in rows if row.get("reason_code") == reason_code]
    if role is not None:
        rows = [row for row in rows if row.get("role") == role]
    return rows


def regeneration_hint(reason_code: str) -> RegenerationStrategy:
    """The documented regeneration path the producer must read (REV-05)."""
    return strategy_for(reason_code)


def record_pair_cost(
    episode_dir: Path | str,
    *,
    role: str,
    producer_model: str,
    critic_model: str,
    producer_tokens: int,
    critic_tokens: int,
    producer_cost_usd: float,
    critic_cost_usd: float,
    pair_id: Optional[str] = None,
) -> Path:
    """Append both sides of one dual-agent run (DUAL-08, NFR-01).

    Each row carries that side's own cost, so summing ``cost_usd`` over rows
    yields the true total (a single shared value would double-count when the
    ledger is aggregated). ``pair_id`` ties the two rows together.
    """
    if producer_tokens < 0 or critic_tokens < 0:
        raise ValueError("dual-agent token counts cannot be negative")
    if producer_cost_usd < 0 or critic_cost_usd < 0:
        raise ValueError("dual-agent cost cannot be negative")
    now = datetime.now(timezone.utc).isoformat()
    pair_id = pair_id or uuid.uuid4().hex
    path = Path(episode_dir) / COST_FILENAME
    for actor, model, tokens, cost in (
        ("producer", producer_model, producer_tokens, producer_cost_usd),
        ("critic", critic_model, critic_tokens, critic_cost_usd),
    ):
        _append_jsonl(
            path,
            {
                "created_at": now,
                "pair_id": pair_id,
                "role": role,
                "actor": actor,
                "model": model,
                "tokens": int(tokens),
                "cost_usd": float(cost),
            },
        )
    return path