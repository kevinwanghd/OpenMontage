"""M1 real generation driver for the short-drama pipeline (NFR-01/03/06, ORC-03).

The driver walks the nine stages declared in ``pipeline_defs/short-drama.yaml``
in manifest order and never names a provider itself: every tool-backed stage is
resolved through :func:`lib.capability_slots.resolve`, so switching a binding
profile changes nothing here and nothing in the pipeline file (MOD-01 / DEL-08).

Two modes, one stage plan:

- **mock** (default): every stage is planned and every slot is resolved, but no
  tool executes and no external API is touched. ``om demo --mock`` keeps its M0
  behaviour and does not go through this driver.
- **real**: requires an explicit live approval *and* a cash budget. Before any
  paid call the driver takes an atomic budget check (spent + reserved + this
  call's worst-case estimate including retries) against the
  :class:`lib.submission_ledger.SubmissionLedger`, then records a submission
  intent. A crash after submission but before a task id leaves the entry
  ``unknown``; :func:`lib.resume.resume_plan` then reports ``reconcile`` and the
  driver refuses to re-pay automatically.

Everything is injectable: tests pass a fake ``executor`` and never open a
socket. The default executor is the live tool registry and is only reachable
behind ``--approve-live --budget-usd``.
"""
from __future__ import annotations

# risk:untested reason:"driver content chain and gates are covered by tests/contracts/test_shortdrama_driver.py and test_shortdrama_content.py; thin static gate cannot link that mapping" owner:@kevinwanghd reviewed:2026-11-12

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from lib import capability_slots as slots
from lib.checkpoint import PROJECTS_DIR, read_checkpoint, write_checkpoint
from lib.decision_log import DecisionEntry, DecisionLog
from lib.pipeline_loader import (
    get_stage_order,
    get_stage_skill,
    load_pipeline_readonly,
)
from lib.resume import resume_plan
from lib.submission_ledger import (
    DuplicateSubmissionError,
    LedgerError,
    SubmissionLedger,
)
from shortdrama import content
from shortdrama.content import ContentError, WorkItem
from shortdrama.errors import ApiError, ErrorCode

PIPELINE_NAME = "short-drama"

#: File extensions per slot for the driver-assigned output path.
_OUTPUT_EXTENSIONS = {
    "image_character_key": ".png",
    "image_reference": ".png",
    "video_primary": ".mp4",
    "lipsync": ".mp4",
    "tts_voice": ".mp3",
    "music": ".mp3",
    "subtitle": ".srt",
    "compose": ".mp4",
    "upscale": ".mp4",
    "text_llm": ".json",
}

#: REV-06 caps retries at three, so the worst-case single call is four attempts.
DEFAULT_RETRIES = 3

#: Planning rate used to express the CNY catalog prices in the USD budget the
#: operator approves. It is recorded on every ledger entry (``rate``) so the
#: original price and the conversion stay auditable (NFR-01: keep currency and
#: rate provenance).
DEFAULT_CNY_PER_USD = 7.2

#: Slots whose provider submission has an identity worth tracking even at a
#: declared zero cash price; local tools need no task id.
_LOCAL_TIERS = frozenset({"local"})

#: Failures that mean "these parameters will never work": retrying the same
#: input only burns time and quota, so the exact input hash is remembered.
_CONTENT_ERROR_TOKENS = (
    "invalidparameter", "invalid parameter", "invalid_parameter",
    "unsupported", "not supported", "bad request", "requires a reference image",
    "must be", "cannot be", "out of range",
)
_QUOTA_ERROR_TOKENS = (
    "arrearage", "freetieronly", "free tier", "free quota", "quota",
    "insufficient", "balance",
)
_TRANSIENT_ERROR_TOKENS = (
    "timeout", "timed out", "connection", "temporar", "rate limit", "429",
    "500", "502", "503", "504",
)

#: Remembers content-rejected input hashes so a resume cannot re-pay for a
#: call that is guaranteed to fail again until the content changes.
FAILED_INPUTS_FILE = "failed_inputs.json"


def classify_provider_error(message: Any) -> str:
    """Classify a provider failure as content / quota / transient / unknown.

    Quota is checked first: "free quota" also contains "free", and rotating to
    another model is the only useful response there. Content is next because
    those errors must never be retried with the same input. Everything else is
    treated as possibly transient.
    """
    text = str(message or "").lower()
    if any(token in text for token in _QUOTA_ERROR_TOKENS):
        return "quota"
    if any(token in text for token in _CONTENT_ERROR_TOKENS):
        return "content"
    if any(token in text for token in _TRANSIENT_ERROR_TOKENS):
        return "transient"
    return "unknown"


