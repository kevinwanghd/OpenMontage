"""Deterministic QA gate for the short-drama pipeline.

This is the code half of the automatic review: objective, repeatable checks
that run before a stage is accepted (and after generation) with no human in
the loop. The subjective/visual half is the ``shortdrama-reviewer`` agent,
which consumes the same artifacts and returns a structured verdict.

Checks are deliberately conservative: a finding is either blocking (the clip
cannot ship) or a warning (worth fixing, does not stop the pipeline).
"""
from __future__ import annotations

# risk:untested reason:"QA 门禁由 tests/contracts/test_shortdrama_qa.py 覆盖；thin 静态门禁无法关联测试映射" owner:@kevinwanghd reviewed:2026-11-14

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from shortdrama import continuity

SLOT_CLIPS = "shot_gen_video_primary_{shot_id}.mp4"
DIALOGUE_AUDIO = "shot_gen_tts_voice_{shot_id}.mp3"
SUBTITLE_FILE = "assembly_subtitle_subtitle_default.srt"
DURATION_TOLERANCE_S = 1.5
MAX_CUE_SECONDS = 3.0  # speech + 0.4s tail + guard band


def _ffprobe(path: Path) -> Optional[dict[str, Any]]:
    """width/height/duration/audio for a local media file (None when absent)."""
    if not path.is_file():
        return None
    import subprocess

    # risk:swallowed-exception reason:"媒体探测失败返回 None，由 QA 判为缺失并阻断，不会静默放行" owner:@kevinwanghd reviewed:2026-11-14
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        data = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        duration = 0.0
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "duration": duration,
        "has_audio": has_audio,
    }


def review_media(
    assets_dir: Path,
    scene_plan: dict[str, Any],
    *,
    stage: str = "shot_gen",
    probe: Callable[[Path], Optional[dict[str, Any]]] = _ffprobe,
) -> dict[str, Any]:
    """Check generated clips: portrait, duration, dialogue audio."""
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for shot in scene_plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("shot_id"))
        clip = assets_dir / SLOT_CLIPS.format(shot_id=sid)
        info = probe(clip)
        if info is None:
            if stage == "shot_gen":
                blocking.append({
                    "shot": sid, "issue": "clip missing",
                    "evidence": clip.name, "fix": "generate the shot",
                })
            continue
        width, height = info.get("width") or 0, info.get("height") or 0
        if not width or not height or height <= width:
            blocking.append({
                "shot": sid, "issue": "not vertical 9:16",
                "evidence": f"{width}x{height}", "fix": "regenerate with ratio 9:16",
            })
        declared = float(shot.get("duration_s") or 0)
        if declared and abs(float(info.get("duration") or 0) - declared) > DURATION_TOLERANCE_S:
            warnings.append({
                "shot": sid, "issue": "duration differs from scene plan",
                "evidence": f"{info.get('duration'):.2f}s vs {declared}s",
                "fix": "trim or regenerate to the declared duration",
            })
        dialogue = shot.get("dialogue")
        if isinstance(dialogue, dict) and (dialogue.get("text") or "").strip():
            if not info.get("has_audio"):
                blocking.append({
                    "shot": sid, "issue": "dialogue shot has no audio (no lipsync)",
                    "evidence": "ffprobe: no audio stream",
                    "fix": "regenerate with audio_path",
                })
    return {"blocking": blocking, "warnings": warnings}


def _parse_srt(text: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
    )

    def secs(stamp: str) -> float:
        h, m, rest = stamp.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    for match in pattern.finditer(text):
        cues.append({"start": secs(match.group(1)), "end": secs(match.group(2))})
    return cues


def review_subtitles(path: Path) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    if not path.is_file():
        return {"blocking": [], "warnings": [
            {"issue": "subtitle file missing", "evidence": path.name,
             "fix": "run the subtitle slot"}]}
    cues = _parse_srt(path.read_text(encoding="utf-8"))
    if not cues:
        return {"blocking": [], "warnings": [
            {"issue": "subtitle file has no cues", "evidence": path.name, "fix": "rebuild cues"}]}
    for index, cue in enumerate(cues, start=1):
        length = cue["end"] - cue["start"]
        if length > MAX_CUE_SECONDS:
            warnings.append({
                "cue": index, "issue": "cue outlives its speech",
                "evidence": f"{length:.2f}s", "fix": "cap the cue to the audio length",
            })
        if length <= 0:
            warnings.append({"cue": index, "issue": "non-positive cue duration",
                             "evidence": f"{length:.2f}s", "fix": "fix timing"})
    return {"blocking": [], "warnings": warnings}


