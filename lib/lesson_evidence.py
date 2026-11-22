"""Producer for `lesson_evidence_<stage>.json` (v2.7 CARD-E01).

`shortdrama/driver.py` evaluates hard lessons against an evidence file that
nothing used to write. The predicates were therefore judged on a checkpoint and
an edit-decision blob only, and every measurement-based lesson (subtitle timing,
loudness, version binding) was effectively disarmed in real runs.

This module writes that file from artifacts that already exist. It never
generates, measures with an external service, or spends money: if a source is
missing, the *field* is missing, and the predicate that needs it reports
BLOCKED instead of silently passing.

Sources, by field:

===================================  ==========================================
``subtitles``                         ASS/SRT subtitle cues
``speech``                            TTS sidecar JSON (sentence start/end)
``silent_lines``                      subtitle indices with no overlapping speech
``loudness_lufs``                     render_report.json
``version_binding`` / ``current_version``  render_report.json
===================================  ==========================================

The write is atomic (temp file + rename) so a partially written evidence file
can never be mistaken for a complete one.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

EVIDENCE_PREFIX = "lesson_evidence_"
SUBTITLE_CANDIDATES = (
    "assembly_subtitle_subtitle_default.srt",
    "m1_subtitles.srt",
)
TTS_GLOB = "*tts*.json"
RENDER_REPORT_NAMES = ("render_report.json", "render_report_v6.json")

_ASS_TS = re.compile(r"^(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})[.:](?P<cs>\d{2})$")
_SRT_TS = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})\s*-->\s*"
    r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2}),(?P<ms2>\d{3})$"
)


class LessonEvidenceError(ValueError):
    """A source file exists but cannot be turned into evidence."""


def _seconds(hours: str, minutes: str, seconds: str, frac: str, frac_scale: float) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(frac) / frac_scale


def parse_subtitles(path: Path) -> list[dict[str, float]]:
    """Parse SRT or ASS cues into ``[{start, end}]`` in seconds."""
    text = path.read_text(encoding="utf-8", errors="replace")
    cues: list[dict[str, float]] = []

    if path.suffix.lower() == ".ass":
        for line in text.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            start, end = parts[1].strip(), parts[2].strip()
            m_start, m_end = _ASS_TS.match(start), _ASS_TS.match(end)
            if not (m_start and m_end):
                continue
            cues.append({
                "start": _seconds(m_start["h"], m_start["m"], m_start["s"], m_start["cs"], 100),
                "end": _seconds(m_end["h"], m_end["m"], m_end["s"], m_end["cs"], 100),
            })
        return cues

    for line in text.splitlines():
        match = _SRT_TS.match(line.strip())
        if not match:
            continue
        cues.append({
            "start": _seconds(match["h"], match["m"], match["s"], match["ms"], 1000),
            "end": _seconds(match["h2"], match["m2"], match["s2"], match["ms2"], 1000),
        })
    return cues


def parse_tts_speech(path: Path) -> list[dict[str, float]]:
    """Sentence intervals from a Doubao TTS sidecar (seconds may be in ms)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    sentences = ((data.get("data") or {}).get("sentences")) or []
    speech: list[dict[str, float]] = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        try:
            start = float(sentence["startTime"])
            end = float(sentence["endTime"])
        except (KeyError, TypeError, ValueError) as error:
            raise LessonEvidenceError(
                f"{path.name}: sentence without numeric startTime/endTime"
            ) from error
        # Sidecars in the wild carry both seconds and milliseconds; a value
        # above 600 cannot be a seconds offset in a short-drama line.
        if start > 600 or end > 600:
            start, end = start / 1000.0, end / 1000.0
        speech.append({"start": start, "end": end})
    return speech