Executor = Callable[[str, dict[str, Any]], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Driver:
    """Stage-ordered, budget-gated driver for the short-drama pipeline."""

    def __init__(
        self,
        project: str,
        episode: str,
        *,
        projects_dir: Path = PROJECTS_DIR,
        profile: str = "",
        executor: Optional[Executor] = None,
        retries: int = DEFAULT_RETRIES,
        cny_per_usd: float = DEFAULT_CNY_PER_USD,
        currency: str = "USD",
        clip_duration_s: float = 5.0,
        keyframes_required: bool = True,
        max_item_cost_cny: Optional[float] = None,
    ) -> None:
        self.project = project
        self.episode = episode
        self.projects_dir = Path(projects_dir)
        self.profile = profile
        self.retries = max(int(retries), 0)
        self.cny_per_usd = float(cny_per_usd)
        self.currency = currency
        self.clip_duration_s = float(clip_duration_s)
        # Declared fallback for a blocked image slot: animate from the shot
        # text instead of a reviewed keyframe. Quality differs; the run JSON
        # records it so the operator never mistakes it for the normal path.
        self.keyframes_required = bool(keyframes_required)
        # Hard per-item ceiling: a misconfigured duration or model must not be
        # able to turn one shot into an unexpectedly large charge.
        self.max_item_cost_cny = (
            float(max_item_cost_cny) if max_item_cost_cny is not None else None
        )
        # Code-level QA validates real media on disk. A test/rehearsal injects a
        # fake executor that produces no files, so QA is only meaningful when
        # the real registry executor runs.
        self.qa_enabled = executor is None
        self._executor = executor or self._registry_executor
        self.executed = 0
        self.manifest = load_pipeline_readonly(PIPELINE_NAME)
        self.stage_order = get_stage_order(self.manifest)
        self._budget_usd: Optional[float] = None
        self._artifacts_cache: Optional[dict[str, dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Stage / slot plan (never provider names)
    # ------------------------------------------------------------------

    def stages(self) -> list[str]:
        """The nine stages in manifest order."""
        return list(self.stage_order)

    def stage_slots(self, stage: str) -> list[str]:
        """Return the capability slots declared for ``stage`` in the manifest."""
        for declared in self.manifest["stages"]:
            if declared.get("name") != stage:
                continue
            out: list[str] = []
            for entry in declared.get("tools_available") or []:
                if isinstance(entry, str) and entry.startswith("slot:"):
                    out.append(entry.split(":", 1)[1])
            return out
        return []

    def _resolve(self, slot: str) -> str:
        definition = slots.slot_definition(slot)
        required = list(definition.get("required_operations") or [])
        operation = required[0] if required else None
        profile = self.profile or slots.active_profile()
        return slots.resolve(slot, profile, operation=operation)

    def _slot_required(self, slot: str) -> bool:
        """Manifest-declared requirement; unknown slots stay required (fail loud)."""
        try:
            definition = slots.slot_definition(slot)
        # risk:swallowed-exception reason:"槽位定义缺失时按必需处理（fail loud），异常不会被忽略" owner:@kevinwanghd reviewed:2026-11-14
        except Exception:
            return True
        return bool(definition.get("required", True))

    def _cost_row(self, slot: str) -> Optional[dict[str, Any]]:
        """The cost-comparison row for the binding this slot currently resolves to."""
        profile = self.profile or slots.active_profile()
        definition = slots.slot_definition(slot)
        required = list(definition.get("required_operations") or [])
        tool = self._resolve(slot)
        rows = slots.cost_comparison(
            slot,
            profile,
            operation=required[0] if required else None,
            duration_s=self.clip_duration_s,
        )
        return next((row for row in rows if row.get("tool") == tool), None)

    def plan(self) -> dict[str, Any]:
        """Resolve every stage/slot without executing anything (mock plan)."""
        stages: list[dict[str, Any]] = []
        unresolved = 0
        for index, stage in enumerate(self.stage_order, start=1):
            entry: dict[str, Any] = {
                "index": index,
                "stage": stage,
                "skill": get_stage_skill(self.manifest, stage),
                "executed": False,
                "slots": [],
            }
            for slot in self.stage_slots(stage):
                item: dict[str, Any] = {"slot": slot, "resolved": False, "tool": None}
                try:
                    item["tool"] = self._resolve(slot)
                    item["resolved"] = True
                except slots.CapabilitySlotsError as exc:
                    item["error"] = str(exc)
                    unresolved += 1
                entry["slots"].append(item)
            stages.append(entry)
        return {"stages": stages, "unresolved_slots": unresolved}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def cost_preview(self, stages: Optional[list[str]] = None) -> dict[str, Any]:
        """Per-shot cost estimate for the stages that are still to run.

        Items already produced (same input hash + artifact on disk) are priced
        at zero, so resuming a paused run does not look like a fresh spend.
        The preview never calls a provider.
        """
        stage_list = list(stages or self.stage_order)
        ledger = SubmissionLedger(self.project, self.episode, self.projects_dir)
        rows: list[dict[str, Any]] = []
        total_cny = 0.0
        worst_cny = 0.0

        for stage in stage_list:
            for slot in self.stage_slots(stage):
                row: Optional[dict[str, Any]] = None
                try:
                    tool = self._resolve(slot)
                    row = self._cost_row(slot)
                except slots.CapabilitySlotsError as exc:
                    rows.append({
                        "stage": stage, "slot": slot, "tool": None,
                        "unresolved": str(exc), "estimated_cny": 0.0,
                    })
                    continue
                if row is None:
                    rows.append({
                        "stage": stage, "slot": slot, "tool": tool,
                        "unresolved": "no price estimate", "estimated_cny": 0.0,
                    })
                    continue

                try:
                    items = self._slot_items(stage, slot)
                except Exception:
                    items = []
                per_item = float(row.get("estimated_cny") or 0.0)
                if items:
                    item_rows = []
                    for work_item in items:
                        reused = self._already_produced(
                            ledger, stage, slot, tool, work_item,
                            model=row.get("model"),
                        ) is not None
                        item_rows.append({
                            "item_id": work_item.item_id,
                            "estimated_cny": 0.0 if reused else per_item,
                            "reused": reused,
                        })
                else:
                    # Artifacts not produced yet: assume one call at the
                    # declared price so the estimate stays conservative.
                    item_rows = [{
                        "item_id": "(pending artifacts)",
                        "estimated_cny": per_item,
                        "reused": False,
                    }]

                subtotal = sum(entry["estimated_cny"] for entry in item_rows)
                worst = subtotal * (1 + self.retries)
                total_cny += subtotal
                worst_cny += worst
                rows.append({
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "model": row.get("model"),
                    "item_count": len(item_rows),
                    "estimated_cny": round(subtotal, 4),
                    "worst_case_cny": round(worst, 4),
                    "items": item_rows,
                })

        return {
            "currency": "CNY",
            "estimated_cny": round(total_cny, 4),
            "worst_case_cny": round(worst_cny, 4),
            "estimated_usd": round(total_cny / self.cny_per_usd, 6),
            "worst_case_usd": round(worst_cny / self.cny_per_usd, 6),
            "budget_usd": self._budget_usd,
            "rows": rows,
        }

    def run(
        self,
        *,
        real: bool = False,
        approve_live: bool = False,
        budget_usd: Optional[float] = None,
        pause_for_review: bool = False,
        stop_after: Optional[str] = None,
        start_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run all nine stages; mock by default, live only with explicit consent.

        ``pause_for_review`` honours the manifest's ``manual_all`` checkpoint
        policy: a stage that made provider calls writes an ``awaiting_human``
        checkpoint and the run stops there, so no later stage can spend money
        on unreviewed assets. ``stop_after`` is the automation escape hatch.
        """
        if real:
            self._require_live_approval(approve_live, budget_usd)
            self._budget_usd = float(budget_usd)
        else:
            self._budget_usd = None

        resume = resume_plan(
            self.project,
            self.episode,
            projects_dir=self.projects_dir,
            pipeline_type=PIPELINE_NAME,
        )
        result: dict[str, Any] = {
            "pipeline": PIPELINE_NAME,
            "project_id": self.project,
            "episode_id": self.episode,
            "profile": self.profile or slots.active_profile(),
            "mock": not real,
            "real": bool(real),
            "resume": resume,
            "stages": [],
            "external_api_calls": 0,
        }

        if not real:
            planned = self.plan()
            result["stages"] = planned["stages"]
            result["unresolved_slots"] = planned["unresolved_slots"]
            return result

        # NFR-03: unresolved billing means "stop and reconcile", never re-pay.
        if resume["mode"] != "run":
            raise ApiError(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"resume plan is '{resume['mode']}': {resume['reason']}",
                "reconcile the ledger entries with the provider (or ask a human), "
                "then re-run",
                recovery="ask-human",
                side_effects="unknown",
                context={"resume": resume},
            )

        start = 0
        if start_at:
            if start_at not in self.stage_order:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    f"start_at stage {start_at!r} is not in this pipeline",
                    f"choose one of {self.stage_order}",
                    recovery="fix-then-retry",
                    exit_code=2,
                )
            start = self.stage_order.index(start_at)
        elif resume["stage"] in self.stage_order:
            start = self.stage_order.index(resume["stage"])
        elif resume["stage"] is None:
            result["status"] = "already-complete"
            return result

        preview_stages = self.stage_order[start:]
        if stop_after in preview_stages:
            preview_stages = preview_stages[: preview_stages.index(stop_after) + 1]
        preview = self.cost_preview(preview_stages)
        result["cost_estimate"] = preview
        if (
            self._budget_usd is not None
            and preview["worst_case_usd"] > float(self._budget_usd) + 1e-9
        ):
            # Fail before the first call instead of discovering the shortfall
            # after several paid shots.
            raise self._pause(
                ErrorCode.BUDGET_EXCEEDED,
                f"estimated worst case {preview['worst_case_usd']:.4f} "
                f"{self.currency} exceeds budget {float(self._budget_usd):.4f}; "
                "no call was made",
                "raise --budget-usd or reduce the run (--stop-after / fewer "
                "shots), then re-run; nothing was spent",
                recovery="ask-human",
                side_effects="none",
                context={
                    "budget": float(self._budget_usd),
                    "estimate": preview,
                },
            )

        self._record_preexecution_decisions()
        ledger = SubmissionLedger(self.project, self.episode, self.projects_dir)

        for stage in self.stage_order[start:]:
            pending = read_checkpoint(self.projects_dir, self.project, stage)
            if (
                pending
                and pending.get("status") == "awaiting_human"
                and self.stage_slots(stage)
            ):
                # The previous run already spent money on this stage; review it
                # before any re-execution, never re-pay for the same work.
                result["status"] = "waiting-for-review"
                result["waiting_for"] = stage
                break
            stage_entry: dict[str, Any] = {
                "index": self.stage_order.index(stage) + 1,
                "stage": stage,
                "skill": get_stage_skill(self.manifest, stage),
                "slots": [],
            }
            self._preflight_stage(stage)
            for slot in self.stage_slots(stage):
                stage_entry["slots"].append(self._run_slot(ledger, stage, slot))
            stage_entry["executed"] = any(
                item.get("executed") for item in stage_entry["slots"]
            )
            if (stage_entry["executed"] and self.qa_enabled
                    and stage in ("shot_gen", "assembly")):
                from shortdrama import qa

                verdict = qa.review(
                    self.projects_dir / self.project, stage, episode=self.episode
                )
                stage_entry["qa"] = verdict
                if not verdict["passed"]:
                    raise self._pause(
                        ErrorCode.BATCH_PRECONDITION_FAILED,
                        f"automatic QA rejected stage '{stage}' "
                        f"({len(verdict['blocking'])} blocking issue(s))",
                        "fix the listed shots and re-run; the reviewer agent "
                        "handles visual judgement, this gate is deterministic",
                        recovery="do-X-first",
                        side_effects="partial",
                        context={"stage": stage, "qa": verdict},
                    )
            if stage_entry["executed"] and pause_for_review:
                stage_entry["checkpoint"] = str(
                    self._write_review_checkpoint(stage, stage_entry, ledger)
                )
            result["stages"].append(stage_entry)
            if stage_entry["executed"] and pause_for_review:
                result["status"] = "waiting-for-review"
                result["waiting_for"] = stage
                break
            if stop_after == stage:
                result["status"] = "stopped"
                result["stopped_after"] = stage
                break

        result["external_api_calls"] = self.executed
        result["budget"] = self._budget_summary(ledger)
        return result

    # ------------------------------------------------------------------
    # Consent gate
    # ------------------------------------------------------------------

    def _require_live_approval(
        self, approve_live: bool, budget_usd: Optional[float]
    ) -> None:
        missing: list[str] = []
        if not approve_live:
            missing.append("--approve-live")
        if budget_usd is None or float(budget_usd) <= 0:
            missing.append("--budget-usd N (positive)")
        if not missing:
            return
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "real generation requires explicit live approval and a positive budget",
            "re-run: om demo --real --approve-live --budget-usd <USD>",
            recovery="do-X-first",
            side_effects="none",
            context={"missing": missing, "requested_real": True},
            exit_code=2,
        )

    # ------------------------------------------------------------------
    # Review gates + content chain (M1)
    # ------------------------------------------------------------------

    def _stage_decl(self, stage: str) -> dict[str, Any]:
        for declared in self.manifest["stages"]:
            if declared.get("name") == stage:
                return declared
        return {}

    def _artifacts(self) -> dict[str, dict[str, Any]]:
        if self._artifacts_cache is None:
            names = (
                "brief",
                "beat_map",
                "script",
                "character_design",
                "drama_character",
                "scene_plan",
                "asset_manifest",
                "edit_decisions",
                "render_report",
            )
            try:
                self._artifacts_cache = content.load_artifacts(
                    self.projects_dir, self.project, self.episode, names
                )
            except ContentError as exc:
                raise self._pause(
                    ErrorCode.BATCH_PRECONDITION_FAILED,
                    f"cannot read project artifacts: {exc}",
                    "repair the artifact JSON, then re-run",
                    recovery="do-X-first",
                    context={"project": self.project, "episode": self.episode},
                ) from exc
        return self._artifacts_cache

    def _preflight_stage(self, stage: str) -> None:
        """Fail before spending: artifacts exist and creative review is done.

        Stages without slots are agent-authored (idea/beat_map/script/
        scene_plan). They cannot be "executed" by the driver, so they must
        already carry a completed, human-approved checkpoint -- this is the
        gate that makes script and scene-plan review mandatory before any
        paid generation call.
        """
        declared = self._stage_decl(stage)
        required = [str(name) for name in declared.get("required_artifacts_in") or []]
        if not self.keyframes_required and stage == "shot_gen":
            required = [name for name in required if name != "asset_manifest"]
        missing = content.missing_artifacts(
            self.projects_dir, self.project, self.episode, required
        )
        if missing:
            artifacts_dir = content.episode_artifacts_dir(
                self.projects_dir, self.project, self.episode
            )
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"stage '{stage}' requires artifact(s) {missing} before any paid call",
                f"produce them with the {get_stage_skill(self.manifest, stage)} "
                f"director skill under {artifacts_dir}/<name>.json, then re-run",
                recovery="do-X-first",
                context={
                    "stage": stage,
                    "missing_artifacts": missing,
                    "artifacts_dir": str(artifacts_dir),
                },
            )
        errors = self._continuity_errors(stage)
        if errors:
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"continuity gate failed at stage '{stage}': " + "; ".join(errors),
                "fix the scene_plan / keyframes / character registry so identity, "
                "props and scene stay consistent, then re-run",
                recovery="do-X-first",
                side_effects="none",
                context={"stage": stage, "continuity_errors": errors},
            )
        if stage == "shot_gen" and self.keyframes_required and self.qa_enabled:
            # The reviewer runs on stills, before the expensive video call: a
            # rejected keyframe must never be inherited by a generated clip.
            review_errors = self._visual_review_errors()
            if review_errors:
                raise self._pause(
                    ErrorCode.BATCH_PRECONDITION_FAILED,
                    "keyframe visual gate failed before shot_gen: "
                    + "; ".join(review_errors),
                    "run the shortdrama-reviewer agent on the keyframes, fix or "
                    "regenerate the rejected candidates, persist a passing "
                    "verdict, then re-run",
                    recovery="do-X-first",
                    side_effects="none",
                    context={"stage": stage, "visual_review_errors": review_errors},
                )
        if self.stage_slots(stage):
            return

        checkpoint = read_checkpoint(self.projects_dir, self.project, stage)
        approved = bool(
            checkpoint
            and checkpoint.get("status") == "completed"
            and checkpoint.get("human_approved")
        )
        if approved:
            return
        state = (
            "missing"
            if checkpoint is None
            else f"{checkpoint.get('status')} "
                 f"(human_approved={bool(checkpoint.get('human_approved'))})"
        )
        raise self._pause(
            ErrorCode.BATCH_PRECONDITION_FAILED,
            f"stage '{stage}' is agent-authored and has no approved checkpoint "
            f"({state}); paid generation must wait for human review",
            f"produce the artifact with the {get_stage_skill(self.manifest, stage)} "
            f"director skill, present it to the user, then write checkpoint_{stage}.json "
            "with status='completed', human_approved=True and re-run",
            recovery="ask-human",
            side_effects="none",
            context={
                "stage": stage,
                "checkpoint": state,
                "review_required": True,
            },
        )

    def _continuity_errors(self, stage: str) -> list[str]:
        """Machine-checkable identity/scene/keyframe invariants, pre-spend."""
        from shortdrama import continuity

        artifacts = self._artifacts()
        scene_plan = artifacts.get("scene_plan")
        if not scene_plan:
            return []
        known = continuity.character_ids(artifacts)
        known |= set(
            content._character_cards(
                self.projects_dir, self.project, self.episode, artifacts
            )
        )
        errors: list[str] = []
        if stage in ("keyframe", "shot_gen", "assembly"):
            errors += continuity.validate_scene_plan(scene_plan, known)
        if stage in ("shot_gen", "assembly") and self.keyframes_required:
            errors += continuity.validate_keyframes(
                scene_plan, artifacts.get("asset_manifest")
            )
        return errors

    def _visual_review_errors(self) -> list[str]:
        """Keyframes must carry a passing visual verdict before video spend."""
        from shortdrama import preflight

        path = content.artifact_path(
            self.projects_dir, self.project, self.episode, "qa_visual"
        )
        return preflight.visual_review_errors(path.parent)

    def _video_preflight(self, items: list[WorkItem], row: dict[str, Any]) -> list[str]:
        """Every item's parameters must be legal for the resolved model."""
        from shortdrama import preflight

        catalog_model = row.get("model")
        if not catalog_model:
            # A local/custom binding declares no catalog contract to check.
            return []
        return preflight.validate_items(
            items,
            catalog_model=catalog_model,
            model=row.get("tool_model"),
            provider=row.get("provider"),
            fallback_models=row.get("fallback_models") or (),
            resolution=self._draft_resolution(),
            base_dir=self.projects_dir / self.project / self.episode,
        )

    def _slot_items(self, stage: str, slot: str) -> list[WorkItem]:
        try:
            return content.build_work_items(
                stage,
                slot,
                self._artifacts(),
                projects_dir=self.projects_dir,
                project=self.project,
                episode=self.episode,
                keyframes_required=self.keyframes_required,
            )
        except ContentError as exc:
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"no usable content for stage '{stage}' slot '{slot}': {exc}",
                "produce or repair the upstream artifact with its director skill, "
                "then re-run",
                recovery="do-X-first",
                context={"stage": stage, "slot": slot},
            ) from exc

    # ------------------------------------------------------------------
    # Budget gate + ledger (NFR-01 / ORC-03)
    # ------------------------------------------------------------------

    def _run_slot(
        self, ledger: SubmissionLedger, stage: str, slot: str
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "slot": slot,
            "resolved": False,
            "tool": None,
            "executed": False,
        }
        try:
            tool = self._resolve(slot)
        except slots.CapabilitySlotsError as exc:
            if not self._slot_required(slot):
                # Optional slots (e.g. a music bed) must not block a paid run.
                item["skipped"] = True
                item["error"] = str(exc)
                return item
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"no usable binding for slot '{slot}' at stage '{stage}': {exc}",
                "run `om doctor` and fix the profile binding, then re-run",
                recovery="do-X-first",
                context={"stage": stage, "slot": slot},
            ) from exc

        item["resolved"] = True
        item["tool"] = tool
        row = self._cost_row(slot)
        if row is None:
            raise self._pause(
                ErrorCode.BUDGET_EXCEEDED,
                f"no price estimate for {tool} on slot '{slot}'",
                "declare a binding price or bind a priced tool, then re-run",
                recovery="ask-human",
                context={"stage": stage, "slot": slot, "tool": tool},
            )
        if not row.get("cost_known"):
            raise self._pause(
                ErrorCode.BUDGET_EXCEEDED,
                f"price unknown for slot '{slot}' ({tool}); refusing to spend blind",
                "record cost_cny_per_run / cost_cny_per_s on the binding, or pick a "
                "priced binding, then re-run",
                recovery="ask-human",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "cost_basis": row.get("cost_basis"),
                },
            )

        work_items = self._slot_items(stage, slot)
        if not work_items and slot == "tts_voice":
            # No on-screen dialogue / voiceover in this episode: nothing to say.
            item["skipped"] = True
            return item
        if not work_items:
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"no work items for stage '{stage}' slot '{slot}'",
                "produce the upstream artifact content, then re-run",
                recovery="do-X-first",
                side_effects="none",
                context={"stage": stage, "slot": slot, "tool": tool},
            )
        if slot == "video_primary":
            contract_errors = self._video_preflight(work_items, row)
            if contract_errors:
                raise self._pause(
                    ErrorCode.BATCH_PRECONDITION_FAILED,
                    f"model contract preflight failed at stage '{stage}': "
                    + "; ".join(contract_errors),
                    "fix the work-item parameters (operation, duration, ratio, "
                    "resolution, reference files) or bind a model that supports "
                    "them, then re-run; nothing was spent",
                    recovery="do-X-first",
                    side_effects="none",
                    context={
                        "stage": stage,
                        "slot": slot,
                        "tool": tool,
                        "contract_errors": contract_errors,
                    },
                )

        results = [
            self._run_item(ledger, stage, slot, tool, row, work_item)
            for work_item in work_items
        ]
        executed = [result for result in results if result.get("executed")]
        item.update(
            {
                "executed": bool(executed),
                "items": results,
                "item_count": len(results),
                "estimated_cny": round(
                    sum(float(result.get("estimated_cny") or 0.0) for result in results),
                    4,
                ),
                "worst_case_usd": round(
                    sum(float(result.get("worst_case_usd") or 0.0) for result in results),
                    6,
                ),
                "ledger_entry_id": next(
                    (result.get("ledger_entry_id") for result in results), None
                ),
                "ledger_entry_ids": [
                    result.get("ledger_entry_id") for result in results
                ],
                "task_id": next(
                    (result.get("task_id") for result in results if result.get("task_id")),
                    None,
                ),
                "billing_state": (
                    "billed"
                    if any(result.get("billing_state") == "billed" for result in results)
                    else "free"
                ),
                "artifacts": [
                    artifact
                    for result in results
                    for artifact in result.get("artifacts") or []
                ],
            }
        )
        return item

    def _run_item(
        self,
        ledger: SubmissionLedger,
        stage: str,
        slot: str,
        tool: str,
        row: dict[str, Any],
        work_item: WorkItem,
    ) -> dict[str, Any]:
        """One budget-gated provider call for one content work item."""
        item: dict[str, Any] = {
            "item_id": work_item.item_id,
            "resolved": True,
            "tool": tool,
            "executed": False,
        }
        worst_cny = float(row.get("estimated_cny") or 0.0) * (1 + self.retries)
        worst_usd = round(worst_cny / self.cny_per_usd, 6)
        item["estimated_cny"] = round(float(row.get("estimated_cny") or 0.0), 4)
        item["worst_case_usd"] = worst_usd

        if (
            self.max_item_cost_cny is not None
            and float(row.get("estimated_cny") or 0.0) > self.max_item_cost_cny + 1e-9
        ):
            raise self._pause(
                ErrorCode.BUDGET_EXCEEDED,
                f"item '{work_item.item_id}' would cost "
                f"¥{float(row.get('estimated_cny') or 0.0):.4f}, above the "
                f"per-item ceiling ¥{self.max_item_cost_cny:.4f}; nothing was spent",
                "raise max_item_cost_cny or shorten/cheapen the item, then re-run",
                recovery="ask-human",
                side_effects="none",
                context={
                    "stage": stage,
                    "slot": slot,
                    "item_id": work_item.item_id,
                    "estimated_cny": float(row.get("estimated_cny") or 0.0),
                    "max_item_cost_cny": self.max_item_cost_cny,
                },
            )

        # A content-rejected input is remembered so a resume cannot pay again
        # for a call that is guaranteed to fail until the parameters change.
        previous_failure = self._known_failure(stage, slot, tool, work_item)
        if previous_failure is not None:
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"item '{work_item.item_id}' already failed with a content error "
                f"({previous_failure.get('error')}); the same input will fail again",
                "change the prompt/parameters or the bound model; the input hash "
                "covers both, so a real change is a new work unit",
                recovery="do-X-first",
                side_effects="none",
                context={
                    "stage": stage,
                    "slot": slot,
                    "item_id": work_item.item_id,
                    "prior_error": previous_failure.get("error"),
                },
            )

        # A stage paused mid-way (budget or crash) must resume, not re-pay.
        # The input hash already covers the content, so a resolved entry with
        # the same hash plus an existing output file means this exact item was
        # produced; a changed prompt hashes differently and is generated again.
        existing = self._already_produced(
            ledger, stage, slot, tool, work_item, model=row.get("model")
        )
        if existing is not None:
            item.update(
                {
                    "reused": True,
                    "artifacts": existing,
                    "billing_state": "reused",
                }
            )
            return item

        entry = self._reserve(
            ledger,
            stage,
            slot,
            tool,
            row,
            worst_cny=worst_cny,
            worst_usd=worst_usd,
            work_item=work_item,
        )
        item["ledger_entry_id"] = entry["entry_id"]

        try:
            raw = self._executor(
                tool, self._slot_inputs(stage, slot, tool, row, work_item)
            )
        # risk:swallowed-exception reason:"provider errors are converted to an agent-ready ApiError with UNKNOWN billing state and reconciled before any retry; never ignored" owner:@kevinwanghd reviewed:2026-11-12
        except Exception as exc:  # provider call failed; billing identity unknown
            ledger.mark_submitted(entry["entry_id"], None)
            kind = classify_provider_error(exc)
            if kind == "content":
                self._remember_failed_input(
                    stage, slot, tool, work_item, str(exc)
                )
            raise self._pause(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} failed at stage '{stage}' slot '{slot}' "
                f"item '{work_item.item_id}': {exc}",
                (
                    "the provider rejected these parameters; fix the prompt/"
                    "parameters or the bound model before retrying (the input is "
                    "remembered so it will not be re-paid unchanged)"
                    if kind == "content"
                    else "reconcile the ledger entry with the provider "
                         "(resume mode=reconcile) before retrying; do not re-pay blindly"
                ),
                recovery="do-X-first" if kind == "content" else "ask-human",
                side_effects="none" if kind == "content" else "unknown",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "item_id": work_item.item_id,
                    "entry_id": entry["entry_id"],
                    "resume_mode": "reconcile",
                    "error_kind": kind,
                },
            ) from exc

        payload = self._normalize_result(raw)
        tier = str(row.get("tier") or "")
        # Only asynchronous submissions can leave billing unknown when a task
        # id is missing; synchronous tools (image APIs) return their result and
        # receipt inline, so requiring a task id would falsely block them.
        needs_task_id = tier not in _LOCAL_TIERS and self._requires_task_id(tool)

        if not payload["success"]:
            ledger.mark_submitted(entry["entry_id"], None)
            failure_text = payload.get("error") or "no error detail"
            kind = classify_provider_error(failure_text)
            if kind == "content":
                self._remember_failed_input(
                    stage, slot, tool, work_item, str(failure_text)
                )
            raise self._pause(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} reported failure at stage '{stage}' slot '{slot}' "
                f"item '{work_item.item_id}': {failure_text}",
                {
                    "content": (
                        "the provider rejected these parameters; fix the prompt/"
                        "parameters or the bound model before retrying (the input "
                        "is remembered so it will not be re-paid unchanged)"
                    ),
                    "quota": (
                        "every declared model reports exhausted quota; bind a "
                        "different model or wait for the quota window"
                    ),
                }.get(kind, "reconcile the ledger entry with the provider before retrying"),
                recovery="do-X-first" if kind in ("content", "quota") else "ask-human",
                side_effects="none" if kind in ("content", "quota") else "unknown",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "item_id": work_item.item_id,
                    "entry_id": entry["entry_id"],
                    "resume_mode": "reconcile",
                    "error_kind": kind,
                },
            )

        if needs_task_id and not payload.get("task_id"):
            # Submitted, but the task id never made it back: billing is UNKNOWN.
            ledger.mark_submitted(entry["entry_id"], None)
            raise self._pause(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} completed at stage '{stage}' slot '{slot}' "
                f"item '{work_item.item_id}' without a task id; billing state is UNKNOWN",
                "reconcile the ledger entry with the provider (resume mode=reconcile) "
                "before any re-submission",
                recovery="ask-human",
                side_effects="unknown",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "item_id": work_item.item_id,
                    "entry_id": entry["entry_id"],
                    "resume_mode": "reconcile",
                },
            )

        ledger.mark_submitted(entry["entry_id"], payload.get("task_id"))
        billed = float(payload.get("cost_cny") or 0.0) > 0
        ledger.mark_result(entry["entry_id"], "billed" if billed else "free")
        self.executed += 1

        item.update(
            {
                "executed": True,
                "task_id": payload.get("task_id"),
                "billing_state": "billed" if billed else "free",
                "artifacts": payload.get("artifacts") or [],
            }
        )
        return item

    def _requires_task_id(self, tool: str) -> bool:
        """Async tools prove billing by task id; sync tools do not have one."""
        try:
            from tools.tool_registry import registry

            registry.ensure_discovered()
            registered = registry.get(tool)
        # risk:swallowed-exception reason:"注册表不可用时按异步工具处理并要求 task id（保守失败），不静默放行" owner:@kevinwanghd reviewed:2026-11-14
        except Exception:
            return True
        if registered is None:
            return True
        mode = getattr(registered, "execution_mode", None)
        if mode is None:
            return True
        return str(getattr(mode, "value", mode)).lower() == "async"

    def _already_produced(
        self,
        ledger: SubmissionLedger,
        stage: str,
        slot: str,
        tool: str,
        work_item: WorkItem,
        model: Optional[str] = None,
    ) -> Optional[list[str]]:
        """Artifacts from a resolved prior attempt of this exact content item.

        The model is part of the identity: different models can share the same
        content hash (the work item is provider-neutral), but the file at the
        deterministic path may have been overwritten by a different model.
        Reusing it would ship a clip that no longer matches the reviewed input.
        """
        input_hash = self._input_hash(stage, slot, tool, work_item)
        prior = [
            record
            for record in ledger.find_by_input_hash(input_hash)
            if (
                record.get("billing_state") == "billed"
                # A "free" result only proves success when the provider
                # returned a task id; reconciled failures are also "free".
                or (record.get("billing_state") == "free" and record.get("task_id"))
            )
            and (model is None or record.get("model") == model)
        ]
        if not prior:
            return None
        output_path = self._slot_inputs(stage, slot, tool, {}, work_item).get(
            "output_path"
        )
        if not output_path:
            return None
        path = Path(output_path)
        if not path.is_file() or path.stat().st_size == 0:
            return None
        return [str(path)]

    def _reserve(
        self,
        ledger: SubmissionLedger,
        stage: str,
        slot: str,
        tool: str,
        row: dict[str, Any],
        *,
        worst_cny: float,
        worst_usd: float,
        work_item: Optional[WorkItem] = None,
    ) -> dict[str, Any]:
        """Atomic spent+reserved+this-call check, then intent, under one lock."""
        provider = str(row.get("provider") or tool)
        input_hash = self._input_hash(stage, slot, tool, work_item)
        budget = float(self._budget_usd or 0.0)

        # ORC-10: the ledger's class-wide (re-entrant) lock makes the read of
        # spent/reserved and the intent append one critical section, so two
        # concurrent drivers cannot both pass the gate and over-book.
        with SubmissionLedger._lock:
            spent, reserved = _ledger_totals(ledger, self.currency)
            projected = spent + reserved + worst_usd
            if projected > budget + 1e-9:
                raise self._pause(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"budget pause at stage '{stage}' slot '{slot}': "
                    f"spent {spent:.4f} + reserved {reserved:.4f} + worst case "
                    f"{worst_usd:.4f} {self.currency} exceeds budget {budget:.4f}",
                    "raise --budget-usd or reduce the run, then re-run; nothing was spent",
                    recovery="ask-human",
                    context={
                        "stage": stage,
                        "slot": slot,
                        "tool": tool,
                        "currency": self.currency,
                        "budget": budget,
                        "spent": round(spent, 6),
                        "reserved": round(reserved, 6),
                        "worst_case": worst_usd,
                        "worst_case_cny": round(worst_cny, 4),
                        "retries": self.retries,
                    },
                )
            try:
                return ledger.record_intent(
                    stage,
                    provider,
                    input_hash,
                    reserve_amount=worst_usd,
                    currency=self.currency,
                    rate=self.cny_per_usd,
                    episode_id=self.episode,
                    model=row.get("model"),
                )
            except DuplicateSubmissionError as exc:
                raise self._pause(
                    ErrorCode.BATCH_PRECONDITION_FAILED,
                    f"unresolved submission already exists for stage '{stage}' "
                    f"slot '{slot}': {exc}",
                    "reconcile the existing entry with the provider before re-submitting",
                    recovery="ask-human",
                    side_effects="unknown",
                    context={"stage": stage, "slot": slot, "tool": tool},
                ) from exc
            except LedgerError as exc:
                raise self._pause(
                    ErrorCode.INTERNAL_ERROR,
                    f"ledger rejected the submission intent: {exc}",
                    "inspect the ledger file and retry",
                    recovery="ask-human",
                    context={"stage": stage, "slot": slot, "tool": tool},
                ) from exc

    def _budget_summary(self, ledger: SubmissionLedger) -> dict[str, Any]:
        spent, reserved = _ledger_totals(ledger, self.currency)
        budget = float(self._budget_usd or 0.0)
        return {
            "currency": self.currency,
            "cny_per_usd": self.cny_per_usd,
            "budget": budget,
            "spent": round(spent, 6),
            "reserved": round(reserved, 6),
            "remaining": round(budget - spent - reserved, 6),
            "retries_included": self.retries,
        }

    def _write_review_checkpoint(
        self, stage: str, stage_entry: dict[str, Any], ledger: SubmissionLedger
    ) -> Path:
        """Persist generated files as an ``awaiting_human`` checkpoint.

        A generated asset is not an approved asset. The manifest gate forbids
        writing ``completed`` without human approval, so the driver records the
        work here and stops; approving rewrites the same stage as completed.
        """
        generated: dict[str, list[str]] = {}
        for slot_entry in stage_entry.get("slots") or []:
            files = [
                artifact
                for item in slot_entry.get("items") or []
                for artifact in item.get("artifacts") or []
            ]
            if files:
                generated[str(slot_entry["slot"])] = files
        return write_checkpoint(
            self.projects_dir,
            self.project,
            stage,
            "awaiting_human",
            {"generated": generated},
            pipeline_type=PIPELINE_NAME,
            checkpoint_policy="manual_all",
            human_approval_required=True,
            human_approved=False,
            cost_snapshot=self._budget_summary(ledger),
            metadata={"driver": "shortdrama.driver", "episode": self.episode},
        )

    # ------------------------------------------------------------------
    # Decision log (NFR-06 / ORC-04): record before executing
    # ------------------------------------------------------------------

    def _record_preexecution_decisions(self) -> None:
        log = DecisionLog(self.project, self.projects_dir)
        seen = {
            (record.get("category"), record.get("subject"), record.get("decision"))
            for record in log.entries()
        }

        for stage in self.stage_order:
            for slot in self.stage_slots(stage):
                try:
                    tool = self._resolve(slot)
                    rows = slots.cost_comparison(
                        slot,
                        self.profile or slots.active_profile(),
                        duration_s=self.clip_duration_s,
                    )
                except slots.CapabilitySlotsError as exc:
                    if not self._slot_required(slot):
                        continue
                    # Fail before spending: an unresolvable required slot cannot
                    # run later, so stop before the first paid call.
                    raise self._pause(
                        ErrorCode.BATCH_PRECONDITION_FAILED,
                        f"cannot plan slot '{slot}' at stage '{stage}': {exc}",
                        "run `om doctor` and fix the profile binding, then re-run",
                        recovery="do-X-first",
                        context={"stage": stage, "slot": slot},
                    ) from exc
                row = next((r for r in rows if r.get("tool") == tool), None)
                if row is None or int(row.get("chain_index") or 0) == 0:
                    continue
                subject = f"{slot}:{tool}"
                key = ("provider_selection", subject, tool)
                if key in seen:
                    continue
                alternatives = [
                    {
                        "option_id": str(r.get("tool") or ""),
                        "label": f"{r.get('provider') or 'unknown'}/"
                                 f"{r.get('model') or r.get('tool')}",
                        "reason": str(r.get("cost_basis") or ""),
                    }
                    for r in rows
                ]
                log.append(DecisionEntry(
                    category="provider_selection",
                    subject=subject,
                    decision=tool,
                    reason=(
                        "declared priority chain fell back from the primary binding; "
                        "recorded before spending"
                    ),
                    alternatives=alternatives,
                    episode_id=self.episode,
                    metadata={
                        "stage": stage,
                        "profile": self.profile or slots.active_profile(),
                        "approved_live": True,
                        "budget_usd": self._budget_usd,
                    },
                ))
                seen.add(key)

        self._record_degradations(log, seen)

    def _record_degradations(
        self,
        log: DecisionLog,
        seen: set[tuple],
    ) -> None:
        profile = self.profile or slots.active_profile()

        # No native dialogue lipsync -> predeclared TTS + lipsync + mux path.
        try:
            capability = slots.supports("video_primary", profile)
        except slots.CapabilitySlotsError as exc:
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"cannot read video_primary capabilities from profile '{profile}': {exc}",
                "run `om doctor` and fix the profile, then re-run",
                recovery="do-X-first",
                context={"slot": "video_primary", "profile": profile},
            ) from exc
        if capability.get("lipsync_from_dialogue") is not True:
            plans = slots.plan_degradation(
                ["video_primary"],
                profile,
                {"capabilities": {"lipsync_from_dialogue": True}},
            )
            for plan in plans:
                if plan.get("status") != "DEGRADED":
                    continue
                subject = f"video_primary:lipsync:{plan.get('plan_id') or 'chain'}"
                key = ("downgrade_approval", subject, "separate_lipsync_path")
                if key in seen:
                    continue
                log.append(DecisionEntry(
                    category="downgrade_approval",
                    subject=subject,
                    decision="separate_lipsync_path",
                    reason=str(plan.get("reason") or "declared degradation path"),
                    alternatives=[
                        {
                            "option_id": str(step.get("binding") or step.get("stage")),
                            "label": str(step.get("stage")),
                            "reason": "declared degradation step",
                        }
                        for step in plan.get("path") or []
                    ],
                    episode_id=self.episode,
                    metadata={
                        "lost": plan.get("lost") or [],
                        "added": plan.get("added"),
                        "approved_live": True,
                        "budget_usd": self._budget_usd,
                    },
                ))
                seen.add(key)

        # Draft -> delivery upscale is a declared change, not a silent one.
        try:
            profile_data = slots.load_profile(profile)
        except slots.CapabilitySlotsError as exc:
            raise self._pause(
                ErrorCode.BATCH_PRECONDITION_FAILED,
                f"cannot load profile '{profile}': {exc}",
                "run `om profile list` and select a valid profile, then re-run",
                recovery="do-X-first",
                context={"profile": profile},
            ) from exc
        delivery = profile_data.get("delivery") or {}
        if not isinstance(delivery, dict) or not delivery.get("requires_upscale"):
            return
        subject = f"delivery:upscale:{delivery.get('degradation_id') or 'undeclared'}"
        key = ("downgrade_approval", subject, "upscale")
        if key in seen:
            return
        log.append(DecisionEntry(
            category="downgrade_approval",
            subject=subject,
            decision="upscale",
            reason=(
                f"draft {delivery.get('draft_resolution')} -> "
                f"{delivery.get('target_resolution')} requires upscale (DEL-01/F34); "
                "recorded before rendering delivery"
            ),
            alternatives=[
                {
                    "option_id": "native_delivery",
                    "label": f"render natively at {delivery.get('target_resolution')}",
                    "reason": "not available on the current binding",
                },
                {
                    "option_id": "delivery_upscale",
                    "label": "upscale the approved draft (DEGRADED)",
                    "reason": "predeclared degradation path",
                },
            ],
            episode_id=self.episode,
            metadata={
                "lost": ["native_1080p"],
                "approved_live": True,
                "budget_usd": self._budget_usd,
            },
        ))
        seen.add(key)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pause(
        self,
        code: ErrorCode,
        what: str,
        fix: str,
        *,
        recovery: str,
        context: dict[str, Any],
        side_effects: Optional[str] = None,
    ) -> ApiError:
        """Agent-ready pause; side_effects reflects calls already made."""
        effects = side_effects or ("partial" if self.executed else "none")
        return ApiError(
            code,
            what,
            fix,
            recovery=recovery,
            side_effects=effects,
            context=context,
        )

    def _failed_inputs_path(self) -> Path:
        return (
            self.projects_dir / self.project / self.episode / FAILED_INPUTS_FILE
        )

    def _load_failed_inputs(self) -> dict[str, dict[str, Any]]:
        path = self._failed_inputs_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _remember_failed_input(
        self,
        stage: str,
        slot: str,
        tool: str,
        work_item: WorkItem,
        error: str,
    ) -> None:
        """Pin a content-rejected input so the same call is never re-paid."""
        data = self._load_failed_inputs()
        data[self._input_hash(stage, slot, tool, work_item)] = {
            "stage": stage,
            "slot": slot,
            "tool": tool,
            "item_id": work_item.item_id,
            "error": error[:500],
            "at": _now(),
        }
        path = self._failed_inputs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _known_failure(
        self, stage: str, slot: str, tool: str, work_item: WorkItem
    ) -> Optional[dict[str, Any]]:
        data = self._load_failed_inputs()
        return data.get(self._input_hash(stage, slot, tool, work_item))

    def _input_hash(
        self, stage: str, slot: str, tool: str, work_item: Optional[WorkItem] = None
    ) -> str:
        """Content is part of the identity: a changed prompt is a new work unit.

        ORC-03 only lets the ledger reuse an entry for the same verified input;
        an orchestration-only hash would silently reuse an old paid asset after
        the shot text or keyframe changed.
        """
        payload = json.dumps(
            {
                "version": "1.0",
                "pipeline": PIPELINE_NAME,
                "project": self.project,
                "episode": self.episode,
                "stage": stage,
                "slot": slot,
                "tool": tool,
                "item_id": work_item.item_id if work_item else None,
                "content": work_item.inputs if work_item else {},
            },
            sort_keys=True,
        )
        return _sha256(payload)

    def _draft_resolution(self) -> str:
        """Draft resolution declared by the active profile (DEL-01/F34)."""
        try:
            profile_data = slots.load_profile(self.profile or slots.active_profile())
        except slots.CapabilitySlotsError:
            return "480p"
        delivery = profile_data.get("delivery") or {}
        if isinstance(delivery, dict) and delivery.get("draft_resolution"):
            return str(delivery["draft_resolution"])
        return "480p"

    def _slot_inputs(
        self,
        stage: str,
        slot: str,
        tool: str,
        row: dict[str, Any],
        work_item: Optional[WorkItem] = None,
    ) -> dict[str, Any]:
        inputs: dict[str, Any] = {
            "project_id": self.project,
            "episode_id": self.episode,
            "stage": stage,
            "slot": slot,
            "tool": tool,
            "profile": self.profile or slots.active_profile(),
            # A catalog id (provider/model) is not a provider model name; only
            # pass plain tool-level models declared by the binding itself.
            **(
                {"model": row["tool_model"]}
                if row.get("tool_model")
                else {}
            ),
            **(
                {"fallback_models": list(row["fallback_models"])}
                if row.get("fallback_models")
                else {}
            ),
            "output_dir": str(self.projects_dir / self.project / self.episode / "assets"),
            "budget_usd": self._budget_usd,
            "approve_live": True,
        }
        if work_item is not None:
            inputs.update(work_item.inputs)
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", work_item.item_id) or "item"
            extension = _OUTPUT_EXTENSIONS.get(slot, ".bin")
            inputs["output_path"] = str(
                self.projects_dir
                / self.project
                / self.episode
                / "assets"
                / f"{stage}_{slot}_{safe_id}{extension}"
            )
        if slot == "video_primary" and not inputs.get("resolution"):
            inputs["resolution"] = self._draft_resolution()
        return inputs

    def _normalize_result(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            cost_cny = raw.get("cost_cny")
            if cost_cny is None and raw.get("cost_usd") is not None:
                cost_cny = float(raw["cost_usd"]) * self.cny_per_usd
            return {
                "success": bool(raw.get("success", True)),
                "task_id": raw.get("task_id"),
                "cost_cny": float(cost_cny or 0.0),
                "artifacts": list(raw.get("artifacts") or []),
                "error": raw.get("error") or raw.get("data", {}).get("error"),
            }
        # ToolResult-compatible object from the live registry.
        cost_usd = float(getattr(raw, "cost_usd", 0.0) or 0.0)
        return {
            "success": bool(getattr(raw, "success", False)),
            "task_id": getattr(raw, "data", {}).get("task_id"),
            "cost_cny": cost_usd * self.cny_per_usd,
            "artifacts": list(getattr(raw, "artifacts", []) or []),
            "error": getattr(raw, "error", None),
        }

    def _registry_executor(self, tool_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """Default live executor: resolve the tool from the registry and run it."""
        from tools.tool_registry import registry

        registry.ensure_discovered()
        tool = registry.get(tool_name)
        if tool is None:
            raise LookupError(f"tool {tool_name!r} is not registered")
        return tool.execute(inputs)


def _ledger_totals(ledger: SubmissionLedger, currency: str) -> tuple[float, float]:
    """(billed spend, held reservations) in ``currency``.

    ``_latest()`` is the ledger's own append-only fold; the class-level lock is
    held by the caller so this read and the following append are one critical
    section (ORC-10). A billed entry keeps its reservation amount as the
    conservative recorded spend (actual cost is never lower than the reserve).
    """
    spent = 0.0
    for record in ledger._latest().values():
        if record.get("currency") != currency:
            continue
        reservation = record.get("reservation") or {}
        if record.get("billing_state") == "billed":
            spent += float(reservation.get("amount") or 0.0)
    return spent, ledger.reserved_total(currency)