"""Generation blocks vs edit beats, and tail-frame chaining (GATE-05).

The pipeline generated one clip per 2-6 second shot and cut them together. That
produces two failure modes visible in the 2026-11-17 cut: identity drifts between
neighbouring clips of the same scene, and the edit reads as a montage of
disconnected fragments rather than a continuous scene.

The fix used by the projects surveyed in the v2.7 audit is a separation that our
manifest did not express:

* **generation block** — what one model call renders (8-30s), where continuity
  is free because it is a single generation;
* **edit beats** — how that block is cut in the edit (2-8s each).

Chaining then has one rule that matters: continuing the *same scene* may start
from the previous block's real last frame; a *different scene* must start fresh,
because carrying a tail frame across a scene change propagates drift.

This module validates the plan and answers the chaining question. It does not
render anything.
"""
from __future__ import annotations

from typing import Any, Optional

DEFAULT_BLOCK_RANGE = (8.0, 30.0)
DEFAULT_BEAT_RANGE = (2.0, 8.0)


def _range(value: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return default


def validate_scene_plan(plan: dict[str, Any], *,
                        block_range: tuple[float, float] = DEFAULT_BLOCK_RANGE,
                        beat_range: tuple[float, float] = DEFAULT_BEAT_RANGE
                        ) -> list[dict[str, Any]]:
    """Return blocking findings for scenes that declare a generation block."""
    blocking: list[dict[str, Any]] = []
    for scene in plan.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("id") or "?")
        block = scene.get("generation_block_seconds")
        beats = scene.get("edit_beats")

        if block is None and beats is None:
            # Legacy scene: no block declared. Reported by the caller as a
            # warning, not a block, so existing plans keep validating.
            continue

        if block is None:
            blocking.append({
                "scene": scene_id, "issue": "edit_beats declared without generation_block_seconds",
                "severity": "P0",
                "fix": "declare the block the beats are cut from",
            })
            continue

        try:
            block_seconds = float(block)
        except (TypeError, ValueError):
            blocking.append({
                "scene": scene_id, "issue": "generation_block_seconds is not a number",
                "severity": "P0", "fix": "use seconds as a number",
            })
            continue

        low, high = block_range
        if not (low <= block_seconds <= high):
            blocking.append({
                "scene": scene_id,
                "issue": f"generation block {block_seconds}s outside [{low}, {high}]",
                "severity": "P0",
                "fix": "split into more blocks rather than shrinking the asset set",
            })

        if not isinstance(beats, list) or not beats:
            blocking.append({
                "scene": scene_id, "issue": "generation block has no edit_beats",
                "severity": "P0", "fix": "declare at least one edit beat",
            })
            continue

        beat_low, beat_high = beat_range
        total = 0.0
        for index, beat in enumerate(beats):
            value = beat.get("duration_s") if isinstance(beat, dict) else beat
            try:
                duration = float(value)
            except (TypeError, ValueError):
                blocking.append({
                    "scene": scene_id, "issue": f"edit_beats[{index}] has no numeric duration",
                    "severity": "P0", "fix": "give every beat a duration in seconds",
                })
                continue
            total += duration
            if not (beat_low <= duration <= beat_high):
                blocking.append({
                    "scene": scene_id,
                    "issue": f"edit beat {duration}s outside [{beat_low}, {beat_high}]",
                    "severity": "P0", "fix": "re-cut the beat",
                })

        if abs(total - block_seconds) > 0.01:
            blocking.append({
                "scene": scene_id,
                "issue": f"edit beats total {total:.2f}s but the block is {block_seconds:.2f}s",
                "severity": "P0",
                "fix": "beats must exactly consume their block",
            })
    return blocking


def chain_mode(previous_scene_id: Optional[str], next_scene_id: str) -> str:
    """Return ``"tail_frame"`` or ``"fresh_start"`` for the next block.

    Same scene continues from the previous block's real last frame; a scene
    change must start from new material. Carrying a tail frame across scenes is
    how a drift in one shot becomes a drift in all of them.
    """
    if previous_scene_id and previous_scene_id == next_scene_id:
        return "tail_frame"
    return "fresh_start"


def validate_chaining(sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`sequence` items: ``{scene_id, starts_from}`` in generation order."""
    blocking: list[dict[str, Any]] = []
    previous: Optional[str] = None
    for index, item in enumerate(sequence):
        scene_id = str(item.get("scene_id") or "")
        expected = chain_mode(previous, scene_id)
        declared = str(item.get("starts_from") or expected)
        if index > 0 and declared != expected:
            blocking.append({
                "index": index,
                "issue": f"block starts from {declared} but scene continuity requires {expected}",
                "severity": "P0",
                "fix": "start a new scene from fresh material, or continue within the scene",
            })
        previous = scene_id
    return blocking