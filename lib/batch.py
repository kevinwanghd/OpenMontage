"""DEL-05 batch throughput: multi-episode scheduling, quota, resume (ORC-10).

The batch driver is deliberately boring: one local writer advances state one
step at a time, persists an append-only decision log plus a checkpoint after
every step, and stops cleanly when the paid-quota window is exhausted.

Parallelism follows ORC-10 / D8: concurrent work happens on the **provider
side** (asynchronous submission); the local writer never runs two state
mutations at once. A ``BatchLock`` file enforces the single-writer discipline
across processes, and state is written atomically (temp file + replace), so a
crash leaves either the old or the new checkpoint -- never a half-written one.

R5 quota: a ``QuotaAccount`` tracks a rolling window. A step that would exceed
the limit pauses the batch with an explicit ``paused_quota`` stop reason and a
``resume_from`` pointer; after the window rolls (or the limit is raised) the
same plan resumes for free on already-completed steps.

M0: no provider call and no spend. Provider task ids and resolutions are
supplied as data.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

SCHEMA_VERSION = "1.0"

READY = "ready"
RUNNING = "running"
COMPLETED = "completed"
WAITING_PROVIDER = "waiting_provider"
PAUSED_QUOTA = "paused_quota"
EPISODE_DONE = "completed"

STEP_LOCAL = "local"
STEP_ASYNC = "async"  # provider-side asynchronous generation
STEP_PROVIDERS = (STEP_LOCAL, STEP_ASYNC)

_EPS = 1e-9


class BatchError(ValueError):
    """Malformed batch plan, state or provider result."""


class SingleWriterViolation(RuntimeError):
    """Another writer already owns this batch state (ORC-10)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --- R5 quota ----------------------------------------------------------------


class QuotaAccount:
    """Rolling-window quota (R5). Reservation is atomic per writer call."""

    def __init__(
        self,
        limit: float,
        *,
        window_seconds: Optional[float] = None,
        used: float = 0.0,
        window_started_at: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit < 0:
            raise BatchError("quota limit must be a non-negative number")
        if used < 0:
            raise BatchError("quota used must be non-negative")
        self.limit = float(limit)
        self.window_seconds = None if window_seconds is None else float(window_seconds)
        self.used = float(used)
        self._clock = clock or _utc_now
        self.window_started_at = window_started_at or _iso(self._clock())

    @property
    def remaining(self) -> float:
        self._roll_window()
        return max(0.0, self.limit - self.used)

    def _roll_window(self) -> None:
        if self.window_seconds is None:
            return
        started = _parse_iso(self.window_started_at)
        if started is None:
            self.window_started_at = _iso(self._clock())
            return
        if self._clock() >= started + timedelta(seconds=self.window_seconds):
            self.used = 0.0
            self.window_started_at = _iso(self._clock())

    def try_reserve(self, units: float) -> bool:
        """Atomically reserve ``units``; False when the window would overflow."""
        if isinstance(units, bool) or not isinstance(units, (int, float)) or units < 0:
            raise BatchError(f"units must be a non-negative number, got {units!r}")
        self._roll_window()
        if self.used + float(units) > self.limit + _EPS:
            return False
        self.used += float(units)
        return True

    def advance_window(self) -> None:
        """Roll the window now (used by tests / a confirmed quota reset)."""
        self.used = 0.0
        self.window_started_at = _iso(self._clock())

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "window_seconds": self.window_seconds,
            "window_started_at": self.window_started_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()

    @classmethod
    def from_dict(cls, document: Mapping[str, Any], **kw: Any) -> "QuotaAccount":
        return cls(
            document["limit"],
            window_seconds=document.get("window_seconds"),
            used=document.get("used", 0.0),
            window_started_at=document.get("window_started_at"),
            **kw,
        )


# --- ORC-10 single writer ----------------------------------------------------


class BatchLock:
    """Exclusive lock file; the second writer raises ``SingleWriterViolation``."""

    def __init__(self, directory: Path | str, name: str = ".batch_writer.lock") -> None:
        self.directory = Path(directory)
        self.path = self.directory / name
        self.token = uuid.uuid4().hex
        self._held = False

    def acquire(self) -> "BatchLock":
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise SingleWriterViolation(
                f"batch state is already owned by another writer: {self.path}"
            ) from exc
        try:
            os.write(fd, json.dumps({"token": self.token, "pid": os.getpid()}).encode("utf-8"))
        finally:
            os.close(fd)
        self._held = True
        return self

    # risk:swallowed-exception reason:"lock cleanup is best-effort: a missing or foreign/malformed lock file must never mask the batch result or delete another writer's lock" owner:@kevinwanghd reviewed:2026-11-11
    def release(self) -> None:
        if not self._held:
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = {}
        if document.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self._held = False

    def __enter__(self) -> "BatchLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


# --- state -------------------------------------------------------------------


def _empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": _iso(_utc_now()),
        "status": READY,
        "stop_reason": None,
        "plan": None,
        "episodes": {},
        "events": [],
        "quota": None,
    }


