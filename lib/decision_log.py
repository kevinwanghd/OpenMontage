"""Append-only decision log for production-wide choices (ORC-04, F32).

Truth-source split (ORC-04): per-SHOT accept/reject decisions belong in
``reviews.jsonl`` (see :mod:`lib.review`). Everything that is not a shot
review -- provider/profile selection, downgrade approval, budget trade-offs
and render-runtime selection -- belongs HERE. Keeping the two apart means a
shot decision can never be silently recorded as a pipeline decision and the
category vocabulary stays closed.

The category enum is CLOSED to the four names mapped by ORC-04. Unknown
categories are rejected, never invented at call sites (F32). Records are
validated against ``schemas/artifacts/decision_log.schema.json``
(``$defs/shortdrama_decision_record``) on every write, so schema and code
cannot drift.

The log is append-only: corrections are new records, never rewrites.

M0: local files only, zero external API calls.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from lib.checkpoint import PROJECTS_DIR

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "artifacts" / "decision_log.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_RECORD_SCHEMA = _SCHEMA["$defs"]["shortdrama_decision_record"]
_VALIDATOR = Draft202012Validator(_RECORD_SCHEMA)

# ORC-04 closed mapping. Do not add names here without updating the schema
# enum and the requirement mapping together (F32: no invented categories).
DECISION_CATEGORIES = frozenset({
    "provider_selection",
    "downgrade_approval",
    "budget_tradeoff",
    "render_runtime_selection",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionLogError(Exception):
    """Base decision-log error."""


class DecisionValidationError(DecisionLogError):
    """Unknown category / missing field / schema violation."""


class UnknownDecisionError(DecisionLogError):
    """decision_id not found."""


@dataclass
class DecisionEntry:
    """One immutable decision record.

    ``alternatives`` lists every option that was considered (including the
    rejected ones) so the audit trail shows what was NOT chosen and why.
    ``episode_id`` / ``shot_id`` scope the decision; both are optional because
    profile/TTS/runtime choices may apply project-wide.
    """

    category: str
    subject: str
    decision: str
    reason: str
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    episode_id: Optional[str] = None
    shot_id: Optional[str] = None
    operator: str = "owner"
    metadata: dict[str, Any] = field(default_factory=dict)
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_now)


class DecisionLog:
    """Append-only per-project decision log. Single writer (ORC-10)."""

    _lock = threading.Lock()

    def __init__(self, project: str, projects_dir: Path = PROJECTS_DIR):
        self.project = project
        self.dir = Path(projects_dir) / project
        self.path = self.dir / "decision_log.jsonl"

    # --- low-level append / read ---

    def append(self, entry: DecisionEntry) -> dict[str, Any]:
        if entry.category not in DECISION_CATEGORIES:
            raise DecisionValidationError(
                f"unknown category '{entry.category}'; allowed: {sorted(DECISION_CATEGORIES)}")
        rec = {
            "version": "1.0",
            "project": self.project,
            **asdict(entry),
        }
        _VALIDATOR.validate(rec)  # enforce the contract on every write
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # --- query (ORC-04: by episode / category / time) ---

    def query(self, *, category: Optional[str] = None,
              episode_id: Optional[str] = None,
              shot_id: Optional[str] = None,
              since: Optional[str] = None,
              until: Optional[str] = None) -> list[dict[str, Any]]:
        """Filter the append-only log without mutating it.

        ``since``/``until`` are ISO-8601 strings compared lexicographically
        against ``created_at``; a bare date (``2026-11-11``) works as a bound.
        """
        out = []
        for rec in self.entries():
            if category is not None and rec.get("category") != category:
                continue
            if episode_id is not None and rec.get("episode_id") != episode_id:
                continue
            if shot_id is not None and rec.get("shot_id") != shot_id:
                continue
            if since is not None and rec.get("created_at", "") < since:
                continue
            if until is not None and rec.get("created_at", "") > until:
                continue
            out.append(rec)
        return out

    def get(self, decision_id: str) -> dict[str, Any]:
        for rec in self.entries():
            if rec.get("decision_id") == decision_id:
                return rec
        raise UnknownDecisionError(f"decision_id '{decision_id}' not found")

    def counts_by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.entries():
            cat = rec["category"]
            counts[cat] = counts.get(cat, 0) + 1
        return counts