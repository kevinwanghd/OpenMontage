"""Downstream invalidation / stale propagation for the short-drama pipeline (ORC-09, F19).

When an upstream artifact, config, media version or review target changes, the
dependent downstream artifacts, reviews and quality gates must be identified and
marked stale — WITHOUT deleting history and WITHOUT overwriting old versions.

Model:

- Nodes are versioned artifacts/records (``node_id`` like ``script:ep01``) of
  kinds: script, beat_map, character_ref, keyframe, shot_media, assembly,
  identity_score, review, quality_gate, binding, threshold, ...
- Edges are declared upstream -> downstream. Invalidating a node marks its
  transitive dependents stale, keeps currently-valid unrelated nodes, and
  returns the affected set plus the shots that must be re-queued.
- A re-run registers a NEW version; the old version stays queryable in history
  with ``superseded_by`` set. Staleness is recorded per (node, version), so the
  attached history remains auditable.

F19 specifics:

- ``binding_changed(old, new)`` marks every identity score computed under the
  old binding stale/UNKNOWN and re-queues its shots, then propagates downstream.
- ``threshold_changed(check, old_version, new_version)`` does the same for
  identity scores measured under the superseded threshold version.

The log is append-only (``invalidation.jsonl``); state is a projection.
M0: local files, zero external API calls.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from lib.checkpoint import PROJECTS_DIR

STALE = "stale"
UNKNOWN = "UNKNOWN"


class InvalidationError(Exception):
    """Unknown node/edge or invalid invalidation request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NodeVersion:
    node_id: str
    kind: str
    version: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    superseded_by: Optional[str] = None
    stale: bool = False
    stale_cause: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "kind": self.kind, "version": self.version,
            "payload": self.payload, "created_at": self.created_at,
            "superseded_by": self.superseded_by, "stale": self.stale,
            "stale_cause": self.stale_cause,
        }