def _first_existing(directory: Path, names: tuple[str, ...]) -> Optional[Path]:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _overlaps(a: dict[str, float], b: dict[str, float]) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _load_json(path: Path) -> Optional[Any]:
    """Read a JSON artifact. Absent is None; present-but-broken raises."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LessonEvidenceError(f"{path.name}: unreadable ({error})") from error


SECTION_ID = re.compile(r"(?:^|[_-])(s\d+)(?:[_-]|\.|$)")


def section_offsets(project_dir: Path) -> dict[str, float]:
    """Section id -> start time on the finished timeline, from the script."""
    script_path = Path(project_dir) / "artifacts" / "script.json"
    script = _load_json(script_path)
    if not isinstance(script, dict):
        return {}
    offsets: dict[str, float] = {}
    for section in script.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = section.get("id")
        start = section.get("start_seconds")
        if isinstance(section_id, str) and isinstance(start, (int, float)):
            offsets[section_id] = float(start)
    return offsets


def silent_section_windows(project_dir: Path) -> list[tuple[float, float]]:
    """Time windows of sections the script marks as having no dialogue.

    Windows, not indices: subtitle cues exist only for spoken lines, so a
    section's position in the script is not its position in the cue list.
    """
    script = _load_json(Path(project_dir) / "artifacts" / "script.json")
    if not isinstance(script, dict):
        return []
    windows: list[tuple[float, float]] = []
    for section in script.get("sections") or []:
        if not isinstance(section, dict) or str(section.get("text") or "").strip():
            continue
        start, end = section.get("start_seconds"), section.get("end_seconds")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            windows.append((float(start), float(end)))
    return windows


def collect(project_dir: Path | str, stage: str, *, episode: str = "ep01") -> dict[str, Any]:
    """Build the evidence payload for one stage from existing artifacts only."""
    project_dir = Path(project_dir)
    assets = project_dir / episode / "assets"
    artifacts = project_dir / "artifacts"
    evidence: dict[str, Any] = {}

    subtitle_path = _first_existing(assets, SUBTITLE_CANDIDATES)
    if subtitle_path is None:
        subtitle_path = _first_existing(project_dir, SUBTITLE_CANDIDATES)
    if subtitle_path is not None:
        cues = parse_subtitles(subtitle_path)
        if cues:
            evidence["subtitles"] = cues

    # A TTS sidecar's times are relative to that clip, not to the finished cut.
    # Adding them raw puts every line at 0-2s while the subtitles sit at 5-30s,
    # which is not a small error: it makes every cue look speechless. Only
    # sidecars that can be tied to a script section are usable, and each is
    # shifted by that section's start time.
    offsets = section_offsets(project_dir)
    speech: list[dict[str, float]] = []
    mapped_sections: set[str] = set()
    if assets.is_dir() and offsets:
        for sidecar in sorted(assets.glob(TTS_GLOB)):
            match = SECTION_ID.search(sidecar.name)
            if not match or match.group(1) not in offsets:
                continue
            section_id = match.group(1)
            if section_id in mapped_sections:
                raise LessonEvidenceError(
                    f"{sidecar.name}: more than one TTS sidecar maps to section "
                    f"{section_id}; cannot tell which track is in the cut"
                )
            shift = offsets[section_id]
            for interval in parse_tts_speech(sidecar):
                speech.append({
                    "start": interval["start"] + shift,
                    "end": interval["end"] + shift,
                })
            mapped_sections.add(section_id)
    if speech:
        evidence["speech"] = speech

    # `silent_lines` must come from the script, never from "no speech found".
    # The predicate skips silent lines outright, so inferring them from missing
    # evidence turns BLOCKED into a vacuous PASS — the exact failure this
    # producer exists to prevent.
    silent_windows = silent_section_windows(project_dir)
    if evidence.get("subtitles") and silent_windows:
        silent_indices = [
            index for index, cue in enumerate(evidence["subtitles"])
            # Fully inside, not merely starting inside: the incident cue began
            # at 3.64s, inside the 0-5s silent hook, and would have been skipped.
            if any(start <= cue["start"] and cue["end"] <= end
                   for start, end in silent_windows)
        ]
        if silent_indices:
            evidence["silent_lines"] = silent_indices

    # Render reports live beside the episode's other artifacts; older projects
    # also kept one at project level, so both are checked. Missing here would
    # silently drop loudness and version binding, disarming two hard lessons.
    report_path = (
        _first_existing(project_dir / episode / "artifacts", RENDER_REPORT_NAMES)
        or _first_existing(artifacts, RENDER_REPORT_NAMES)
    )
    if report_path is not None:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        audio = report.get("audio") if isinstance(report, dict) else None
        if isinstance(audio, dict) and audio.get("loudness_lufs") is not None:
            evidence["loudness_lufs"] = float(audio["loudness_lufs"])
        if isinstance(report, dict):
            if report.get("version_binding") is not None:
                evidence["version_binding"] = report["version_binding"]
            if report.get("current_version") is not None:
                evidence["current_version"] = report["current_version"]
    return evidence


def evidence_path(project_dir: Path | str, stage: str, *, episode: str = "ep01") -> Path:
    return Path(project_dir) / episode / "artifacts" / f"{EVIDENCE_PREFIX}{stage}.json"


def write(project_dir: Path | str, stage: str, evidence: dict[str, Any],
          *, episode: str = "ep01") -> Path:
    """Atomically persist the evidence file. Never writes a partial file."""
    target = evidence_path(project_dir, stage, episode=episode)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        # risk:swallowed-exception reason:"清理临时文件后原样抛出，不吞掉写入失败本身；临时文件残留会污染证据目录" owner:@kevinwanghd reviewed:2026-11-21
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target