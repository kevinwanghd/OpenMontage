"""Resume-state contract (NFR-03, F4).

``get_next_stage`` alone cannot express "stop and reconcile billing" — it only
returns the first uncompleted stage name. This layer adds the missing vocabulary:
before any paid call the driver MUST consult :func:`resume_plan` and honour its
``mode``:

- ``run``       : no unresolved billing; safe to continue at ``stage``;
- ``reconcile`` : unknown billing state exists; query the provider / human before
                  any re-submission (never auto re-pay);
- ``blocked``   : escalated reservations or other human decision required.

M0: local files, zero external API.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from lib.checkpoint import PROJECTS_DIR, get_next_stage
from lib.submission_ledger import SubmissionLedger


def resume_plan(project: str, episode: str, *, projects_dir: Path = PROJECTS_DIR,
                pipeline_type: Optional[str] = None) -> dict[str, Any]:
    ledger = SubmissionLedger(project, episode, projects_dir)

    unknown = ledger.unknown_entries()
    if unknown:
        return {
            "stage": None, "mode": "reconcile",
            "reason": f"{len(unknown)} unknown billing entry(ies); reconcile before any paid re-submission",
            "entry_ids": [r["entry_id"] for r in unknown],
        }

    escalated = [r for r in ledger._latest().values()
                 if (r.get("reservation") or {}).get("state") == "escalated"]
    if escalated:
        return {
            "stage": None, "mode": "blocked",
            "reason": "escalated reservation(s) require a human decision",
            "entry_ids": [r["entry_id"] for r in escalated],
        }

    stage = get_next_stage(Path(projects_dir), project, pipeline_type)
    if stage is None:
        return {"stage": None, "mode": "run", "reason": "all stages completed"}
    return {"stage": stage, "mode": "run", "reason": "no unresolved billing; safe to continue"}