"""Deterministic creative-gate sampling (v2.5 DIR-04).

After the director agent auto-reviews a creative gate, a fraction of the
decisions is re-checked by a human. The selection is made by deterministic code
with a recorded seed, never by the agent: an agent that picks its own audit
sample can pick the cases it is confident about, which proves nothing.

The queue is independent of the technical sampling in ``ReviewPolicy``
(``detail_ratio``/``sampling_salt``); the creative ratio defaults to 0.2 and
lives in ``ReviewPolicy.creative_ratio``. Seeds and selections are appended to
``creative_samples.jsonl`` next to ``reviews.jsonl`` so any draw can be
replayed and audited.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

AUDIT_FILENAME = "creative_samples.jsonl"
DEFAULT_RATIO = 0.2


@dataclass(frozen=True)
class CreativeSample:
    """One recorded draw: which of the candidates get a human re-check."""

    ratio: float
    seed: int
    candidates: tuple[str, ...]
    chosen: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "ratio": self.ratio,
            "seed": self.seed,
            "candidates": list(self.candidates),
            "chosen": list(self.chosen),
        }


def _validate_ratio(ratio: float) -> float:
    value = float(ratio)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"creative sampling ratio {value} outside 0..1")
    return value


def select(
    review_ids: Iterable[str],
    ratio: float = DEFAULT_RATIO,
    seed: Optional[int] = None,
) -> CreativeSample:
    """Draw the creative-gate human sample. Same seed => same selection."""
    ratio = _validate_ratio(ratio)
    ids = sorted({str(rid) for rid in review_ids})
    if seed is None:
        # Random when first drawn, but returned so the draw can be replayed.
        seed = random.SystemRandom().randrange(2**32)
    count = min(len(ids), math.ceil(len(ids) * ratio)) if ratio > 0 else 0
    rng = random.Random(seed)
    chosen = tuple(sorted(rng.sample(ids, count))) if count else ()
    return CreativeSample(
        ratio=ratio, seed=int(seed), candidates=tuple(ids), chosen=chosen
    )


def replay(sample: CreativeSample) -> bool:
    """True when the recorded selection matches a fresh draw from its seed."""
    return select(sample.candidates, ratio=sample.ratio, seed=sample.seed).chosen == sample.chosen


def audit_path(episode_dir: Path | str) -> Path:
    return Path(episode_dir) / AUDIT_FILENAME


def write_audit(episode_dir: Path | str, sample: CreativeSample) -> Path:
    """Append the draw (ratio, seed, candidates, chosen) to the audit log."""
    path = audit_path(episode_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **sample.as_dict(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path