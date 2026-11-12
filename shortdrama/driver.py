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

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from lib import capability_slots as slots
from lib.checkpoint import PROJECTS_DIR
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
from shortdrama.errors import ApiError, ErrorCode

PIPELINE_NAME = "short-drama"

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
    ) -> None:
        self.project = project
        self.episode = episode
        self.projects_dir = Path(projects_dir)
        self.profile = profile
        self.retries = max(int(retries), 0)
        self.cny_per_usd = float(cny_per_usd)
        self.currency = currency
        self.clip_duration_s = float(clip_duration_s)
        self._executor = executor or self._registry_executor
        self.executed = 0
        self.manifest = load_pipeline_readonly(PIPELINE_NAME)
        self.stage_order = get_stage_order(self.manifest)
        self._budget_usd: Optional[float] = None

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

    def run(
        self,
        *,
        real: bool = False,
        approve_live: bool = False,
        budget_usd: Optional[float] = None,
    ) -> dict[str, Any]:
        """Run all nine stages; mock by default, live only with explicit consent."""
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
        if resume["stage"] in self.stage_order:
            start = self.stage_order.index(resume["stage"])
        elif resume["stage"] is None:
            result["status"] = "already-complete"
            return result

        self._record_preexecution_decisions()
        ledger = SubmissionLedger(self.project, self.episode, self.projects_dir)

        for stage in self.stage_order[start:]:
            stage_entry: dict[str, Any] = {
                "index": self.stage_order.index(stage) + 1,
                "stage": stage,
                "skill": get_stage_skill(self.manifest, stage),
                "slots": [],
            }
            for slot in self.stage_slots(stage):
                stage_entry["slots"].append(self._run_slot(ledger, stage, slot))
            stage_entry["executed"] = any(
                item.get("executed") for item in stage_entry["slots"]
            )
            result["stages"].append(stage_entry)

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

        worst_cny = float(row.get("estimated_cny") or 0.0) * (1 + self.retries)
        worst_usd = round(worst_cny / self.cny_per_usd, 6)
        item["estimated_cny"] = round(float(row.get("estimated_cny") or 0.0), 4)
        item["worst_case_usd"] = worst_usd

        entry = self._reserve(
            ledger, stage, slot, tool, row, worst_cny=worst_cny, worst_usd=worst_usd
        )
        item["ledger_entry_id"] = entry["entry_id"]

        try:
            raw = self._executor(tool, self._slot_inputs(stage, slot, tool, row))
        except Exception as exc:  # provider call failed; billing identity unknown
            ledger.mark_submitted(entry["entry_id"], None)
            raise self._pause(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} failed at stage '{stage}' slot '{slot}': {exc}",
                "reconcile the ledger entry with the provider (resume mode=reconcile) "
                "before retrying; do not re-pay blindly",
                recovery="ask-human",
                side_effects="unknown",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "entry_id": entry["entry_id"],
                    "resume_mode": "reconcile",
                },
            ) from exc

        payload = self._normalize_result(raw)
        tier = str(row.get("tier") or "")
        needs_task_id = tier not in _LOCAL_TIERS

        if not payload["success"]:
            ledger.mark_submitted(entry["entry_id"], None)
            raise self._pause(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} reported failure at stage '{stage}' slot '{slot}': "
                f"{payload.get('error') or 'no error detail'}",
                "reconcile the ledger entry with the provider before retrying",
                recovery="ask-human",
                side_effects="unknown",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
                    "entry_id": entry["entry_id"],
                    "resume_mode": "reconcile",
                },
            )

        if needs_task_id and not payload.get("task_id"):
            # Submitted, but the task id never made it back: billing is UNKNOWN.
            ledger.mark_submitted(entry["entry_id"], None)
            raise self._pause(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} completed at stage '{stage}' slot '{slot}' without a task id; "
                "billing state is UNKNOWN",
                "reconcile the ledger entry with the provider (resume mode=reconcile) "
                "before any re-submission",
                recovery="ask-human",
                side_effects="unknown",
                context={
                    "stage": stage,
                    "slot": slot,
                    "tool": tool,
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
    ) -> dict[str, Any]:
        """Atomic spent+reserved+this-call check, then intent, under one lock."""
        provider = str(row.get("provider") or tool)
        input_hash = self._input_hash(stage, slot, tool)
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
                    # Fail before spending: an unresolvable slot cannot run later.
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

    def _input_hash(self, stage: str, slot: str, tool: str) -> str:
        payload = json.dumps(
            {
                "version": "1.0",
                "pipeline": PIPELINE_NAME,
                "project": self.project,
                "episode": self.episode,
                "stage": stage,
                "slot": slot,
                "tool": tool,
            },
            sort_keys=True,
        )
        return _sha256(payload)

    def _slot_inputs(
        self,
        stage: str,
        slot: str,
        tool: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "project_id": self.project,
            "episode_id": self.episode,
            "stage": stage,
            "slot": slot,
            "tool": tool,
            "profile": self.profile or slots.active_profile(),
            "model": row.get("model"),
            "output_dir": str(self.projects_dir / self.project / self.episode / "assets"),
            "budget_usd": self._budget_usd,
            "approve_live": True,
        }

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