def review(
    project_dir: Path,
    stage: str = "shot_gen",
    *,
    episode: str = "ep01",
    probe: Callable[[Path], Optional[dict[str, Any]]] = _ffprobe,
) -> dict[str, Any]:
    """Full automatic review for one stage. ``passed`` is machine-decided."""
    project_dir = Path(project_dir)
    artifacts_dir = project_dir / "artifacts"
    scene_plan = _load(artifacts_dir / "scene_plan.json")
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if scene_plan is None:
        issues.append({"issue": "scene_plan missing", "fix": "produce the artifact"})
    else:
        known = set()
        design = _load(artifacts_dir / "character_design.json")
        if design:
            known = {str(c.get("id")) for c in design.get("characters") or [] if isinstance(c, dict)}
        for err in continuity.validate_scene_plan(scene_plan, known):
            issues.append({"issue": "continuity", "evidence": err,
                           "fix": "register the reference before generating"})
        # GATE-05: a declared generation block must fit the model and its beats
        # must consume it exactly. Plans that declare neither are legacy (the
        # skills require the fields for new plans), so they only warn.
        if stage in ("scene_plan", "shot_gen"):
            from lib import generation_blocks

            for finding in generation_blocks.validate_scene_plan(scene_plan):
                issues.append({"issue": "generation block",
                               "evidence": f"scene {finding.get('scene')}: "
                                           f"{finding.get('issue')}",
                               "fix": finding.get("fix")})
            scenes = [s for s in scene_plan.get("scenes") or [] if isinstance(s, dict)]
            legacy = [s for s in scenes
                      if s.get("generation_block_seconds") is None
                      and s.get("edit_beats") is None]
            if legacy:
                warnings.append({
                    "issue": "legacy scene(s) declare no generation block",
                    "evidence": ", ".join(str(s.get("id")) for s in legacy),
                    "fix": "new plans must declare generation_block_seconds and edit_beats",
                })
        assets_dir = project_dir / episode / "assets"
        media = review_media(assets_dir, scene_plan, stage=stage, probe=probe)
        issues += media["blocking"]
        warnings += media["warnings"]
        if stage in ("assembly", "delivery"):
            sub = review_subtitles(assets_dir / SUBTITLE_FILE)
            issues += sub["blocking"]
            warnings += sub["warnings"]
        # GATE-04: a shot may only be accepted with evidence a reviewer can
        # actually look at. Opt-in via the scene plan so in-flight projects
        # whose packs predate this rule keep resuming.
        if stage == "shot_gen" and scene_plan.get("evidence_pack_required"):
            from lib import evidence_pack

            for shot in scene_plan.get("shots") or []:
                if not isinstance(shot, dict) or not shot.get("dialogue"):
                    continue
                pack = evidence_pack.validate(
                    project_dir, str(shot.get("shot_id")), episode=episode)
                issues += pack["blocking"]
                warnings += pack["warnings"]

    # Visual judgement from the reviewer agent, persisted as an artifact so the
    # gate is repeatable without a human re-running the review by hand.
    visual = review_visual(project_dir)
    issues += visual["blocking"]
    warnings += visual["warnings"]

    issues = _tag(issues, "P0")
    warnings = _tag(warnings, "P1")
    score = max(0, 100 - 20 * len(issues) - 5 * len(warnings))
    return {
        "stage": stage,
        "passed": not issues,
        "score": score,
        "blocking": issues,
        "warnings": warnings,
    }


VISUAL_VERDICT_FILE = "qa_visual.json"


def review_visual(project_dir: Path) -> dict[str, Any]:
    """Consume the reviewer agent's persisted verdict, if present.

    The agent writes ``artifacts/qa_visual.json`` with the same blocking /
    warnings schema. Absent file means the visual review has not run yet; that
    is reported as a warning (not silently passed) so a missing review cannot
    be mistaken for a clean one.
    """
    data = _load(Path(project_dir) / "artifacts" / VISUAL_VERDICT_FILE)
    if data is None:
        return {"blocking": [], "warnings": [
            {"issue": "visual review not run", "evidence": VISUAL_VERDICT_FILE,
             "fix": "run the shortdrama-reviewer agent and persist its verdict"}]}
    blocking = _tag(data.get("blocking"), "P0")
    warnings = _tag(data.get("warnings"), "P1")
    if data.get("verdict") == "reject" and not blocking:
        blocking = [{"issue": "reviewer rejected without detail",
                     "evidence": "verdict=reject", "fix": "inspect reviewer notes",
                     "severity": "P0"}]
    return {"blocking": blocking, "warnings": warnings}


def _tag(entries: Any, severity: str) -> list[dict[str, Any]]:
    """Stamp a severity on each finding so the checkpoint gate can act on it.

    Without an explicit severity the gate can only count findings, which is how
    a P0 (mouth never moves, wrong voice) and a P1 (subtitle 40px too low) end
    up with the same weight.
    """
    tagged: list[dict[str, Any]] = []
    for entry in entries or []:
        item = dict(entry) if isinstance(entry, dict) else {"issue": str(entry)}
        item.setdefault("severity", severity)
        tagged.append(item)
    return tagged


def _load(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    # risk:swallowed-exception reason:"artifact 损坏返回 None，由调用方按缺失处理并阻断" owner:@kevinwanghd reviewed:2026-11-14
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None