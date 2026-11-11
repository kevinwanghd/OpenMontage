"""Shot-level human review for the short-drama pipeline (REV-01/02/03/13, ORC-04).

This is the SHOT-granularity review layer. Stage-level approval gates remain in
``scripts/pipeline_gate.py`` (``approve_stage`` / ``stage_gate``); this module does
not rewrite them. It manages an append-only ``reviews.jsonl`` per episode and is
the single mutation authority: both the CLI (``om review``) and the HTTP API call
:func:`ReviewStore.decide`, so they validate identically (X-11 parity).

Invariants enforced here:
- ``decide()`` is the only way a decision is recorded; it validates action,
  reason-code (required for reject, stage-applicable), candidate version (stale
  check) and human override evidence before appending.
- The log is append-only; corrections are ``undo`` compensation events that
  supersede a prior decision (REV-13), never rewrites.
- ``UNKNOWN`` is never silently treated as ``PASS``; a human may override an
  UNKNOWN only with recorded evidence (X-2).
- Sampling is deterministic (per-episode salt), so changing the ratio changes
  queue length only, never regenerates anything (REV-02/10).

M0: local files only, zero external API calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from lib.checkpoint import PROJECTS_DIR

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "artifacts" / "review_decision.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)

# Canonical reason codes (F27: MOTION_NATURAL corrected to MOTION_UNNATURAL;
# NARRATIVE_WEAK for BEAT-08 story quality; IDENTITY_UNKNOWN for unresolved face).
REASON_CODES = {
    "FACE_MISMATCH", "WARDROBE_DRIFT", "OFF_POSE", "LIP_SYNC_FAIL",
    "MOTION_WRONG", "ARTIFACT", "FRAMING_BAD", "LIGHTING_WRONG",
    "PACING", "DIALOGUE_CUT", "DIALECT_OFF", "MOTION_UNNATURAL",
    "CLIPPING", "SUBTITLE_MISALIGN", "HOOK_WEAK", "NARRATIVE_WEAK",
    "IDENTITY_UNKNOWN", "OTHER",
}

ACTIONS = {"accept", "reject", "pick_variant", "edit_prompt", "send_back", "skip"}

# Per-stage applicable reason codes (REV-03): a code outside the stage's set is
# rejected (e.g. PACING on shot_gen, SUBTITLE_MISALIGN on keyframe).
_COMMON = {"ARTIFACT", "OTHER"}
STAGE_REASON_CODES: dict[str, set[str]] = {
    "brief": {"HOOK_WEAK", "NARRATIVE_WEAK", "OTHER"},
    "beat_map": {"PACING", "HOOK_WEAK", "NARRATIVE_WEAK", "OTHER"},
    "script": {"PACING", "DIALOGUE_CUT", "DIALECT_OFF", "NARRATIVE_WEAK", "HOOK_WEAK", "OTHER"},
    "character_cast": {"FACE_MISMATCH", "WARDROBE_DRIFT", "IDENTITY_UNKNOWN", "OTHER"},
    "scene_plan": {"FRAMING_BAD", "PACING", "NARRATIVE_WEAK", "OTHER"},
    "keyframe": {"FACE_MISMATCH", "WARDROBE_DRIFT", "OFF_POSE", "FRAMING_BAD",
                 "LIGHTING_WRONG", "IDENTITY_UNKNOWN", "ARTIFACT", "OTHER"},
    "shot_gen": {"FACE_MISMATCH", "WARDROBE_DRIFT", "OFF_POSE", "LIP_SYNC_FAIL",
                 "MOTION_WRONG", "MOTION_UNNATURAL", "ARTIFACT", "FRAMING_BAD",
                 "LIGHTING_WRONG", "DIALOGUE_CUT", "IDENTITY_UNKNOWN", "OTHER"},
    "assembly": {"PACING", "DIALOGUE_CUT", "LIP_SYNC_FAIL", "SUBTITLE_MISALIGN",
                 "CLIPPING", "MOTION_UNNATURAL", "NARRATIVE_WEAK", "OTHER"},
    "delivery": {"SUBTITLE_MISALIGN", "CLIPPING", "FRAMING_BAD", "ARTIFACT", "OTHER"},
}


class ReviewError(Exception):
    """Base review error."""


class ReviewValidationError(ReviewError):
    """Invalid action / reason code / missing evidence."""


class StaleReviewError(ReviewError):
    """Decision targets a superseded candidate version (HTTP 409 STALE_REVIEW)."""


class UnknownReviewError(ReviewError):
    """review_id not found."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_bucket(salt: str, review_id: str) -> int:
    """Deterministic 0-99 bucket (process-independent, unlike hash())."""
    digest = hashlib.sha256(f"{salt}:{review_id}".encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


@dataclass
class ReviewPolicy:
    mode: str = "sampled"            # all | sampled
    detail_ratio: float = 0.35
    face_sim_threshold: float = 0.75
    always_detail: tuple[str, ...] = ("hook", "climax")
    sampling_salt: str = "default"


@dataclass
class ReviewItem:
    review_id: str
    project: str
    episode: str
    stage: str
    slot: str
    candidates: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    ref_context: dict[str, Any] = field(default_factory=dict)
    cost_if_approved_usd: float = 0.0
    priority: int = 0                # 0=must-view 1=recommended 2=skippable
    priority_reasons: list[str] = field(default_factory=list)
    face_similarity: Optional[float] = None
    candidate_version: str = "v1"
    review_version: str = "1"
    created_at: str = field(default_factory=_now)


@dataclass
class ReviewDecision:
    action: str
    chosen_candidate: Optional[str] = None
    reason_code: Optional[str] = None
    note: str = ""
    prompt_patch: Optional[str] = None
    override: bool = False
    evidence: str = ""
    decided_at: str = field(default_factory=_now)
    decided_by: str = "owner"


class ReviewStore:
    """Append-only per-episode review log. Single mutation authority."""

    _lock = threading.Lock()  # in-process single-writer discipline (ORC-10)

    def __init__(self, project: str, episode: str, projects_dir: Path = PROJECTS_DIR):
        self.project = project
        self.episode = episode
        self.dir = Path(projects_dir) / project / episode
        self.path = self.dir / "reviews.jsonl"

    # --- low-level append / read ---

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {"version": "1.0", "project": self.project, "episode": self.episode, **record}
        _VALIDATOR.validate(record)  # enforce the contract on every write
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # --- items & effective decisions ---

    def add_item(self, item: ReviewItem) -> dict[str, Any]:
        rec = asdict(item)
        rec["event_type"] = "item"
        return self._append(rec)

    def _effective(self) -> dict[str, dict[str, Any]]:
        """Fold the log: latest item snapshot + effective decision per review_id.

        An ``undo`` event nullifies the current effective decision (REV-13).
        """
        items: dict[str, dict] = {}
        decisions: dict[str, Optional[dict]] = {}
        for rec in self._records():
            rid = rec.get("review_id")
            et = rec.get("event_type")
            if et == "item":
                items[rid] = rec
                decisions.setdefault(rid, None)
            elif et == "decision":
                decisions[rid] = rec.get("decision")
            elif et == "undo":
                target = rec.get("supersedes") or rid
                decisions[target] = None
        return {rid: {"item": items[rid], "decision": decisions.get(rid)}
                for rid in items}

    def items(self) -> list[dict[str, Any]]:
        return [v["item"] for v in self._effective().values()]

    def get(self, review_id: str) -> dict[str, Any]:
        eff = self._effective().get(review_id)
        if not eff:
            raise UnknownReviewError(f"review_id '{review_id}' not found")
        return eff

    # --- queue (REV-02/10) ---

    def pending(self, policy: Optional[ReviewPolicy] = None) -> list[dict[str, Any]]:
        """Undecided items: all must-view + deterministic sample of the rest.

        Changing ``detail_ratio`` changes queue length only; nothing regenerates.
        """
        policy = policy or ReviewPolicy()
        undecided = [v["item"] for v in self._effective().values() if v["decision"] is None]
        must = [i for i in undecided if i.get("priority", 0) == 0]
        rest = [i for i in undecided if i.get("priority", 0) > 0]
        if policy.mode == "all":
            chosen = rest
        else:
            threshold = policy.detail_ratio * 100
            chosen = [i for i in rest if _stable_bucket(policy.sampling_salt, i["review_id"]) < threshold]
            target = math.ceil(len(undecided) * policy.detail_ratio)
            need = max(0, target - len(must) - len(chosen))
            if need:
                picked = {i["review_id"] for i in chosen}
                for i in sorted(rest, key=lambda x: (x.get("priority", 1), x.get("slot", ""))):
                    if need <= 0:
                        break
                    if i["review_id"] not in picked:
                        chosen.append(i)
                        picked.add(i["review_id"])
                        need -= 1
        order = {i["review_id"]: n for n, i in enumerate(
            sorted(must + chosen, key=lambda x: (x.get("priority", 0), x.get("slot", ""))))}
        return sorted(must + chosen, key=lambda x: order[x["review_id"]])

    # --- mutation authority ---

    def decide(self, review_id: str, decision: ReviewDecision,
               candidate_version: Optional[str] = None) -> dict[str, Any]:
        eff = self.get(review_id)
        item = eff["item"]
        stage = item.get("stage", "")

        if decision.action not in ACTIONS:
            raise ReviewValidationError(f"invalid action '{decision.action}'")
        if decision.action == "reject":
            if not decision.reason_code:
                raise ReviewValidationError("reject requires a reason_code")
            if decision.reason_code not in REASON_CODES:
                raise ReviewValidationError(f"unknown reason_code '{decision.reason_code}'")
            allowed = STAGE_REASON_CODES.get(stage, REASON_CODES)
            if decision.reason_code not in allowed:
                raise ReviewValidationError(
                    f"reason_code '{decision.reason_code}' not applicable to stage '{stage}'")
        if decision.action == "pick_variant":
            if not decision.chosen_candidate or decision.chosen_candidate not in item.get("candidates", []):
                raise ReviewValidationError("pick_variant requires a chosen_candidate among candidates")
        if decision.override and not decision.evidence:
            raise ReviewValidationError("override of UNKNOWN requires recorded evidence")

        ver = candidate_version or item.get("candidate_version")
        if ver and item.get("candidate_version") and ver != item.get("candidate_version"):
            raise StaleReviewError(
                f"candidate version '{ver}' != current '{item.get('candidate_version')}' (STALE_REVIEW)")

        rec = {
            "review_id": review_id, "stage": stage, "slot": item.get("slot"),
            "event_type": "decision",
            "candidate_version": item.get("candidate_version"),
            "review_version": item.get("review_version"),
            "decision": asdict(decision),
            "created_at": _now(),
        }
        return self._append(rec)

    def undo(self, review_id: str, reason: str = "") -> dict[str, Any]:
        item = self.get(review_id)["item"]  # must exist
        return self._append({
            "review_id": review_id, "stage": item.get("stage"), "slot": item.get("slot"),
            "event_type": "undo",
            "supersedes": review_id, "metadata": {"reason": reason},
            "created_at": _now(),
        })

    # --- stats (REV-01) ---

    def stats(self) -> dict[str, Any]:
        eff = self._effective()
        reason_dist: dict[str, int] = {}
        actions: dict[str, int] = {}
        decided = 0
        first_pass_accept = 0
        for v in eff.values():
            d = v["decision"]
            if not d:
                continue
            decided += 1
            act = d.get("action")
            actions[act] = actions.get(act, 0) + 1
            if act == "accept":
                first_pass_accept += 1
            rc = d.get("reason_code")
            if rc:
                reason_dist[rc] = reason_dist.get(rc, 0) + 1
        total = len(eff)
        return {
            "total_items": total,
            "decided": decided,
            "pending": total - decided,
            "action_distribution": actions,
            "reason_code_distribution": reason_dist,
            "first_pass_accept_rate": (first_pass_accept / decided) if decided else None,
        }