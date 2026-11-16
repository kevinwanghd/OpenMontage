"""Shot-level human review for the short-drama pipeline (REV-01/02/03/05/06/10/13/14, ORC-04).

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
  queue length only, never regenerates anything (REV-02/10). Policy changes are
  persisted beside the log and never touch items or prior decisions.
- Rejections trip a per-shot circuit breaker at N=3 (REV-06); the streak and
  its reset rules live in :mod:`lib.review_strategies`.
- The log is the review timing source of truth (REV-14): item ``created_at`` is
  the open time, decision ``decided_at`` the close time; stats expose
  items/minute and L1/L2/L3 minutes. Timing stays local (X-15).

M0: local files only, zero external API calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from lib.checkpoint import PROJECTS_DIR
from lib.review_strategies import effective_decision_chain
from lib.review_strategies import (
    BLOCKED_WHEN_TRIPPED, BreakerState, RetryPolicy,
    effective_decision_chain, evaluate_breaker,
)

POLICY_FILE = "review_policy.json"

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


class CircuitBreakerOpenError(ReviewError):
    """Shot hit the consecutive-reject limit; send_back or human override (REV-06)."""

    def __init__(self, review_id: str, limit: int, streak: int):
        self.review_id = review_id
        self.limit = limit
        self.consecutive_rejects = streak
        super().__init__(
            f"circuit breaker open for '{review_id}': {streak} consecutive rejects "
            f"(limit {limit}); send_back or override with recorded evidence")


class UnknownReviewError(ReviewError):
    """review_id not found."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp; naive values are treated as UTC (REV-14)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # risk:swallowed-exception reason:"时间戳来自 append-only 审核日志的既有记录，无法解析时按缺失计时处理以免整份吞吐统计失败，原始字符串仍原样保留在事件里" owner:@kevin reviewed:2026-11-11
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _layer(priority: Any) -> str:
    """Map item priority to the layered review model: L1 must-view, L2, L3."""
    try:
        p = int(priority or 0)
    # risk:swallowed-exception reason:"priority 来自既有审核日志或外部导入，非法值按 must-view 分层处理以免单条脏数据使整份统计投影失败" owner:@kevin reviewed:2026-11-11
    except (TypeError, ValueError):
        p = 0
    if p <= 0:
        return "L1"
    if p == 1:
        return "L2"
    return "L3"


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
    # v2.5 DIR-04: creative-gate human sampling runs in its own queue, with
    # its own ratio and a recorded seed (see lib.creative_sampling).
    creative_ratio: float = 0.2
    creative_seed: Optional[int] = None

    def validate(self) -> "ReviewPolicy":
        if self.mode not in ("all", "sampled"):
            raise ReviewValidationError(
                f"invalid review mode '{self.mode}' (expected all|sampled)")
        if not 0.0 <= float(self.detail_ratio) <= 1.0:
            raise ReviewValidationError(
                f"detail_ratio {self.detail_ratio} outside 0..1")
        if not 0.0 <= float(self.face_sim_threshold) <= 1.0:
            raise ReviewValidationError(
                f"face_sim_threshold {self.face_sim_threshold} outside 0..1")
        if not 0.0 <= float(self.creative_ratio) <= 1.0:
            raise ReviewValidationError(
                f"creative_ratio {self.creative_ratio} outside 0..1")
        return self

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "ReviewPolicy":
        data = data or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known}
        if "always_detail" in kwargs:
            kwargs["always_detail"] = tuple(kwargs["always_detail"] or ())
        return cls(**kwargs)


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
    # v2.5 DIR-02: which gate this decision closes. None keeps pre-v2.5
    # callers working; the creative/technical split is opt-in per decision.
    gate_type: Optional[str] = None
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

    def _decision_records(self) -> dict[str, Optional[dict[str, Any]]]:
        """Latest effective decision *record* per review_id (REV-13/14).

        Unlike ``_effective`` (which projects the inner decision object) this
        keeps the candidate version the decision was made on, so stale detection
        and timing do not need a second writer or a schema change.
        """
        out: dict[str, Optional[dict]] = {}
        for rec in self._records():
            rid = rec.get("review_id")
            et = rec.get("event_type")
            if et == "decision":
                out[rid] = rec
            elif et == "undo":
                target = rec.get("supersedes") or rid
                out[target] = None
        return out

    # --- per-episode policy (REV-10) ---

    def get_policy(self) -> ReviewPolicy:
        """Load the per-episode review policy (defaults when never written)."""
        path = self.dir / POLICY_FILE
        if not path.exists():
            return ReviewPolicy()
        return ReviewPolicy.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def set_policy(self, policy: ReviewPolicy) -> ReviewPolicy:
        """Persist the sampling policy; never touches items or decisions.

        ``reviews.jsonl`` is not appended to and no candidate is regenerated —
        changing the ratio only changes ``pending()`` length (REV-10).
        """
        policy.validate()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / POLICY_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(policy), ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
        tmp.replace(path)
        return policy

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
        policy = (policy or ReviewPolicy()).validate()
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
               candidate_version: Optional[str] = None, *,
               enforce_breaker: bool = True,
               retry_policy: Optional[RetryPolicy] = None) -> dict[str, Any]:
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
        if decision.gate_type not in (None, "creative", "technical"):
            raise ReviewValidationError(
                f"invalid gate_type '{decision.gate_type}' (expected creative or technical)")
        if decision.gate_type == "creative" and decision.action in {"accept", "pick_variant"}:
            # DIR-02: the technical gate PASS is a prerequisite for the
            # creative gate. Approving creatively without it would let a
            # creative judgment paper over an unverified technical defect.
            chain = effective_decision_chain(self._records(), review_id)
            passed = any(
                (rec.get("decision") or {}).get("gate_type") == "technical"
                and (rec.get("decision") or {}).get("action") in {"accept", "pick_variant"}
                for rec in chain
            )
            if not passed:
                raise ReviewValidationError(
                    "creative gate requires a prior effective technical PASS "
                    "for this item (DIR-02)")

        ver = candidate_version or item.get("candidate_version")
        if ver and item.get("candidate_version") and ver != item.get("candidate_version"):
            raise StaleReviewError(
                f"candidate version '{ver}' != current '{item.get('candidate_version')}' (STALE_REVIEW)")

        if enforce_breaker and decision.action in BLOCKED_WHEN_TRIPPED:
            state = self.breaker_state(review_id, policy=retry_policy)
            if state["tripped"] and not (decision.override and decision.evidence):
                raise CircuitBreakerOpenError(
                    review_id, state["limit"], state["consecutive_rejects"])

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
            "supersedes": review_id,
            "metadata": {"reason": reason, "compensation": True},
            "created_at": _now(),
        })

    # --- circuit breaker (REV-06) ---

    def breaker_state(self, review_id: str,
                      policy: Optional[RetryPolicy] = None) -> dict[str, Any]:
        """Current per-shot breaker state projected from the append-only log."""
        item = self.get(review_id)["item"]
        chain = effective_decision_chain(self._records(), review_id)
        state: BreakerState = evaluate_breaker(
            chain, policy,
            current_candidate_version=item.get("candidate_version"))
        return state.as_dict()

    # --- stale decisions (REV-13) ---

    def stale(self) -> list[dict[str, Any]]:
        """Decisions made on a candidate version that is no longer current.

        The log is never mutated: a stale decision stays recorded and is
        compensated with ``undo`` (``om review list --stale`` surface).
        """
        out: list[dict[str, Any]] = []
        decisions = self._decision_records()
        for rid, eff in self._effective().items():
            rec = decisions.get(rid)
            if not rec:
                continue
            current = eff["item"].get("candidate_version")
            decided_on = rec.get("candidate_version")
            if current and decided_on and current != decided_on:
                out.append({
                    "review_id": rid,
                    "stage": eff["item"].get("stage"),
                    "slot": eff["item"].get("slot"),
                    "decision": rec.get("decision"),
                    "decided_on_candidate_version": decided_on,
                    "current_candidate_version": current,
                })
        return sorted(out, key=lambda x: x["review_id"])

    # --- timing (REV-14) ---

    def timings(self) -> list[dict[str, Any]]:
        """Per-item open/close timestamps and review duration.

        ``opened_at`` is the item's ``created_at``; ``decided_at`` comes from
        the effective decision. All data stays in the episode log (X-15).
        """
        decisions = self._decision_records()
        out: list[dict[str, Any]] = []
        for rid, eff in self._effective().items():
            item = eff["item"]
            rec = decisions.get(rid)
            decision = rec.get("decision") if rec else None
            opened_at = item.get("created_at")
            decided_at = decision.get("decided_at") if decision else None
            opened = _parse_ts(opened_at)
            decided = _parse_ts(decided_at)
            seconds = None
            if opened and decided:
                seconds = max(0.0, (decided - opened).total_seconds())
            out.append({
                "review_id": rid,
                "stage": item.get("stage"),
                "slot": item.get("slot"),
                "layer": _layer(item.get("priority")),
                "opened_at": opened_at,
                "decided_at": decided_at,
                "seconds": seconds,
            })
        return sorted(out, key=lambda t: (t["decided_at"] or "", t["review_id"]))

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
            "timing": self._timing_stats(),
        }

    def _timing_stats(self) -> dict[str, Any]:
        """Throughput metrics over the append-only timing fields (REV-14)."""
        timings = self.timings()
        durations = [t["seconds"] for t in timings if t["seconds"] is not None]
        opened = [ts for ts in (_parse_ts(t["opened_at"]) for t in timings) if ts]
        closed = [ts for ts in (_parse_ts(t["decided_at"]) for t in timings) if ts]

        elapsed_seconds = None
        if opened and closed:
            span = max(closed) - min(opened)
            elapsed_seconds = max(0.0, span.total_seconds())
        elapsed_minutes = (elapsed_seconds / 60.0) if elapsed_seconds is not None else None

        by_layer = {"L1": [], "L2": [], "L3": []}
        for t in timings:
            if t["seconds"] is not None:
                by_layer[t["layer"]].append(t["seconds"])

        def _minutes(values: list[float]) -> float:
            return round(sum(values) / 60.0, 3)

        return {
            "privacy_scope": "local episode log only; no upload (X-15)",
            "opened_at": min(opened).isoformat() if opened else None,
            "last_decided_at": max(closed).isoformat() if closed else None,
            "elapsed_minutes": round(elapsed_minutes, 3) if elapsed_minutes is not None else None,
            "review_minutes": _minutes(durations),
            "median_review_seconds": (round(statistics.median(durations), 3)
                                      if durations else None),
            "items_per_minute": (round(len(durations) / elapsed_minutes, 3)
                                 if elapsed_minutes else None),
            "l1_items": len(by_layer["L1"]),
            "l1_minutes": _minutes(by_layer["L1"]),
            "l2_items": len(by_layer["L2"]),
            "l2_minutes": _minutes(by_layer["L2"]),
            "l3_items": len(by_layer["L3"]),
            "l3_minutes": _minutes(by_layer["L3"]),
        }