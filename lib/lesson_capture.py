"""Automatic lesson capture (v2.6 LESSON-01 / CARD-L02).

Three signals already exist in every run and each one is evidence that something
went wrong:

1. a REJECT in ``reviews.jsonl`` (reason code + stage + evidence);
2. a blocking entry in a ``qa_*.json`` review report;
3. a second ``decision_log`` entry for the same (category, subject) pair — a
   revision means the earlier decision was wrong.

Capture writes an AgentGate-compatible pending lesson under
``.governance/pending-lessons/`` so the same record can later be reviewed and
promoted. Deduplication is by fingerprint: the same pattern seen twice bumps
``occurrence_count`` instead of creating a second file.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

PENDING_DIRNAME = Path(".governance") / "pending-lessons"
SOURCE_REPO_DEFAULT = "OpenMontage"


class LessonCaptureError(ValueError):
    """A capture input could not be turned into a pending lesson."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase so the same mistake fingerprints equal."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def fingerprint(pattern_type: str, snippet: str) -> str:
    """16 hex chars: sha256(pattern_type + normalized snippet)."""
    digest = hashlib.sha256(f"{pattern_type}|{normalize_text(snippet)}".encode("utf-8"))
    return digest.hexdigest()[:16]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return slug or "lesson"


def _pending_dir(root: Path) -> Path:
    return Path(root) / PENDING_DIRNAME