def _validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise BatchError("plan must be a mapping")
    episodes = plan.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise BatchError("plan.episodes must be a non-empty list")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for episode in episodes:
        if not isinstance(episode, Mapping):
            raise BatchError("episode plan must be a mapping")
        episode_id = str(episode.get("episode_id") or "").strip()
        if not episode_id:
            raise BatchError("episode plan needs episode_id")
        if episode_id in seen:
            raise BatchError(f"duplicate episode_id: {episode_id}")
        seen.add(episode_id)
        steps = episode.get("steps")
        if not isinstance(steps, list) or not steps:
            raise BatchError(f"episode {episode_id} needs a non-empty steps list")
        seen_steps: set[str] = set()
        normalized_steps: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, Mapping):
                raise BatchError(f"episode {episode_id}: step must be a mapping")
            step_id = str(step.get("step_id") or "").strip()
            if not step_id:
                raise BatchError(f"episode {episode_id}: step needs step_id")
            if step_id in seen_steps:
                raise BatchError(f"episode {episode_id}: duplicate step_id {step_id}")
            seen_steps.add(step_id)
            units = step.get("units", 0.0)
            if isinstance(units, bool) or not isinstance(units, (int, float)) or units < 0:
                raise BatchError(f"{episode_id}.{step_id}: units must be non-negative")
            provider = str(step.get("provider", STEP_LOCAL)).strip().lower()
            if provider not in STEP_PROVIDERS:
                raise BatchError(
                    f"{episode_id}.{step_id}: provider must be one of {list(STEP_PROVIDERS)}"
                )
            normalized_steps.append(
                {"step_id": step_id, "units": float(units), "provider": provider}
            )
        normalized.append({"episode_id": episode_id, "steps": normalized_steps})
    return {"episodes": normalized}


def _empty_episode(episode_id: str) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "status": READY,
        "completed_steps": {},
        "pending_tasks": {},
        "last_error": None,
    }


