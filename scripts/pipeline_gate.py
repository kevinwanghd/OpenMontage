"""Hard approval gates and task reporting for the bespoke pipeline drivers.

The stock Backlot flow expects the agent to pause at a gated stage, show the
artifact to a human, and only continue once the human said so. A bespoke
driver (one that drives the stages itself instead of going through the task
issuing path) has to honour the same rule, or it is silently forging
approvals.

The protocol this module implements:

1. the driver writes the stage's artifacts and a checkpoint with status
   ``awaiting_human``;
2. it STOPS (it does not poll and it does not continue) — control goes back
   to the reviewer;
3. the reviewer approves in chat; the agent then runs
   ``python scripts/pipeline_gate.py --project <id> --approve <stage>``,
   which flips ``human_approved`` on that checkpoint;
4. the driver is re-run and walks past the approved gate.

The driver never asserts ``human_approved`` on its own: the only way the
flag becomes true is through :func:`approve_stage`.

Gate flags come from the pipeline manifest
(``human_approval_default``), so they are not hardcoded here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.checkpoint import PROJECTS_DIR, write_checkpoint  # noqa: E402
from lib.events import emit_task  # noqa: E402


def gated_stages(pipeline_type: str = "cinematic") -> set[str]:
    """Stage names whose manifest entry requires human approval."""
    try:
        from lib.pipeline_loader import load_pipeline, get_stage_human_approval_default

        manifest = load_pipeline(pipeline_type)
        return {
            str(stage.get("name"))
            for stage in manifest.get("stages", [])
            if isinstance(stage, dict)
            and stage.get("name")
            and get_stage_human_approval_default(manifest, str(stage.get("name")))
        }
    except Exception:
        return {"proposal", "script", "scene_plan", "assets", "publish"}


def read_checkpoint(pid: str, stage: str) -> Optional[dict[str, Any]]:
    path = PROJECTS_DIR / pid / f"checkpoint_{stage}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def approve_stage(pid: str, stage: str, note: str = "") -> bool:
    """Flip ``human_approved`` on a stage checkpoint that awaits review.

    Returns False when the stage is not currently awaiting a human — an
    approval must never be pre-applied to a stage nobody was asked about.
    """
    cp = read_checkpoint(pid, stage)
    if not cp:
        print(f"[gate] no checkpoint for '{stage}' in project '{pid}'")
        return False
    if cp.get("status") != "awaiting_human":
        print(f"[gate] '{stage}' is '{cp.get('status')}', not awaiting_human — nothing to approve")
        return False
    cp["human_approved"] = True
    review = dict(cp.get("review") or {})
    if note:
        review["approval_note"] = note
    cp["review"] = review
    path = PROJECTS_DIR / pid / f"checkpoint_{stage}.json"
    path.write_text(json.dumps(cp, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[gate] '{stage}' approved in project '{pid}'")
    return True


def task(
    pid: str,
    task_id: str,
    title: str,
    stage: str,
    status: str,
    detail: Optional[str] = None,
) -> None:
    """Report one unit of work to the board's task list."""
    emit_task(PROJECTS_DIR / pid, task_id, title, stage, status, detail)


def stage_gate(
    pid: str,
    stage: str,
    artifacts: dict[str, Any],
    review: Optional[dict[str, Any]] = None,
    pipeline_type: str = "cinematic",
    **extra: Any,
) -> str:
    """Advance one stage through its gate.

    Returns ``"skipped"`` when the stage is already completed, ``"done"``
    when it is approved and now completed, and ``"blocked"`` when it is
    waiting for a human — the caller must stop the run on ``"blocked"``.
    """
    cp = read_checkpoint(pid, stage)
    if cp and cp.get("status") == "completed":
        return "skipped"

    gated = stage in gated_stages(pipeline_type)
    approved = bool(cp.get("human_approved")) if cp else False

    if gated and not approved:
        write_checkpoint(
            PROJECTS_DIR, pid, stage, "awaiting_human", artifacts,
            pipeline_type=pipeline_type, review=review, **extra,
        )
        task(
            pid, f"gate:{stage}", f"{stage} · 等待审批", stage, "blocked",
            "在看板或对话里审批后，用 --approve-stage 放行",
        )
        print(f"[gate] '{stage}' is waiting for your approval — the run stops here.")
        return "blocked"

    if gated:
        write_checkpoint(
            PROJECTS_DIR, pid, stage, "completed", artifacts,
            pipeline_type=pipeline_type, human_approved=True, review=review, **extra,
        )
    else:
        write_checkpoint(
            PROJECTS_DIR, pid, stage, "completed", artifacts,
            pipeline_type=pipeline_type, review=review, **extra,
        )
    task(pid, f"gate:{stage}", f"{stage} · 已完成", stage, "done")
    return "done"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--approve", default="", help="stage name to approve")
    parser.add_argument("--note", default="", help="approval note recorded in the checkpoint")
    parser.add_argument("--status", default="", help="print the status of a stage and exit")
    args = parser.parse_args()

    if args.approve:
        return 0 if approve_stage(args.project, args.approve, args.note) else 1

    if args.status:
        cp = read_checkpoint(args.project, args.status)
        print(json.dumps(cp, indent=2, ensure_ascii=False) if cp else "(no checkpoint)")
        return 0

    parser.error("one of --approve or --status is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
