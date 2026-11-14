"""Pre-spend validation of work items against the model's declared contract.

The driver already refuses to run without artifacts, approved checkpoints and
continuity. What it never checked is whether the *parameters* each work item
carries are legal for the resolved model: an unsupported operation, a duration
outside the model's range, a resolution tier the model does not offer, an
aspect ratio it cannot render, or a dialogue track handed to a model that has
no audio input. Every one of those turns into a provider 400 *after* the call
has been reserved, or -- worse -- into a silent clip nobody notices until it
is watched.

These checks are pure local lookups against ``bindings/catalog/*.yaml`` and the
filesystem. They never call a provider and never modify an artifact.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from lib import capability_slots as slots

#: The pipeline speaks ``first_frame``; the catalog declares ``image_to_video``.
_OPERATION_ALIASES = {
    "first_frame": "image_to_video",
    "first_last_frame_to_video": "image_to_video",
}


def normalize_operation(value: Any) -> str:
    operation = str(value or "text_to_video")
    return _OPERATION_ALIASES.get(operation, operation)


def _resolution(value: Any, default: str) -> str:
    text = str(value or default or "720p").strip().lower()
    return text


def _entry_for(
    catalog_model: Optional[str], model: Optional[str], operation: str
) -> Optional[dict[str, Any]]:
    """Resolve the catalog contract for a binding's model + operation."""
    if catalog_model:
        return slots.catalog_entry(catalog_model, operation)
    if model and "/" in str(model):
        return slots.catalog_entry(str(model), operation)
    return None


def _non_empty_file(path_value: Any, base_dir: Optional[Path] = None) -> bool:
    """True when the path points at a non-empty file.

    Relative artifact paths may be recorded from the repo root (the pipeline's
    usual CWD) or relative to the episode directory, so both are tried before
    declaring the file missing.
    """
    try:
        path = Path(str(path_value))
    except (TypeError, ValueError):
        return False
    candidates = [path]
    if not path.is_absolute() and base_dir is not None:
        candidates.append(Path(base_dir) / path)
    return any(
        candidate.is_file() and candidate.stat().st_size > 0
        for candidate in candidates
    )


def validate_items(
    items: Iterable[Any],
    *,
    catalog_model: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    fallback_models: Iterable[str] = (),
    resolution: str = "720p",
    base_dir: Optional[Path] = None,
) -> list[str]:
    """Return every contract violation across ``items`` (empty means clean).

    A binding without a ``catalog_model`` declares no contract; the checks are
    skipped so local/custom tools keep working. A declared catalog id that is
    missing from the catalog is itself an error: the binding is misconfigured
    and nobody can say what the model supports.
    """
    errors: list[str] = []
    items = list(items)

    for item in items:
        inputs = getattr(item, "inputs", {}) or {}
        operation = normalize_operation(inputs.get("operation"))
        entry = _entry_for(catalog_model, model, operation)
        label = f"shot '{getattr(item, 'item_id', '?')}'"

        if catalog_model and entry is None:
            errors.append(
                f"{label}: catalog has no entry for {catalog_model!r} "
                f"operation {operation!r}; declare the model's contract before spending"
            )
            continue

        prompt = inputs.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{label}: prompt is empty")

        if entry is None:
            # No declared contract; still make sure the referenced files exist.
            errors.extend(_file_errors(inputs, label, base_dir))
            continue

        errors.extend(_parameter_errors(inputs, entry, resolution, label))
        errors.extend(_file_errors(inputs, label, base_dir))
        errors.extend(_lipsync_errors(inputs, entry, label))

        for fallback in fallback_models or ():
            fallback_entry = _fallback_entry(fallback, provider, operation)
            if fallback_entry is None:
                continue
            if inputs.get("audio_path") and not _supports_dialogue(fallback_entry):
                errors.append(
                    f"{label}: fallback model {fallback!r} cannot drive lipsync; "
                    "rotating to it would produce a silent clip"
                )

    return errors


