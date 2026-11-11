"""Single per-episode append-only event log + projections (ORC-01/11, F6).

One writer, one source of truth for shot-level state. Shot state, reject counts
and the review queue are PROJECTIONS of this log, never independently derived
from reviews/task/checkpoint (avoids split ownership). Crash recovery = replay.

Stage-level checkpoints remain in lib/checkpoint.py for Backlot compatibility;
this log owns everything shot-level. M0: local files, zero external API.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib.checkpoint import PROJECTS_DIR

# event_type -> resulting shot state
STATE_EVENTS = {
    "shot_planned": "planned",
    "shot_keyframing": "keyframing",
    "shot_keyframe_approved": "keyframe_approved",
    "shot_generating": "generating",
    "shot_in_review": "in_review",
    "shot_accepted": "accepted",
    "shot_rejected": "rejected",
    "shot_assembled": "assembled",
    "shot_skipped": "skipped",
}

# legal state transitions (ORC-01); rejected may retry, skip != pass
TRANSITIONS = {
    "planned": {"keyframing", "skipped"},
    "keyframing": {"keyframe_approved", "rejected"},
    "keyframe_approved": {"generating", "skipped"},
    "generating": {"in_review", "rejected"},
    "in_review": {"accepted", "rejected", "skipped"},
    "rejected": {"keyframing", "generating", "skipped"},
    "accepted": {"assembled"},
    "assembled": set(),
    "skipped": {"planned"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EpisodeLogError(Exception):
    pass


class IllegalTransitionError(EpisodeLogError):
    pass


class EpisodeLog:
    _lock = threading.Lock()

    def __init__(self, project: str, episode: str, projects_dir: Path = PROJECTS_DIR):
        self.project = project
        self.episode = episode
        self.dir = Path(projects_dir) / project / episode
        self.path = self.dir / "events.jsonl"

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def append_event(self, event_type: str, shot_id: str, *,
                     candidate_version: Optional[str] = None,
                     payload: Optional[dict] = None) -> dict[str, Any]:
        if event_type not in STATE_EVENTS:
            raise EpisodeLogError(f"unknown event_type '{event_type}'")
        target = STATE_EVENTS[event_type]
        states = self.shot_states()
        current = states.get(shot_id, {}).get("state")
        if current is None:
            if target != "planned":
                raise IllegalTransitionError(f"shot '{shot_id}' must start at planned, got {target}")
        elif target not in TRANSITIONS.get(current, set()):
            raise IllegalTransitionError(f"illegal transition {current} -> {target} for '{shot_id}'")

        seq = len(self.events()) + 1
        rec = {"seq": seq, "event_type": event_type, "shot_id": shot_id,
               "state": target, "candidate_version": candidate_version,
               "payload": payload or {}, "ts": _now()}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def shot_states(self) -> dict[str, dict[str, Any]]:
        """Replay the log into per-shot state projections (shot_state schema)."""
        out: dict[str, dict] = {}
        for ev in self.events():
            sid = ev["shot_id"]
            st = out.setdefault(sid, {"version": "1.0", "shot_id": sid,
                                      "episode_id": self.episode, "state": "planned",
                                      "reject_count": 0, "event_seq": 0,
                                      "candidate_version": None})
            st["state"] = ev["state"]
            st["event_seq"] = ev["seq"]
            if ev.get("candidate_version"):
                st["candidate_version"] = ev["candidate_version"]
            if ev["event_type"] == "shot_rejected":
                st["reject_count"] += 1
            elif ev["event_type"] in ("shot_accepted", "shot_keyframe_approved"):
                st["reject_count"] = 0
        return out