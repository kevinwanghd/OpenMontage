"""Evidence pack contract for accepted shots (GATE-04).

The 2026-11-17 review that "passed" was based on seven hand-picked stills. The
reviewer could not have seen that the dialogue shots had no mouth movement,
because the input never contained a way to see it: no contact sheet, no
per-timepoint frames, no audio.

This module defines what "reviewed" has to mean: a fixed set of files that
travel with the shot, and a rule that every finding cites one of them. It
validates and binds; it does not produce the files (the reviewer / agent does).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

PACK_DIRNAME = "evidence"
FRAME_TIMEPOINTS = (0, 25, 50, 75, 100)
MIN_CONTACT_SHEET_FRAMES = 12
PACK_MANIFEST = "pack.json"


def pack_dir(project_dir: Path | str, shot_id: str, *, episode: str = "ep01") -> Path:
    return Path(project_dir) / episode / PACK_DIRNAME / shot_id


def required_files(shot_id: str, *, episode: str = "ep01") -> list[str]:
    """Relative file names a complete pack must contain."""
    names = ["contact_sheet.jpg", "audio.wav", "transcript.json"]
    names += [f"frames/{point:03d}.jpg" for point in FRAME_TIMEPOINTS]
    return names


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(project_dir: Path | str, shot_id: str, *, episode: str = "ep01") -> dict[str, Any]:
    """Return blocking/warnings for one shot's evidence pack."""
    directory = pack_dir(project_dir, shot_id, episode=episode)
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not directory.is_dir():
        blocking.append({
            "shot": shot_id, "issue": "evidence pack missing",
            "severity": "P0",
            "fix": "write contact sheet, 5 timepoint frames, audio and transcript",
        })
        return {"blocking": blocking, "warnings": warnings}

    for name in required_files(shot_id, episode=episode):
        if not (directory / name).is_file():
            blocking.append({
                "shot": shot_id, "issue": f"evidence pack missing {name}",
                "severity": "P0",
                "fix": "regenerate the pack; a review without evidence is not a review",
            })

    manifest_path = directory / PACK_MANIFEST
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            blocking.append({
                "shot": shot_id, "issue": "evidence pack manifest is unreadable",
                "severity": "P0", "fix": f"rewrite {PACK_MANIFEST}",
            })
            manifest = None
        if isinstance(manifest, dict):
            frames = manifest.get("contact_sheet_frames")
            if isinstance(frames, int) and frames < MIN_CONTACT_SHEET_FRAMES:
                blocking.append({
                    "shot": shot_id,
                    "issue": f"contact sheet has {frames} frames, needs {MIN_CONTACT_SHEET_FRAMES}",
                    "severity": "P0",
                    "fix": "regenerate the contact sheet with more frames",
                })
    else:
        warnings.append({
            "shot": shot_id, "issue": "evidence pack has no pack.json binding",
            "severity": "P1",
            "fix": "bind the pack to the artifact hash so a swapped clip invalidates it",
        })
    return {"blocking": blocking, "warnings": warnings}


def bind(project_dir: Path | str, shot_id: str, artifact_path: Path | str,
         *, episode: str = "ep01") -> Path:
    """Record the artifact hash the pack was built from (atomic write)."""
    directory = pack_dir(project_dir, shot_id, episode=episode)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "shot_id": shot_id,
        "artifact": str(artifact_path),
        "artifact_sha256": _sha256(Path(artifact_path)),
        "contact_sheet_frames": MIN_CONTACT_SHEET_FRAMES,
        "timepoints": list(FRAME_TIMEPOINTS),
    }
    target = directory / PACK_MANIFEST
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    return target


def validate_finding(finding: dict[str, Any], project_dir: Path | str,
                     *, episode: str = "ep01") -> Optional[str]:
    """Return an error string when a P0 finding cites no usable evidence."""
    evidence = finding.get("evidence_path")
    if not evidence:
        return "finding cites no evidence_path"
    path = Path(str(evidence))
    if not path.is_absolute():
        path = Path(project_dir) / path
    if not path.is_file():
        return f"evidence_path does not exist: {evidence}"
    return None