def _parameter_errors(
    inputs: dict[str, Any], entry: dict[str, Any], default_resolution: str, label: str
) -> list[str]:
    errors: list[str] = []

    supported_resolutions = [
        str(value).lower() for value in entry.get("output_resolutions") or []
    ]
    resolution = _resolution(inputs.get("resolution"), default_resolution)
    if supported_resolutions and resolution not in supported_resolutions:
        errors.append(
            f"{label}: resolution {resolution!r} is not supported by "
            f"{entry.get('model')!r} (supported: {', '.join(supported_resolutions)})"
        )

    duration = inputs.get("duration_seconds")
    if duration is not None:
        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            errors.append(f"{label}: duration_seconds {duration!r} is not a number")
        else:
            minimum = entry.get("min_duration_s")
            maximum = entry.get("max_duration_s")
            if minimum is not None and seconds < float(minimum):
                errors.append(
                    f"{label}: duration {seconds:g}s is below the model minimum "
                    f"{float(minimum):g}s; the call would be rejected"
                )
            if maximum is not None and seconds > float(maximum):
                errors.append(
                    f"{label}: duration {seconds:g}s exceeds the model maximum "
                    f"{float(maximum):g}s; split the shot instead"
                )

    ratio = inputs.get("ratio")
    ratios = entry.get("aspect_ratios") or []
    if ratio and ratios and str(ratio) not in [str(value) for value in ratios]:
        errors.append(
            f"{label}: ratio {ratio!r} is not supported by {entry.get('model')!r} "
            f"(supported: {', '.join(str(value) for value in ratios)})"
        )

    if entry.get("operation") == "image_to_video":
        has_image = inputs.get("reference_image_path") or inputs.get("reference_image_url")
        if not has_image:
            errors.append(
                f"{label}: image_to_video needs a reference image, but the item has none"
            )

    return errors


def _file_errors(
    inputs: dict[str, Any], label: str, base_dir: Optional[Path] = None
) -> list[str]:
    errors: list[str] = []
    for key in ("reference_image_path", "audio_path"):
        value = inputs.get(key)
        if value and not _non_empty_file(value, base_dir):
            errors.append(f"{label}: {key} {value!r} is missing or empty")
    return errors


def _supports_dialogue(entry: dict[str, Any]) -> bool:
    return bool(entry.get("lipsync_from_dialogue") or entry.get("reference_audio"))


def _lipsync_errors(
    inputs: dict[str, Any], entry: dict[str, Any], label: str
) -> list[str]:
    if not inputs.get("audio_path"):
        return []
    if _supports_dialogue(entry):
        return []
    return [
        f"{label}: the shot has a dialogue track but {entry.get('model')!r} has no "
        "audio-driven lipsync; the clip would ship silent"
    ]


def _fallback_entry(
    fallback: str, provider: Optional[str], operation: str
) -> Optional[dict[str, Any]]:
    for candidate in (fallback, f"{provider}/{fallback}" if provider else None):
        if not candidate:
            continue
        entry = slots.catalog_entry(candidate, operation)
        if entry is not None:
            return entry
    return None


# ---------------------------------------------------------------------------
# Keyframe visual review gate (Layer 2)
# ---------------------------------------------------------------------------

VISUAL_VERDICT_FILE = "qa_visual.json"


def visual_review_errors(artifacts_dir: Path) -> list[str]:
    """Block shot_gen until the reviewer agent has passed the keyframes.

    The reviewer runs *before* video generation so a duplicate prop or an
    extra hand is fixed on a still image instead of being inherited by every
    generated clip. A missing verdict is an error, not a pass: "not reviewed"
    and "reviewed clean" must never look the same.
    """
    path = Path(artifacts_dir) / VISUAL_VERDICT_FILE
    if not path.is_file():
        return [
            f"visual review not run ({path.name} missing); run the "
            "shortdrama-reviewer agent on the keyframes before generating video"
        ]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"visual review verdict is unreadable: {exc}"]
    if not isinstance(data, dict):
        return [f"visual review verdict must be a JSON object, got {type(data).__name__}"]

    blocking = data.get("blocking") or []
    verdict = data.get("verdict")
    if blocking:
        detail = "; ".join(
            item.get("issue", str(item)) if isinstance(item, dict) else str(item)
            for item in blocking
        )
        return [f"keyframe visual review rejected {len(blocking)} shot(s): {detail}"]
    if verdict != "pass":
        return [
            f"keyframe visual review verdict is {verdict!r}, not 'pass'; "
            "resolve the reviewer's findings before generating video"
        ]
    return []