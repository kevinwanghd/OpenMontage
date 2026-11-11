"""Short-drama seven-item quality gate (ORC-08).

The gate covers the seven P0 checks — aspect ratio (画幅), safe area (安全区),
loudness (响度), hook (钩子), identity consistency (一致性), subtitles (字幕),
composition/background (构图背景) — and keeps the MACHINE verdict strictly
separate from the HUMAN verdict.

Invariants (ORC-08, X-2, §3 status semantics):

- Every check records machine status/evidence/threshold version and human
  status/evidence/person/time independently. ``machine UNKNOWN`` is never
  silently reported as ``PASS``.
- Required checks in ``FAIL`` or ``UNKNOWN`` block release. ``N/A`` requires a
  recorded reason and does not block (the item genuinely does not apply).
- ``hook``, ``identity`` and ``composition_background`` require human
  confirmation. A machine-only PASS on those checks cannot release the episode.
- A human PASS on an UNKNOWN machine result is an override and REQUIRES
  evidence (X-2). A human PASS can never override a machine ``FAIL``.
- Machine results carry the threshold version that judged them. When the
  policy threshold version changes, the old result is stale and evaluates as
  ``UNKNOWN`` until re-measured (ORC-09, F19); the historical record is kept.

Inputs are already-measured results or annotations: this module performs no
audio/video analysis and makes zero external calls (M0). Other subsystems pass
measurements to :meth:`QualityGate.report_machine` or pre-computed verdicts to
:meth:`QualityGate.report_machine_result`.

The per-episode log is append-only (``quality_gate.jsonl``); the current state
is a projection. Single-writer discipline as in ORC-10.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from lib.checkpoint import PROJECTS_DIR

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NA = "N/A"

STATUSES = {PASS, FAIL, UNKNOWN, NA}

# Canonical check ids (order is the documented ORC-08 order).
CHECKS = (
    "aspect_ratio",        # 画幅
    "safe_area",           # 安全区
    "loudness",            # 响度
    "hook",                # 钩子
    "identity",            # 一致性（角色身份）
    "subtitles",           # 字幕
    "composition_background",  # 构图背景
)

# Checks that cannot be released on machine evidence alone (ORC-08).
HUMAN_REQUIRED = frozenset({"hook", "identity", "composition_background"})


class QualityGateError(Exception):
    """Invalid check, status transition or missing evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: Optional[str]) -> str:
    return value or _now()


@dataclass(frozen=True)
class ThresholdSpec:
    """Versioned thresholds for one check.

    ``version`` is recorded with every machine result so a threshold change can
    be detected and the old measurement marked stale (ORC-09).
    """

    check_id: str
    version: str
    params: Mapping[str, Any] = field(default_factory=dict)


def default_thresholds() -> dict[str, ThresholdSpec]:
    return {
        "aspect_ratio": ThresholdSpec("aspect_ratio", "aspect-v1", {
            "aspect_width": 9, "aspect_height": 16, "tolerance": 0.01,
        }),
        # TikTok P0 preset: interaction rail on the right, caption/music zone
        # at the bottom (VIS-06). Normalised 0..1 rectangles.
        "safe_area": ThresholdSpec("safe_area", "safe-area-tiktok-v1", {
            "safe_x": (0.05, 0.82), "safe_y": (0.08, 0.80),
        }),
        "loudness": ThresholdSpec("loudness", "loudness-ebur128-v1", {
            "target_lufs": -14.0, "tolerance_lu": 1.0, "max_true_peak_dbtp": -1.0,
        }),
        # Structural hook density only; content effectiveness is human.
        "hook": ThresholdSpec("hook", "hook-structure-v1", {
            "hook_window_s": 3.0, "min_hook_candidates": 5,
        }),
        # A4: 0.75 initial, calibrated per embedder/binding in M1.
        "identity": ThresholdSpec("identity", "identity-threshold-a4-v1", {
            "min_face_similarity": 0.75, "min_detection_coverage": 0.5,
        }),
        # A5: <=12 words and <=3s initial.
        "subtitles": ThresholdSpec("subtitles", "subtitle-a5-v1", {
            "max_words": 12, "max_duration_s": 3.0,
        }),
        "composition_background": ThresholdSpec("composition_background", "composition-human-v1", {}),
    }


