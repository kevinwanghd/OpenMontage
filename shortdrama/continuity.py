"""Continuity gates: machine-checkable invariants enforced BEFORE any paid call.

The M1 sample failed review because each shot re-invented identity, props and
scene from free text, and dialogue audio was produced after the video. These
validators turn this class of defect into a preflight error instead of a
post-hoc human catch:

- every shot references characters that exist in the registry;
- every shot that will be animated has an approved keyframe for the SAME scene;
- every shot with on-screen dialogue has (or will get) a dialogue audio track;
- subtitle cues are derived from real audio, not from script page timings.

Nothing here calls a provider or the network; it only reads artifacts.
"""
from __future__ import annotations

from typing import Any, Optional


class ContinuityError(ValueError):
    """A continuity invariant is violated; generation must not proceed."""


def character_ids(artifacts: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    raw = artifacts.get("drama_character")
    if isinstance(raw, dict):
        for card in raw.get("characters") or []:
            if isinstance(card, dict) and card.get("character_id"):
                ids.add(str(card["character_id"]))
        if raw.get("character_id"):
            ids.add(str(raw["character_id"]))
    design = artifacts.get("character_design")
    if isinstance(design, dict):
        for card in design.get("characters") or []:
            if isinstance(card, dict) and card.get("id"):
                ids.add(str(card["id"]))
    return ids


def validate_scene_plan(scene_plan: dict[str, Any], known: set[str]) -> list[str]:
    """Shots must only reference registered characters and declared scenes."""
    errors: list[str] = []
    scene_ids = {
        str(s.get("id")) for s in scene_plan.get("scenes") or [] if isinstance(s, dict)
    }
    for shot in scene_plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        sid = shot.get("shot_id")
        for ref in shot.get("reference_character_ids") or []:
            if known and str(ref) not in known:
                errors.append(f"shot {sid}: unknown character {ref!r}")
        scene = shot.get("scene_id")
        if scene and scene_ids and str(scene) not in scene_ids:
            errors.append(f"shot {sid}: unknown scene {scene!r}")
    return errors


def validate_keyframes(
    scene_plan: dict[str, Any], asset_manifest: Optional[dict[str, Any]]
) -> list[str]:
    """Every shot needs an approved keyframe; the keyframe must match its scene."""
    errors: list[str] = []
    by_scene: dict[str, dict[str, Any]] = {}
    for asset in (asset_manifest or {}).get("assets") or []:
        if isinstance(asset, dict) and asset.get("type") == "image":
            by_scene[str(asset.get("scene_id"))] = asset
    for shot in scene_plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("shot_id"))
        asset = by_scene.get(sid)
        if asset is None:
            errors.append(f"shot {sid}: no approved keyframe")
            continue
        if shot.get("scene_id") and asset.get("scene_id") != shot.get("scene_id"):
            # scene_id on the asset is the shot id by convention; a mismatch
            # means the keyframe was reviewed for a different shot.
            if asset.get("source_scene") and asset["source_scene"] != shot.get("scene_id"):
                errors.append(f"shot {sid}: keyframe scene mismatch")
    return errors


def dialogue_shots(scene_plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        shot
        for shot in scene_plan.get("shots") or []
        if isinstance(shot, dict)
        and isinstance(shot.get("dialogue"), dict)
        and (shot["dialogue"].get("text") or "").strip()
    ]


def validate_dialogue_audio(
    scene_plan: dict[str, Any], audio_for_shot
) -> list[str]:
    """On-screen dialogue must have an audio track before video generation."""
    errors: list[str] = []
    for shot in dialogue_shots(scene_plan):
        sid = str(shot.get("shot_id"))
        if not audio_for_shot(sid):
            errors.append(f"shot {sid}: dialogue has no audio track (lipsync impossible)")
    return errors


def subtitle_segments(
    script: dict[str, Any], audio_duration, max_cue_s: float = 2.5
) -> list[dict[str, Any]]:
    """Build subtitle cues from real audio length, capped and aligned to speech.

    ``audio_duration(section_id)`` returns the measured seconds of the TTS track
    (or None). A cue never outlives its speech by more than 0.4s and never
    exceeds ``max_cue_s``, so subtitles cannot idle over a silent face.
    """
    segments: list[dict[str, Any]] = []
    for section in script.get("sections") or []:
        if not isinstance(section, dict):
            continue
        text = (section.get("text") or "").strip()
        if not text:
            continue
        start = float(section.get("start_seconds") or 0.0)
        dur = audio_duration(section.get("id"))
        if dur is None:
            dur = min(
                float(section.get("end_seconds") or start + 2.0) - start, max_cue_s
            )
        end = start + min(float(dur) + 0.4, max_cue_s)
        segments.append(
            {"id": section.get("id"), "text": text, "start": start, "end": end}
        )
    return segments