def _existing_by_fingerprint(directory: Path, fp: str) -> Optional[Path]:
    for path in directory.glob("*.yml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, Mapping) and data.get("fingerprint") == fp:
            return path
    return None


def _write_or_bump(
    root: Path,
    *,
    pattern_type: str,
    snippet: str,
    source_ref: str,
    file: str,
    line: int,
    rule_id: str,
    summary: str,
    raw_fragment: str,
    regression: str,
    source_repo: str = SOURCE_REPO_DEFAULT,
) -> Path:
    directory = _pending_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    fp = fingerprint(pattern_type, snippet)
    existing = _existing_by_fingerprint(directory, fp)
    if existing is not None:
        data = yaml.safe_load(existing.read_text(encoding="utf-8")) or {}
        data["occurrence_count"] = int(data.get("occurrence_count") or 1) + 1
        repos = list(data.get("repos_seen") or [source_repo])
        if source_repo not in repos:
            repos.append(source_repo)
        data["repos_seen"] = repos
        existing.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return existing

    record = {
        "id": str(uuid.uuid4()),
        "pattern_type": pattern_type,
        "source_repo": source_repo,
        "source_ref": source_ref,
        "fingerprint": fp,
        "detected_at": _now(),
        "failure_context": {
            "file": file,
            "line": int(line),
            "snippet": snippet[:200],
        },
        "evidence": {
            "rule_id": rule_id,
            "scan_summary": summary,
            "raw_diff_fragment": raw_fragment[:500],
        },
        "regression": regression,
        "status": "pending",
        "occurrence_count": 1,
        "repos_seen": [source_repo],
    }
    path = directory / f"{_slug(pattern_type)}-{fp[:8]}.yml"
    path.write_text(yaml.safe_dump(record, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def capture_rejection(
    root: Path,
    record: Mapping[str, Any],
    *,
    source_ref: str = "local",
    source_repo: str = SOURCE_REPO_DEFAULT,
) -> Path:
    """Capture a REJECT from reviews.jsonl."""
    decision = record.get("decision") if isinstance(record.get("decision"), Mapping) else record
    if str(decision.get("action", "")).lower() != "reject":
        raise LessonCaptureError("capture_rejection expects an action='reject' record")
    reason = str(decision.get("reason_code") or "").strip()
    if not reason:
        raise LessonCaptureError("rejected review has no reason_code")
    stage = str(record.get("stage") or "unknown")
    evidence = str(decision.get("evidence") or decision.get("note") or reason)
    return _write_or_bump(
        root,
        pattern_type=reason,
        snippet=f"{stage}: {evidence}",
        source_ref=source_ref,
        source_repo=source_repo,
        file=f"reviews.jsonl#{record.get('review_id', '?')}",
        line=0,
        rule_id=reason,
        summary=f"{stage} REJECT with {reason}",
        raw_fragment=evidence,
        regression=f"review {record.get('review_id', '?')} rejected at {stage}: {evidence}",
    )


def capture_manual(
    root: Path,
    *,
    reason_code: str,
    stage: str,
    evidence: str,
    enforcement: str = "soft",
    fix: str = "",
    regression: str = "",
    predicate: Optional[Mapping[str, Any]] = None,
    regression_fixture: Optional[str] = None,
    source_ref: str = "local",
    source_repo: str = SOURCE_REPO_DEFAULT,
) -> Path:
    """Capture a user-voiced objection (v2.6 LESSON-02).

    This is the only channel through which a judgement voiced in conversation
    becomes a structured record; without it the objection evaporates with the
    session.
    """
    from lib.review import REASON_CODES

    reason = (reason_code or "").strip()
    if not reason:
        raise LessonCaptureError("reason_code is required")
    if reason not in REASON_CODES:
        raise LessonCaptureError(f"reason_code '{reason}' is not registered in lib/review.py")
    if enforcement not in ("soft", "hard"):
        raise LessonCaptureError("enforcement must be 'soft' or 'hard'")
    if not (stage or "").strip():
        raise LessonCaptureError("stage is required")
    if not (evidence or "").strip():
        raise LessonCaptureError("evidence is required (quote the user)")
    if enforcement == "hard" and not predicate:
        raise LessonCaptureError("hard lessons need a predicate (v2.6 D24)")
    if enforcement == "hard" and not regression_fixture:
        raise LessonCaptureError("hard lessons need a regression_fixture (v2.6 D24)")

    path = _write_or_bump(
        root,
        pattern_type=reason,
        snippet=f"{stage}: {evidence}",
        source_ref=source_ref,
        source_repo=source_repo,
        file="user_feedback",
        line=0,
        rule_id=reason,
        summary=f"user objection at {stage}: {evidence}",
        raw_fragment=evidence,
        regression=regression or evidence,
    )
    if enforcement == "hard" or fix or predicate:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["enforcement"] = enforcement
        if fix:
            data["fix"] = fix
        if predicate:
            data["predicate"] = dict(predicate)
        if regression_fixture:
            data["regression_fixture"] = regression_fixture
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def capture_qa_blocking(
    root: Path,
    report: Mapping[str, Any],
    *,
    source_ref: str = "local",
    source_repo: str = SOURCE_REPO_DEFAULT,
) -> list[Path]:
    """Capture every blocking entry from a qa_*.json review report."""
    written: list[Path] = []
    for entry in report.get("blocking") or []:
        if not isinstance(entry, Mapping):
            continue
        check = str(entry.get("check") or "OTHER")
        issue = str(entry.get("issue") or "")
        evidence = str(entry.get("evidence") or issue)
        written.append(
            _write_or_bump(
                root,
                pattern_type=f"qa-{check}",
                snippet=f"{report.get('stage', 'unknown')}: {issue}",
                source_ref=source_ref,
                source_repo=source_repo,
                file=f"qa#{entry.get('shot', '?')}",
                line=0,
                rule_id=check,
                summary=issue or check,
                raw_fragment=evidence,
                regression=f"{entry.get('shot', '?')} blocking at {check}: {issue}",
            )
        )
    return written


def capture_decision_revisions(
    root: Path,
    entries: Iterable[Mapping[str, Any]],
    *,
    source_ref: str = "local",
    source_repo: str = SOURCE_REPO_DEFAULT,
) -> list[Path]:
    """Capture a decision that was superseded: the earlier call was wrong."""
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for entry in entries:
        key = (str(entry.get("category") or ""), str(entry.get("subject") or ""))
        grouped.setdefault(key, []).append(entry)

    written: list[Path] = []
    for (category, subject), items in grouped.items():
        if len(items) < 2:
            continue
        latest = items[-1]
        first = items[0]
        reason = str(latest.get("reason") or "")
        written.append(
            _write_or_bump(
                root,
                pattern_type=f"decision-revision-{category or 'unknown'}",
                snippet=f"{category}/{subject}",
                source_ref=source_ref,
                source_repo=source_repo,
                file="decision_log.jsonl",
                line=0,
                rule_id=category or "unknown",
                summary=f"{subject}: {first.get('decision')} -> {latest.get('decision')}",
                raw_fragment=reason,
                regression=reason or f"{subject} decision was revised",
            )
        )
    return written