@dataclass(frozen=True)
class QualityPolicy:
    version: str = "gate-policy-v1"
    thresholds: Mapping[str, ThresholdSpec] = field(default_factory=default_thresholds)
    required: frozenset = frozenset(CHECKS)
    human_required: frozenset = HUMAN_REQUIRED

    def threshold(self, check_id: str) -> ThresholdSpec:
        try:
            return self.thresholds[check_id]
        except KeyError:
            raise QualityGateError(f"unknown check '{check_id}'")

    def with_threshold(self, check_id: str, version: str, **params: Any) -> "QualityPolicy":
        """Return a new policy with a bumped threshold version for one check."""
        spec = self.threshold(check_id)
        merged = {**dict(spec.params), **params}
        thresholds = dict(self.thresholds)
        thresholds[check_id] = ThresholdSpec(check_id, version, merged)
        return replace(self, thresholds=thresholds)


def _rect(value: Any) -> Optional[tuple[float, float, float, float]]:
    """Normalise a rect given as [x,y,w,h] or {x,y,w,h} to a tuple."""
    if isinstance(value, Mapping):
        try:
            return (float(value["x"]), float(value["y"]),
                    float(value["w"]), float(value["h"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(float(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def _within(rect: tuple[float, float, float, float],
            x_range: tuple[float, float], y_range: tuple[float, float]) -> bool:
    x, y, w, h = rect
    return (x >= x_range[0] and y >= y_range[0]
            and x + w <= x_range[1] and y + h <= y_range[1])


# --- machine handlers: (measurement, params) -> (status, evidence, detail) ---

def _m_aspect_ratio(m: Mapping[str, Any], p: Mapping[str, Any]):
    width, height = m.get("width"), m.get("height")
    if not width or not height:
        return UNKNOWN, "width/height measurement missing", {}
    expected = p["aspect_width"] / p["aspect_height"]
    actual = float(width) / float(height)
    detail = {"actual_aspect": actual, "expected_aspect": expected}
    if abs(actual - expected) <= p["tolerance"]:
        return PASS, f"{width}x{height} matches {p['aspect_width']}:{p['aspect_height']}", detail
    return FAIL, f"{width}x{height} aspect {actual:.4f} != {expected:.4f}", detail


def _m_safe_area(m: Mapping[str, Any], p: Mapping[str, Any]):
    if not m.get("face_detection_available"):
        return UNKNOWN, "face bbox unavailable (face_tracker not applied)", {}
    subtitle = _rect(m.get("subtitle_rect"))
    if subtitle is None:
        return UNKNOWN, "subtitle burned rectangle missing", {}
    x_range, y_range = tuple(p["safe_x"]), tuple(p["safe_y"])
    violations = []
    detail: dict[str, Any] = {}
    if not _within(subtitle, x_range, y_range):
        violations.append("subtitle_rect")
    detail["subtitle_rect"] = list(subtitle)
    faces = []
    for i, raw in enumerate(m.get("face_bboxes") or []):
        bbox = _rect(raw)
        if bbox is None:
            return UNKNOWN, f"face bbox #{i} malformed", detail
        faces.append(list(bbox))
        if not _within(bbox, x_range, y_range):
            violations.append(f"face_bbox[{i}]")
    detail["face_bboxes"] = faces
    if violations:
        return FAIL, f"outside TikTok safe area: {', '.join(violations)}", detail
    return PASS, "subtitle rect and all face bboxes inside preset safe area", detail


def _m_loudness(m: Mapping[str, Any], p: Mapping[str, Any]):
    lufs, peak = m.get("integrated_lufs"), m.get("true_peak_dbtp")
    if lufs is None or peak is None:
        return UNKNOWN, "loudness measurement incomplete", {}
    detail = {"integrated_lufs": lufs, "true_peak_dbtp": peak}
    target, tol = p["target_lufs"], p["tolerance_lu"]
    if abs(float(lufs) - target) > tol:
        return FAIL, f"{lufs} LUFS outside {target}±{tol}", detail
    if float(peak) > p["max_true_peak_dbtp"]:
        return FAIL, f"true peak {peak} dBTP above {p['max_true_peak_dbtp']}", detail
    return PASS, f"{lufs} LUFS / {peak} dBTP within target", detail


def _m_hook(m: Mapping[str, Any], p: Mapping[str, Any]):
    at, count = m.get("hook_at_s"), m.get("hook_candidates")
    if at is None or count is None:
        return UNKNOWN, "hook timestamps/candidates missing", {}
    detail = {"hook_at_s": at, "hook_candidates": count}
    if float(at) > p["hook_window_s"]:
        return FAIL, f"first hook at {at}s after {p['hook_window_s']}s window", detail
    if int(count) < p["min_hook_candidates"]:
        return FAIL, f"{count} hook candidates < {p['min_hook_candidates']}", detail
    return PASS, f"hook at {at}s, {count} candidates (content effectiveness is human)", detail


def _m_identity(m: Mapping[str, Any], p: Mapping[str, Any]):
    if not m.get("detector_available"):
        return UNKNOWN, "identity tool/detector unavailable", {}
    if m.get("multi_face"):
        return UNKNOWN, "multi-face output; identity score not trustworthy (F7)", {}
    coverage = m.get("detection_coverage")
    if coverage is None or float(coverage) < p["min_detection_coverage"]:
        return UNKNOWN, f"detection coverage {coverage} below {p['min_detection_coverage']}", {}
    score = m.get("face_similarity")
    if score is None:
        return UNKNOWN, "face similarity not produced (no detected reference)", {}
    detail = {"face_similarity": score, "detection_coverage": coverage}
    if float(score) >= p["min_face_similarity"]:
        return PASS, f"similarity {score} >= {p['min_face_similarity']}", detail
    return FAIL, f"similarity {score} < {p['min_face_similarity']}", detail


def _m_subtitles(m: Mapping[str, Any], p: Mapping[str, Any]):
    cues = m.get("cues")
    if not isinstance(cues, list):
        return UNKNOWN, "subtitle cues missing", {}
    if not cues:
        return FAIL, "no subtitle cues", {}
    violations = []
    for i, cue in enumerate(cues):
        text = str(cue.get("text", "")).strip()
        words = len(text.split())
        try:
            start, end = float(cue["start_s"]), float(cue["end_s"])
        except (KeyError, TypeError, ValueError):
            return UNKNOWN, f"cue #{i} timing malformed", {}
        duration = end - start
        if words == 0 or duration <= 0:
            violations.append(f"cue[{i}] empty/zero-duration")
        elif words > p["max_words"] or duration > p["max_duration_s"]:
            violations.append(f"cue[{i}] {words}w/{duration:.2f}s")
    detail = {"cue_count": len(cues)}
    if violations:
        return FAIL, "; ".join(violations), detail
    return PASS, f"{len(cues)} cues within word/duration limits", detail


def _m_composition_background(m: Mapping[str, Any], p: Mapping[str, Any]):
    # Composition/background quality is a human judgement (VIS-02).
    return UNKNOWN, "machine cannot judge composition/background; human review required", {}


_MACHINE: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], tuple[str, str, dict]]] = {
    "aspect_ratio": _m_aspect_ratio,
    "safe_area": _m_safe_area,
    "loudness": _m_loudness,
    "hook": _m_hook,
    "identity": _m_identity,
    "subtitles": _m_subtitles,
    "composition_background": _m_composition_background,
}


def evaluate_machine(check_id: str, measurement: Optional[Mapping[str, Any]],
                     spec: ThresholdSpec) -> tuple[str, str, dict]:
    """Judge one check from an already-measured input.

    Returns ``(status, evidence, detail)``. Missing input is ``UNKNOWN``, never
    ``PASS``. An explicit ``{"na_reason": "..."}`` marks the item N/A.
    """
    handler = _MACHINE.get(check_id)
    if handler is None:
        raise QualityGateError(f"unknown check '{check_id}'")
    if measurement is None:
        return UNKNOWN, "no measurement supplied", {}
    na_reason = measurement.get("na_reason")
    if na_reason:
        return NA, f"N/A: {na_reason}", {"na_reason": na_reason}
    return handler(measurement, spec.params)


@dataclass
class CheckRecord:
    check_id: str
    required: bool = True
    machine_status: str = UNKNOWN
    machine_evidence: str = ""
    machine_threshold_version: str = ""
    machine_measurement: dict[str, Any] = field(default_factory=dict)
    machine_reported_at: Optional[str] = None
    human_status: Optional[str] = None
    human_evidence: str = ""
    human_confirmed_by: Optional[str] = None
    human_confirmed_at: Optional[str] = None
    human_threshold_version: str = ""
    na_reason: str = ""


def effective_status(record: CheckRecord, human_required: bool, *,
                     stale: bool = False) -> str:
    """Resolve machine + human evidence into one gate status.

    Order matters: a FAIL anywhere is final; N/A is final; a stale machine
    result degrades to UNKNOWN; human-required checks stay UNKNOWN until a
    human records PASS/FAIL (with evidence).
    """
    if record.machine_status == FAIL or record.human_status == FAIL:
        return FAIL
    if record.machine_status == NA:
        return NA
    if stale:
        return UNKNOWN
    if human_required:
        if record.human_status == PASS:
            return PASS
        if record.human_status == NA:
            return NA
        return UNKNOWN
    return PASS if record.machine_status == PASS else UNKNOWN


class QualityGate:
    """Append-only per-episode quality-gate log with a projected current state."""

    _lock = threading.Lock()

    def __init__(self, project: str, episode: str, *,
                 policy: Optional[QualityPolicy] = None,
                 projects_dir: Path = PROJECTS_DIR):
        self.project = project
        self.episode = episode
        self.policy = policy or QualityPolicy()
        self.dir = Path(projects_dir) / project / episode
        self.path = self.dir / "quality_gate.jsonl"

    # --- persistence ---

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        rec = {"version": "1.0", "project": self.project,
               "episode": self.episode, **record}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def history(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _project(self) -> dict[str, CheckRecord]:
        out = {cid: CheckRecord(cid, required=cid in self.policy.required)
               for cid in CHECKS}
        for ev in self.history():
            cid = ev.get("check_id")
            if cid not in out:
                continue
            rec = out[cid]
            kind = ev.get("event_type")
            if kind in ("machine", "na"):
                rec.machine_status = ev["status"]
                rec.machine_evidence = ev.get("evidence", "")
                rec.machine_threshold_version = ev.get("threshold_version", "")
                rec.machine_measurement = ev.get("measurement") or {}
                rec.machine_reported_at = ev.get("reported_at")
                rec.na_reason = (ev.get("measurement") or {}).get("na_reason", "")
            elif kind == "human":
                rec.human_status = ev["status"]
                rec.human_evidence = ev.get("evidence", "")
                rec.human_confirmed_by = ev.get("confirmed_by")
                rec.human_confirmed_at = ev.get("confirmed_at")
                rec.human_threshold_version = ev.get("threshold_version", "")
        return out

    # --- recording machine results ---

    def report_machine(self, check_id: str, measurement: Optional[Mapping[str, Any]],
                       *, evidence: str = "") -> CheckRecord:
        """Measure + judge one check from an already-produced measurement."""
        spec = self.policy.threshold(check_id)
        status, machine_evidence, detail = evaluate_machine(check_id, measurement, spec)
        merged = {**(dict(measurement) if measurement else {}), **detail}
        if measurement and measurement.get("na_reason"):
            merged["na_reason"] = measurement["na_reason"]
        self._append({
            "check_id": check_id, "event_type": "machine", "status": status,
            "evidence": evidence or machine_evidence,
            "threshold_version": spec.version, "measurement": merged,
            "reported_at": _now(),
        })
        return self._project()[check_id]

    def report_machine_result(self, check_id: str, status: str, *,
                              evidence: str, measurement: Optional[Mapping[str, Any]] = None,
                              threshold_version: Optional[str] = None) -> CheckRecord:
        """Record a pre-computed machine verdict from another subsystem.

        ``threshold_version`` should be the version the checker actually used;
        it defaults to the current policy version.
        """
        if check_id not in CHECKS:
            raise QualityGateError(f"unknown check '{check_id}'")
        if status not in STATUSES:
            raise QualityGateError(f"invalid status '{status}'")
        if status in (PASS, FAIL) and not evidence:
            raise QualityGateError(f"{status} requires evidence")
        spec = self.policy.threshold(check_id)
        merged = dict(measurement or {})
        if status == NA:
            reason = merged.get("na_reason") or evidence
            if not reason:
                raise QualityGateError("N/A requires a reason")
            merged["na_reason"] = reason
        self._append({
            "check_id": check_id, "event_type": "machine", "status": status,
            "evidence": evidence or merged.get("na_reason", ""),
            "threshold_version": threshold_version or spec.version,
            "measurement": merged, "reported_at": _now(),
        })
        return self._project()[check_id]

    def record_na(self, check_id: str, reason: str, *, by: str = "owner") -> CheckRecord:
        """Mark a check not applicable with a recorded reason (does not block)."""
        if check_id not in CHECKS:
            raise QualityGateError(f"unknown check '{check_id}'")
        if not reason.strip():
            raise QualityGateError("N/A requires a reason")
        self._append({
            "check_id": check_id, "event_type": "na", "status": NA,
            "evidence": reason, "threshold_version": self.policy.threshold(check_id).version,
            "measurement": {"na_reason": reason}, "reported_at": _now(),
            "recorded_by": by,
        })
        return self._project()[check_id]

    # --- recording human confirmation ---

    def record_human(self, check_id: str, status: str, *, evidence: str,
                     confirmed_by: str, confirmed_at: Optional[str] = None) -> CheckRecord:
        if check_id not in CHECKS:
            raise QualityGateError(f"unknown check '{check_id}'")
        if status not in (PASS, FAIL, NA):
            raise QualityGateError(f"invalid human status '{status}'")
        if not confirmed_by.strip():
            raise QualityGateError("human decision requires confirmed_by")
        if not evidence.strip():
            raise QualityGateError("human decision requires recorded evidence")
        rec = self._project()[check_id]
        if status == PASS and rec.machine_status == FAIL:
            raise QualityGateError(
                f"machine FAIL on '{check_id}' cannot be overridden by human PASS")
        self._append({
            "check_id": check_id, "event_type": "human", "status": status,
            "evidence": evidence, "confirmed_by": confirmed_by,
            "confirmed_at": _ts(confirmed_at),
            "threshold_version": self.policy.threshold(check_id).version,
        })
        return self._project()[check_id]

    # --- evaluation ---

    def checks(self) -> dict[str, CheckRecord]:
        return self._project()

    def _stale(self, rec: CheckRecord) -> bool:
        """Machine result judged under a superseded threshold (ORC-09)."""
        if rec.machine_status == UNKNOWN or not rec.machine_threshold_version:
            return False
        return rec.machine_threshold_version != self.policy.threshold(rec.check_id).version

    def blocking_checks(self) -> list[str]:
        checks = self._project()
        out = []
        for cid, rec in checks.items():
            if not rec.required:
                continue
            status = effective_status(rec, cid in self.policy.human_required,
                                      stale=self._stale(rec))
            if status in (FAIL, UNKNOWN):
                out.append(cid)
        return out

    def can_release(self) -> bool:
        return not self.blocking_checks()

    def evaluate(self) -> dict[str, Any]:
        checks = self._project()
        detail: dict[str, Any] = {}
        for cid, rec in checks.items():
            human_required = cid in self.policy.human_required
            stale = self._stale(rec)
            status = effective_status(rec, human_required, stale=stale)
            detail[cid] = {
                "required": rec.required,
                "human_required": human_required,
                "machine": {
                    "status": rec.machine_status,
                    "evidence": rec.machine_evidence,
                    "threshold_version": rec.machine_threshold_version,
                    "stale": stale,
                    "reported_at": rec.machine_reported_at,
                },
                "human": {
                    "status": rec.human_status,
                    "evidence": rec.human_evidence,
                    "confirmed_by": rec.human_confirmed_by,
                    "confirmed_at": rec.human_confirmed_at,
                    "threshold_version": rec.human_threshold_version,
                },
                "effective_status": status,
                "blocking": rec.required and status in (FAIL, UNKNOWN),
            }
        blocking = [cid for cid, d in detail.items() if d["blocking"]]
        return {
            "version": "1.0",
            "gate": "shortdrama_quality",
            "project": self.project,
            "episode": self.episode,
            "policy_version": self.policy.version,
            "checks": detail,
            "verdict": PASS if not blocking else "BLOCKED",
            "blocking_checks": blocking,
            "evaluated_at": _now(),
        }