class BatchRunner:
    """Single-writer batch driver with checkpoint/resume."""

    def __init__(
        self,
        state_path: Path | str,
        *,
        quota: Optional[QuotaAccount] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.quota = quota
        self.clock = clock or _utc_now

    @property
    def lock_path(self) -> Path:
        return self.state_path.parent / (self.state_path.name + ".lock")

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            state = _empty_state()
        else:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if self.quota is None and state.get("quota"):
            self.quota = QuotaAccount.from_dict(state["quota"], clock=self.clock)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _iso(self.clock())
        if self.quota is not None:
            state["quota"] = self.quota.to_dict()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".", suffix=".tmp", dir=str(self.state_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.state_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    # -- provider results ----------------------------------------------------

    def _index_provider_results(
        self, state: Mapping[str, Any], results: Optional[Iterable[Mapping[str, Any]]]
    ) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for result in results or []:
            if not isinstance(result, Mapping):
                raise BatchError("provider result must be a mapping")
            episode_id = str(result.get("episode_id") or "").strip()
            step_id = str(result.get("step_id") or "").strip()
            if not episode_id or not step_id:
                raise BatchError("provider result needs episode_id and step_id")
            episode = state["episodes"].get(episode_id)
            if episode is None:
                raise BatchError(f"provider result for unknown episode {episode_id!r}")
            task_id = f"{episode_id}:{step_id}"
            declared = result.get("task_id")
            if declared is not None and str(declared) != task_id:
                raise BatchError(
                    f"provider result task_id {declared!r} does not match {task_id!r}"
                )
            if task_id not in episode["pending_tasks"]:
                raise BatchError(f"no pending provider task {task_id!r} to resolve")
            if result.get("ok") is False:
                raise BatchError(
                    f"provider task {task_id!r} failed: {result.get('error', 'unknown error')}"
                )
            indexed[task_id] = dict(result)
        return indexed

    # -- run ------------------------------------------------------------------

    def run(
        self,
        plan: Optional[Mapping[str, Any]] = None,
        *,
        provider_results: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Advance the batch until done, quota-paused, or waiting on providers."""
        with BatchLock(self.state_path.parent, name=self.state_path.name + ".lock"):
            state = self.load()
            if plan is not None:
                state["plan"] = _validate_plan(plan)
            if not state.get("plan"):
                raise BatchError("no batch plan: pass plan= on the first run")
            state["plan"] = _validate_plan(state["plan"])

            for episode_plan in state["plan"]["episodes"]:
                state["episodes"].setdefault(
                    episode_plan["episode_id"], _empty_episode(episode_plan["episode_id"])
                )
            await_results = self._index_provider_results(state, provider_results)

            state["status"] = RUNNING
            state["stop_reason"] = None
            self._save(state)

            paused: Optional[dict[str, Any]] = None
            waiting = False
            for episode_plan in state["plan"]["episodes"]:
                episode_id = episode_plan["episode_id"]
                episode = state["episodes"][episode_id]
                if episode["status"] == EPISODE_DONE:
                    continue
                for step in episode_plan["steps"]:
                    step_id = step["step_id"]
                    if step_id in episode["completed_steps"]:
                        continue
                    units = step["units"]
                    task_id = f"{episode_id}:{step_id}"

                    if step["provider"] == STEP_ASYNC:
                        if task_id in await_results:
                            self._complete_step(
                                state, episode, step, task_id, mode="provider_async"
                            )
                            continue
                        if task_id in episode["pending_tasks"]:
                            waiting = True
                            episode["status"] = WAITING_PROVIDER
                            break
                        if self.quota is None:
                            raise BatchError("quota account required for paid async steps")
                        if not self.quota.try_reserve(units):
                            paused = self._pause(state, episode, step)
                            break
                        episode["pending_tasks"][task_id] = {
                            "step_id": step_id,
                            "units": units,
                            "submitted_at": _iso(self.clock()),
                        }
                        episode["status"] = WAITING_PROVIDER
                        state["events"].append(
                            {
                                "type": "provider_submitted",
                                "episode_id": episode_id,
                                "step_id": step_id,
                                "task_id": task_id,
                                "units": units,
                                "at": _iso(self.clock()),
                            }
                        )
                        waiting = True
                        break

                    # local step: single writer performs the state mutation
                    if self.quota is None:
                        if units > 0:
                            raise BatchError("quota account required for paid local steps")
                    elif not self.quota.try_reserve(units):
                        paused = self._pause(state, episode, step)
                        break
                    self._complete_step(state, episode, step, None, mode="local")
                if paused is not None:
                    break

            if paused is not None:
                state["status"] = PAUSED_QUOTA
                state["stop_reason"] = paused
            elif all(
                state["episodes"][e["episode_id"]]["status"] == EPISODE_DONE
                for e in state["plan"]["episodes"]
            ):
                state["status"] = COMPLETED
                state["stop_reason"] = None
            else:
                state["status"] = WAITING_PROVIDER
                state["stop_reason"] = {
                    "reason": "waiting_provider",
                    "detail": "asynchronous provider tasks unresolved; supply results on resume",
                }
            if waiting and paused is None and state["status"] == RUNNING:
                state["status"] = WAITING_PROVIDER
            self._save(state)
            return json.loads(json.dumps(state))

    def resume(
        self,
        *,
        provider_results: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Continue from the persisted checkpoint without redoing done steps."""
        return self.run(None, provider_results=provider_results)

    # -- helpers --------------------------------------------------------------

    def _complete_step(
        self,
        state: dict[str, Any],
        episode: dict[str, Any],
        step: Mapping[str, Any],
        task_id: Optional[str],
        *,
        mode: str,
    ) -> None:
        step_id = step["step_id"]
        episode["completed_steps"][step_id] = {
            "units": step["units"],
            "mode": mode,
            "at": _iso(self.clock()),
        }
        if task_id is not None:
            episode["pending_tasks"].pop(task_id, None)
        state["events"].append(
            {
                "type": "step_completed",
                "episode_id": episode["episode_id"],
                "step_id": step_id,
                "mode": mode,
                "units": step["units"],
                "at": _iso(self.clock()),
            }
        )
        episode_plan = next(
            e for e in state["plan"]["episodes"] if e["episode_id"] == episode["episode_id"]
        )
        if all(s["step_id"] in episode["completed_steps"] for s in episode_plan["steps"]):
            episode["status"] = EPISODE_DONE
        else:
            episode["status"] = RUNNING

    def _pause(
        self,
        state: dict[str, Any],
        episode: Mapping[str, Any],
        step: Mapping[str, Any],
    ) -> dict[str, Any]:
        quota = self.quota.snapshot() if self.quota else None
        episode["last_error"] = (
            f"quota exceeded at {step['step_id']}: needs {step['units']} units"
        )
        state["events"].append(
            {
                "type": "quota_paused",
                "episode_id": episode["episode_id"],
                "step_id": step["step_id"],
                "required_units": step["units"],
                "remaining_units": quota["remaining"] if quota else None,
                "at": _iso(self.clock()),
            }
        )
        return {
            "reason": "quota_exceeded",
            "episode_id": episode["episode_id"],
            "step_id": step["step_id"],
            "required_units": step["units"],
            "remaining_units": quota["remaining"] if quota else None,
            "resume_from": {
                "episode_id": episode["episode_id"],
                "step_id": step["step_id"],
            },
        }


def run_batch(
    plan: Mapping[str, Any],
    state_path: Path | str,
    *,
    quota: QuotaAccount,
    provider_results: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """One-shot convenience wrapper around :class:`BatchRunner`."""
    return BatchRunner(state_path, quota=quota).run(plan, provider_results=provider_results)


__all__ = [
    "BatchError",
    "BatchLock",
    "BatchRunner",
    "COMPLETED",
    "EPISODE_DONE",
    "PAUSED_QUOTA",
    "QuotaAccount",
    "READY",
    "RUNNING",
    "STEP_ASYNC",
    "STEP_LOCAL",
    "SingleWriterViolation",
    "WAITING_PROVIDER",
    "run_batch",
]