class InvalidationLog:
    """Append-only version/dependency/staleness log for one episode."""

    _lock = threading.Lock()

    def __init__(self, project: str, episode: str, projects_dir: Path = PROJECTS_DIR):
        self.project = project
        self.episode = episode
        self.dir = Path(projects_dir) / project / episode
        self.path = self.dir / "invalidation.jsonl"

    # --- persistence ---

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        rec = {"version": "1.0", "project": self.project,
               "episode": self.episode, "ts": _now(), **record}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # --- projections ---

    def versions(self) -> dict[str, list[NodeVersion]]:
        """All recorded versions per node, in append order."""
        out: dict[str, list[NodeVersion]] = {}
        for ev in self.events():
            if ev.get("event_type") != "node":
                continue
            out.setdefault(ev["node_id"], []).append(NodeVersion(
                node_id=ev["node_id"], kind=ev["kind"], version=ev["version"],
                payload=ev.get("payload") or {}, created_at=ev["ts"],
            ))
        # supersede + stale apply to (node, version)
        for ev in self.events():
            if ev.get("event_type") == "supersede":
                for nv in out.get(ev["node_id"], []):
                    if nv.version == ev["version"]:
                        nv.superseded_by = ev["superseded_by"]
            elif ev.get("event_type") == "stale":
                for nv in out.get(ev["node_id"], []):
                    if nv.version == ev["version"]:
                        nv.stale = True
                        nv.stale_cause = ev.get("cause")
        return out

    def current(self) -> dict[str, NodeVersion]:
        return {node_id: versions[-1]
                for node_id, versions in self.versions().items() if versions}

    def _edges(self) -> list[tuple[str, str]]:
        return [(ev["upstream"], ev["downstream"]) for ev in self.events()
                if ev.get("event_type") == "edge"]

    # --- graph maintenance ---

    def register_node(self, node_id: str, kind: str, *, version: Optional[str] = None,
                      payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Register a new version of a node; previous versions are retained."""
        current = self.current().get(node_id)
        if version is None:
            version = f"v{len(self.versions().get(node_id, [])) + 1}"
        record = self._append({
            "event_type": "node", "node_id": node_id, "kind": kind,
            "version": version, "payload": payload or {},
        })
        if current is not None:
            self._append({"event_type": "supersede", "node_id": node_id,
                          "version": current.version, "superseded_by": version})
        return record

    def add_dependency(self, upstream: str, downstream: str) -> None:
        known = self.current()
        for node_id in (upstream, downstream):
            if node_id not in known:
                raise InvalidationError(f"unknown node '{node_id}'")
        if (upstream, downstream) in self._edges():
            return
        self._append({"event_type": "edge", "upstream": upstream,
                      "downstream": downstream})

    def register_review(self, review_id: str, *, targets: Iterable[str],
                        payload: Optional[dict[str, Any]] = None) -> str:
        """Register a review node depending on the artifacts/records it judged."""
        node_id = f"review:{review_id}"
        if node_id not in self.current():
            self.register_node(node_id, "review", payload=payload or {"review_id": review_id})
        for target in targets:
            self.add_dependency(target, node_id)
        return node_id

    def register_quality_gate(self, gate_id: str, *, targets: Iterable[str],
                              payload: Optional[dict[str, Any]] = None) -> str:
        node_id = f"quality_gate:{gate_id}"
        if node_id not in self.current():
            self.register_node(node_id, "quality_gate",
                               payload=payload or {"gate_id": gate_id})
        for target in targets:
            self.add_dependency(target, node_id)
        return node_id

    # --- traversal / staleness ---

    def dependents(self, node_id: str) -> set[str]:
        """Transitive downstream closure (excluding the node itself)."""
        adj: dict[str, set[str]] = {}
        for up, down in self._edges():
            adj.setdefault(up, set()).add(down)
        seen: set[str] = set()
        queue = deque(adj.get(node_id, ()))
        while queue:
            cur = queue.popleft()
            if cur in seen or cur == node_id:
                continue
            seen.add(cur)
            queue.extend(adj.get(cur, ()))
        return seen

    def _mark_stale(self, node_id: str, cause: dict[str, Any]) -> Optional[str]:
        current = self.current().get(node_id)
        if current is None or current.stale:
            return None
        self._append({"event_type": "stale", "node_id": node_id,
                      "version": current.version, "cause": cause})
        return current.version

    def stale_nodes(self) -> dict[str, NodeVersion]:
        return {nid: nv for nid, nv in self.current().items() if nv.stale}

    def requeue_shots(self) -> list[str]:
        """Shots whose identity score went stale/UNKNOWN and need re-queuing."""
        shots = set()
        for nid, nv in self.stale_nodes().items():
            if nv.kind == "identity_score":
                shot = nv.payload.get("shot_id")
                if shot:
                    shots.add(shot)
        return sorted(shots)

    def status_of(self, node_id: str) -> str:
        nv = self.current().get(node_id)
        if nv is None:
            raise InvalidationError(f"unknown node '{node_id}'")
        if not nv.stale:
            return str(nv.payload.get("status", "CURRENT"))
        if nv.kind == "identity_score":
            return UNKNOWN
        return STALE

    # --- invalidation ---

    def invalidate(self, node_id: str, *, new_version: Optional[str] = None,
                   reason: str = "", payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Register an upstream change and mark all transitive dependents stale."""
        if node_id not in self.current():
            raise InvalidationError(f"unknown node '{node_id}'")
        prior = self.current()[node_id]
        self.register_node(node_id, prior.kind, version=new_version,
                           payload=payload if payload is not None else dict(prior.payload))
        new_version = self.current()[node_id].version
        cause = {"type": "upstream_changed", "node_id": node_id,
                 "new_version": new_version, "reason": reason}
        affected = sorted(self.dependents(node_id))
        stale_versions = {}
        for dep in affected:
            version = self._mark_stale(dep, cause)
            if version:
                stale_versions[dep] = version
        return self._report(node_id, new_version, affected, stale_versions, reason)

    def binding_changed(self, old_binding_id: str, new_binding_id: str, *,
                        reason: str = "") -> dict[str, Any]:
        """Binding switch (F19): identity scores from the old binding are stale.

        Affected shots are re-queued; downstream reviews/gates are marked stale.
        """
        new_node = f"binding:{new_binding_id}"
        if new_node not in self.current():
            self.register_node(new_node, "binding",
                               payload={"binding_id": new_binding_id})
        cause = {"type": "binding_changed", "from": old_binding_id,
                 "to": new_binding_id, "reason": reason}
        affected = set()
        stale_versions = {}
        for nid, nv in self.current().items():
            if nv.kind == "identity_score" and nv.payload.get("binding_id") == old_binding_id:
                affected.add(nid)
                version = self._mark_stale(nid, cause)
                if version:
                    stale_versions[nid] = version
                affected.update(self.dependents(nid))
        for dep in sorted(affected):
            if dep not in stale_versions:
                version = self._mark_stale(dep, cause)
                if version:
                    stale_versions[dep] = version
        return self._report(old_binding_id, new_binding_id, sorted(affected),
                            stale_versions, reason)

    def threshold_changed(self, check_id: str, old_version: str, new_version: str, *,
                          reason: str = "") -> dict[str, Any]:
        """Threshold version bump (ORC-09): scores judged by the old version stale."""
        threshold_node = f"threshold:{check_id}"
        current = self.current().get(threshold_node)
        if current is None:
            self.register_node(threshold_node, "threshold", version=old_version,
                               payload={"check_id": check_id, "threshold_version": old_version})
        self.register_node(threshold_node, "threshold", version=new_version,
                           payload={"check_id": check_id, "threshold_version": new_version})
        cause = {"type": "threshold_changed", "check_id": check_id,
                 "from": old_version, "to": new_version, "reason": reason}
        affected = set()
        stale_versions = {}
        for nid, nv in self.current().items():
            if (nv.kind == "identity_score"
                    and nv.payload.get("check_id", "identity") == check_id
                    and nv.payload.get("threshold_version") == old_version):
                affected.add(nid)
                version = self._mark_stale(nid, cause)
                if version:
                    stale_versions[nid] = version
                affected.update(self.dependents(nid))
        for dep in sorted(affected):
            if dep not in stale_versions:
                version = self._mark_stale(dep, cause)
                if version:
                    stale_versions[dep] = version
        return self._report(threshold_node, new_version, sorted(affected),
                            stale_versions, reason)

    # --- helpers ---

    def register_identity_score(self, shot_id: str, *, binding_id: str,
                                threshold_version: str, score: Optional[float],
                                status: str = "PASS",
                                check_id: str = "identity") -> str:
        node_id = f"identity_score:{shot_id}"
        self.register_node(node_id, "identity_score", payload={
            "shot_id": shot_id, "binding_id": binding_id,
            "threshold_version": threshold_version, "score": score,
            "status": status, "check_id": check_id,
        })
        return node_id

    def history(self, node_id: str) -> list[NodeVersion]:
        return self.versions().get(node_id, [])

    def version(self, node_id: str, version: str) -> NodeVersion:
        for nv in self.history(node_id):
            if nv.version == version:
                return nv
        raise InvalidationError(f"node '{node_id}' has no version '{version}'")

    def summary(self) -> dict[str, Any]:
        nodes = {nid: {**nv.as_dict(), "status": self.status_of(nid)}
                 for nid, nv in self.current().items()}
        return {
            "version": "1.0",
            "project": self.project,
            "episode": self.episode,
            "nodes": nodes,
            "edges": [{"upstream": u, "downstream": d} for u, d in self._edges()],
            "stale_nodes": sorted(self.stale_nodes()),
            "requeue_shots": self.requeue_shots(),
        }

    def _report(self, source: str, new_version: str, affected: list[str],
                stale_versions: dict[str, str], reason: str) -> dict[str, Any]:
        current = self.current()
        retained = sorted(set(current) - set(affected) - {source})
        requeue = sorted({
            current[nid].payload["shot_id"]
            for nid in affected
            if nid in current and current[nid].kind == "identity_score"
            and current[nid].payload.get("shot_id")
        })
        return {
            "version": "1.0",
            "source": source,
            "new_version": new_version,
            "reason": reason,
            "affected": affected,
            "stale_versions": stale_versions,
            "retained": retained,
            "requeue_shots": requeue,
            "stale_identity_scores": sorted(
                nid for nid in affected
                if nid in current and current[nid].kind == "identity_score"),
            "evaluated_at": _now(),
        }