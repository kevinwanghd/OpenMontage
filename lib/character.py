"""Short-drama character identity layer (CHAR-01/04/05/06/07/08/09).

The character is the *first-class entity* that anchors cross-shot identity
(CHAR-01). Cards live in :class:`lib.asset_library.AssetLibrary` and validate
against ``schemas/artifacts/drama_character.schema.json``; face vectors live in
the library's independent, embedder-keyed identity store -- never inline in the
card and never mixed into the corpus visual space.

Deliberate M0 separations:

- **Detection != identity** (CHAR-06). ``tools/analysis/face_tracker.py`` only
  detects/tracks and reports ONE face per frame; it has no embedding output.
  This module consumes its payloads for coverage and treats the identity
  matcher as an injected callable. No matcher -> ``UNKNOWN`` (never invented).
  Multiple faces / occluded-or-missing / unavailable tool -> ``UNKNOWN`` human
  review.
- **Coverage** (CHAR-06) is exactly *the fraction of sampled frames in the
  applicable shots with >= 1 detection*, not a per-face ratio.
- **Thresholds** (CHAR-07) are calibrated per binding/embedder (card override
  or per-binding config, persisted to disk). There is no global constant; an
  uncalibrated pair refuses to answer instead of falling back.
- **Reference passing** (CHAR-04/05) is verifiable parameters, never prompt
  text. A binding whose capability catalog says it cannot take reference
  images/first frames is blocked unless an explicitly approved downgrade is
  supplied.

M0: local files only, zero external API calls, no image generation.
"""
from __future__ import annotations

import copy
import functools
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from jsonschema import Draft202012Validator

from lib import capability_slots
from lib.asset_library import IDENTITY_STORE_VERSION, AssetLibrary, _model_slug

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = _REPO_ROOT / "schemas" / "artifacts" / "drama_character.schema.json"

CHARACTER_KIND = "image"  # AssetLibrary restricts kinds to {"image", "video"}
ROLE_CARD = "character_card"
ROLE_WARDROBE = "wardrobe"
ROLE_HISTORICAL_SHOT = "historical_shot"

UNKNOWN = "UNKNOWN"
MATCH = "MATCH"
MISMATCH = "MISMATCH"

#: Minimum detection coverage for identity evidence to be usable. 0.5 is an
#: M0 engineering floor (human review still confirms every P0 shot); it is a
#: parameter, not a silent substitute for a face-similarity threshold.
DEFAULT_MIN_COVERAGE = 0.5

#: Built-in *calibration defaults*, keyed per (binding, embedder). These are
#: starting points from M0 mock calibration only; M1 overwrites them via
#: :meth:`FaceThresholdConfig.calibrate`. Keying by pair is the point: switching
#: binding switches the threshold instead of using one global constant.
_BINDING_THRESHOLDS: dict[tuple[str, str], float] = {
    ("seedance/doubao-seedance-2-0", "facenet512"): 0.78,
    ("seedance/doubao-seedance-2-0", "arcface"): 0.70,
    ("holopix/seedance-2-0", "facenet512"): 0.74,
    ("holopix/seedance-2-0", "arcface"): 0.66,
    ("kling/kling-2-5", "facenet512"): 0.72,
    ("kling/kling-2-5", "arcface"): 0.64,
    ("google/veo-3", "facenet512"): 0.76,
    ("google/veo-3", "arcface"): 0.68,
}

#: Per-embedder calibration fallback when no binding-specific pair exists.
#: Still not a global constant: a different embedder means a different number.
_EMBEDDER_THRESHOLDS: dict[str, float] = {
    "facenet512": 0.75,
    "arcface": 0.68,
    "insightface": 0.70,
    "mock-embed-v1": 0.75,
    "local-hash-v1": 0.80,
}

_USAGE_LOCK = threading.Lock()


class CharacterError(Exception):
    """Base error for the character identity layer."""


class CharacterNotFoundError(CharacterError, KeyError):
    """character_id is not registered as a character card."""


class InvalidCharacterError(CharacterError):
    """Card / wardrobe / request shape violates the constract."""


class ReferenceUnavailableError(CharacterError):
    """No accessible wardrobe or approved historical-shot reference exists."""


class ReferencePassingBlockedError(CharacterError):
    """The binding cannot carry reference images/first frame via parameters."""


class UnknownThresholdError(CharacterError):
    """No calibration exists for this binding/embedder pair."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@functools.lru_cache(maxsize=4)
def _validator(schema_path: Path) -> Draft202012Validator:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def validate_card(card: dict, *, schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict:
    """Validate a character card against the frozen drama_character schema."""
    errors = sorted(_validator(Path(schema_path)).iter_errors(card),
                    key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise InvalidCharacterError(f"invalid character card at {where}: {first.message}")
    return card


def _safe_relpath(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidCharacterError(f"{field} must be a non-empty path string")
    return raw


def _resolve_accessible(raw: str, base_dir: Optional[Path]) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(base_dir or Path.cwd()) / path
    return path


def _normalise_wardrobe(wardrobe: Optional[Iterable[dict]]) -> list[dict]:
    items: list[dict] = []
    for idx, item in enumerate(wardrobe or []):
        if not isinstance(item, dict):
            raise InvalidCharacterError(f"wardrobe[{idx}] must be an object")
        unknown = set(item) - {"path", "angle", "description"}
        if unknown:
            raise InvalidCharacterError(
                f"wardrobe[{idx}] has unsupported keys: {sorted(unknown)}")
        entry: dict[str, str] = {"path": _safe_relpath(item.get("path"), f"wardrobe[{idx}].path")}
        for key in ("angle", "description"):
            if key in item and item[key] is not None:
                if not isinstance(item[key], str):
                    raise InvalidCharacterError(f"wardrobe[{idx}].{key} must be a string")
                entry[key] = item[key]
        items.append(entry)
    return items


# ---------------------------------------------------------------------------
# CHAR-07: per-binding / per-embedder threshold calibration
# ---------------------------------------------------------------------------


class FaceThresholdConfig:
    """Persisted calibration table keyed by (binding, embedder_model).

    Resolution order for a call is: explicit calibration written through
    :meth:`calibrate` -> built-in (binding, embedder) default -> built-in
    per-embedder default -> :class:`UnknownThresholdError`. A character card
    may additionally override a single embedder (handled by the registry), so
    thresholds never collapse into one global number.
    """

    VERSION = "1.0"

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return list(data.get("calibrations") or [])

    def calibrate(
        self,
        binding: str,
        embedder_model: str,
        threshold: float,
        *,
        evidence: str = "",
        calibrated_at: Optional[str] = None,
    ) -> dict:
        if not binding or not embedder_model:
            raise UnknownThresholdError("binding and embedder_model are required")
        if not 0.0 <= float(threshold) <= 1.0:
            raise InvalidCharacterError("threshold must be within [0, 1]")
        records = self._load()
        now = calibrated_at or _now()
        updated = None
        for record in records:
            if (record.get("binding") == binding
                    and record.get("embedder_model") == embedder_model):
                record.update({"threshold": float(threshold), "evidence": evidence,
                               "calibrated_at": now})
                updated = record
                break
        if updated is None:
            updated = {"binding": binding, "embedder_model": embedder_model,
                       "threshold": float(threshold), "evidence": evidence,
                       "calibrated_at": now}
            records.append(updated)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": self.VERSION, "calibrations": records},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return copy.deepcopy(updated)

    def resolve(
        self,
        binding: Optional[str],
        embedder_model: Optional[str],
    ) -> dict:
        """Return ``{threshold, source, binding, embedder_model}`` or raise."""
        if not embedder_model:
            raise UnknownThresholdError(
                "embedder_model is required to resolve a face threshold")
        for record in self._load():
            if (record.get("binding") == binding
                    and record.get("embedder_model") == embedder_model):
                return {"threshold": float(record["threshold"]), "source": "calibrated",
                        "binding": binding, "embedder_model": embedder_model}
        if binding is not None:
            pair = _BINDING_THRESHOLDS.get((binding, embedder_model))
            if pair is not None:
                return {"threshold": float(pair), "source": "binding",
                        "binding": binding, "embedder_model": embedder_model}
        else:
            # With no binding requested, only one calibrated pair per embedder
            # can be used; ambiguity would silently pick a binding.
            matches = [v for (b, e), v in _BINDING_THRESHOLDS.items()
                       if e == embedder_model]
            if len(set(matches)) == 1:
                return {"threshold": float(matches[0]), "source": "binding",
                        "binding": None, "embedder_model": embedder_model}
        embedder_default = _EMBEDDER_THRESHOLDS.get(embedder_model)
        if embedder_default is not None:
            return {"threshold": float(embedder_default), "source": "embedder",
                    "binding": binding, "embedder_model": embedder_model}
        raise UnknownThresholdError(
            f"no face-similarity calibration for binding={binding!r} "
            f"embedder_model={embedder_model!r}")


# ---------------------------------------------------------------------------
# CHAR-06: detection coverage + identity comparison (kept separate)
# ---------------------------------------------------------------------------


@dataclass
class FrameDetection:
    """One sampled frame's detection result.

    ``face_count`` distinguishes "no face" (0) from "one face" (1) and from
    "more than one face" (>1). The shipped face_tracker collapses every frame
    to its single best detection, so callers must supply ``face_count`` from a
    detector that reports it (the M0 two-face fixture) rather than inferring
    multi-face from a bbox.
    """

    frame_index: int
    face_count: int = 0
    bbox: Optional[dict] = None
    confidence: float = 0.0


def _frames_from_tracking(tracking: dict) -> tuple[int, list[FrameDetection]]:
    """Normalise face_tracker output or an explicit frame fixture."""
    if not isinstance(tracking, dict):
        raise InvalidCharacterError("tracking payload must be an object")
    if "frames" in tracking:
        raw_frames = tracking["frames"] or []
        frames = []
        for idx, frame in enumerate(raw_frames):
            if not isinstance(frame, dict):
                raise InvalidCharacterError(f"frames[{idx}] must be an object")
            face_count = int(frame.get("face_count", 1 if frame.get("bbox") else 0))
            frames.append(FrameDetection(
                frame_index=int(frame.get("frame_index", idx)),
                face_count=face_count,
                bbox=frame.get("bbox"),
                confidence=float(frame.get("confidence") or 0.0),
            ))
        return len(frames), frames

    # Native face_tracker payload: ``frame_count`` sampled, ``faces`` is at
    # most one entry per frame (single-face contract).
    sampled = int(tracking.get("frame_count", 0))
    detected_indices = {int(face.get("frame_index", -1))
                        for face in (tracking.get("faces") or [])
                        if isinstance(face, dict)}
    detected_indices.discard(-1)
    frames = [FrameDetection(frame_index=idx, face_count=1 if idx in detected_indices else 0)
              for idx in range(sampled)]
    return sampled, frames


def coverage_from_trackings(trackings: Sequence[dict]) -> dict:
    """CHAR-06 coverage = detected sampled frames / sampled frames.

    Only applicable shots are counted (``applicable: false`` payloads are
    skipped). Multi-face frames are surfaced separately: the detector cannot
    say which face is the character, so the caller must route to UNKNOWN.
    """
    sampled = 0
    detected = 0
    multi_face: list[dict] = []
    for shot_index, tracking in enumerate(trackings or []):
        if isinstance(tracking, dict) and tracking.get("applicable") is False:
            continue
        shot_id = tracking.get("shot_id") if isinstance(tracking, dict) else None
        count, frames = _frames_from_tracking(tracking)
        sampled += count
        for frame in frames:
            if frame.face_count >= 1:
                detected += 1
            if frame.face_count > 1:
                multi_face.append({"shot_id": shot_id, "shot_index": shot_index,
                                   "frame_index": frame.frame_index,
                                   "face_count": frame.face_count})
    coverage = (detected / sampled) if sampled else None
    if multi_face:
        status = "MULTI_FACE"
    elif sampled == 0:
        status = "NO_APPLICABLE_FRAMES"
    elif detected == 0:
        status = "NO_DETECTIONS"
    else:
        status = "OK"
    return {
        "sampled_frames": sampled,
        "detected_frames": detected,
        "coverage": coverage,
        "multi_face_frames": multi_face,
        "status": status,
    }


def _score_from_matcher(result: Any) -> tuple[float, Optional[str]]:
    if isinstance(result, dict):
        if "score" not in result:
            raise InvalidCharacterError("identity matcher result needs a 'score'")
        return float(result["score"]), result.get("model_version")
    return float(result), None


def _threshold_from_card(card: dict, embedder_model: str) -> Optional[dict]:
    metadata = card.get("metadata") or {}
    overrides = metadata.get("face_thresholds") or {}
    if isinstance(overrides, dict) and embedder_model in overrides:
        return {"threshold": float(overrides[embedder_model]),
                "source": "character_card", "embedder_model": embedder_model}
    return None


def assess_character_identity(
    card: dict,
    trackings: Sequence[dict],
    *,
    identity_matcher: Optional[Callable[[dict, list[dict]], Any]] = None,
    embedder_model: Optional[str] = None,
    binding: Optional[str] = None,
    thresholds: Optional[FaceThresholdConfig] = None,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> dict:
    """Assess one character over its applicable shots.

    Detection (face_tracker payloads) and identity comparison
    (``identity_matcher``) are separate inputs by construction. Any condition
    the M0 detector cannot resolve yields ``UNKNOWN`` with a machine reason and
    the matcher is *not* called: no score is fabricated.
    """
    result = {
        "character_id": card.get("character_id"),
        "status": UNKNOWN,
        "reason": None,
        "score": None,
        "threshold": None,
        "threshold_source": None,
        "model_version": None,
        "detector": "face_tracker",
        "embedder_model": embedder_model,
        "binding": binding,
        "min_coverage": float(min_coverage),
    }
    coverage = coverage_from_trackings(trackings)
    result.update({key: value for key, value in coverage.items() if key != "status"})
    result["coverage_status"] = coverage["status"]
    result["coverage_ok"] = (
        coverage["coverage"] is not None
        and coverage["coverage"] >= float(min_coverage)
        and not coverage["multi_face_frames"])

    if result["sampled_frames"] == 0:
        result["reason"] = "no_applicable_frames"
        return result
    if result["multi_face_frames"]:
        result["reason"] = "multiple_faces"
        return result
    if result["detected_frames"] == 0:
        result["reason"] = "no_face_detected"
        return result
    if result["coverage"] < float(min_coverage):
        result["reason"] = "low_coverage"
        return result
    if identity_matcher is None:
        result["reason"] = "identity_tool_unavailable"
        return result

    try:
        if card and card.get("metadata"):
            resolved = _threshold_from_card(card, embedder_model) if embedder_model else None
        else:
            resolved = None
        if resolved is None:
            if thresholds is None:
                result["reason"] = "threshold_config_required"
                return result
            resolved = thresholds.resolve(binding, embedder_model)
    except UnknownThresholdError:
        result["reason"] = "threshold_unconfigured"
        return result

    detections = [{"frame_index": f.frame_index, "bbox": f.bbox,
                   "confidence": f.confidence}
                  for _, frames in ((t.get("shot_id"), _frames_from_tracking(t)[1])
                                    for t in (trackings or [])
                                    if not (isinstance(t, dict) and t.get("applicable") is False))
                  for f in frames if f.face_count >= 1]
    score, model_version = _score_from_matcher(identity_matcher(card, detections))
    result.update({
        "score": score,
        "threshold": resolved["threshold"],
        "threshold_source": resolved.get("source"),
        "model_version": model_version,
    })
    if score >= resolved["threshold"]:
        result["status"] = MATCH
    else:
        result["status"] = MISMATCH
        result["reason"] = "below_threshold"
    return result


# ---------------------------------------------------------------------------
# CHAR-04 / CHAR-05: verifiable reference passing (parameters, not prompts)
# ---------------------------------------------------------------------------


def _reference_paths(
    raw_paths: Sequence[str],
    base_dir: Optional[Path],
    *,
    require_accessible: bool = True,
) -> list[str]:
    resolved: list[str] = []
    for raw in raw_paths:
        path = _resolve_accessible(_safe_relpath(raw, "reference image"), base_dir)
        if require_accessible and not path.is_file():
            raise ReferenceUnavailableError(f"reference image not accessible: {path}")
        resolved.append(str(path))
    return resolved


def keyframe_reference_request(
    card: dict,
    *,
    reference_images: Optional[Sequence[str]] = None,
    base_dir: Optional[Path] = None,
    prompt: str = "",
) -> dict:
    """CHAR-04: build keyframe params that carry the character reference group.

    ``reference_images`` defaults to the card's multi-angle wardrobe. The
    images travel as request parameters; ``prompt`` is a separate field and is
    never the reference channel.
    """
    images = list(reference_images) if reference_images is not None else [
        item["path"] for item in card.get("wardrobe") or []
    ]
    if not images:
        raise ReferenceUnavailableError(
            f"character {card.get('character_id')!r} has no wardrobe reference group")
    return {
        "character_id": card.get("character_id"),
        "mode": "reference_group_edit",
        "reference_channel": "parameters",
        "reference_images": _reference_paths(images, base_dir),
        "prompt": prompt,
    }


def _binding_entry(
    binding: str,
    operation: str,
    catalog_entry_fn: Optional[Callable[[str, str], Optional[dict]]],
) -> dict:
    entry = (catalog_entry_fn or capability_slots.catalog_entry)(binding, operation)
    if not entry:
        raise InvalidCharacterError(
            f"unknown binding/operation {binding!r}/{operation!r} in the capability catalog")
    return entry


def video_binding_declaration(
    card: dict,
    binding: str,
    *,
    operation: str = "image_to_video",
    reference_images: Optional[Sequence[str]] = None,
    first_frame: Optional[str] = None,
    prompt: str = "",
    base_dir: Optional[Path] = None,
    catalog_entry_fn: Optional[Callable[[str, str], Optional[dict]]] = None,
    approved_downgrade: Optional[dict] = None,
) -> dict:
    """CHAR-05: declare reference images/first frame for the video stage.

    Support is read from the capability catalog (unknown/unavailable is not
    support). When the binding cannot carry the reference, the call blocks --
    unless the caller supplies an explicit, attributed downgrade approval, in
    which case the declaration is marked ``DEGRADED`` with identity ``UNKNOWN``.
    """
    entry = _binding_entry(binding, operation, catalog_entry_fn)
    images = list(reference_images) if reference_images is not None else [
        item["path"] for item in card.get("wardrobe") or []
    ]
    resolved_images = _reference_paths(images, base_dir) if images else []
    resolved_first = (_reference_paths([first_frame], base_dir)[0]
                      if first_frame else None)

    max_refs = entry.get("reference_images_max")
    supports_refs = isinstance(max_refs, int) and max_refs > 0
    supports_first = entry.get("first_frame_conditioning") is True
    problems: list[str] = []
    if resolved_images and not supports_refs:
        problems.append(
            f"binding {binding!r}/{operation!r} declares reference_images_max={max_refs!r}")
    if supports_refs and len(resolved_images) > int(max_refs):
        problems.append(
            f"{len(resolved_images)} reference images exceed max {max_refs} for "
            f"{binding!r}/{operation!r}")
    if resolved_first and not supports_first:
        problems.append(
            f"binding {binding!r}/{operation!r} declares "
            "first_frame_conditioning=false")
    if not resolved_images and not resolved_first:
        problems.append("no reference image or first frame supplied")

    declaration = {
        "character_id": card.get("character_id"),
        "binding": binding,
        "operation": operation,
        "reference_channel": "parameters",
        "prompt_only_reference": False,
        "reference_images": resolved_images,
        "first_frame": resolved_first,
        "prompt": prompt,
        "capabilities": {
            "reference_images_max": max_refs,
            "first_frame_conditioning": supports_first,
        },
        "status": "OK",
        "identity_status": None,
        "downgrade": None,
    }
    if problems:
        if approved_downgrade is None:
            raise ReferencePassingBlockedError("; ".join(problems))
        _validate_downgrade(approved_downgrade)
        declaration.update({
            "status": "DEGRADED",
            "identity_status": UNKNOWN,
            "downgrade": copy.deepcopy(approved_downgrade),
            "blockers": problems,
        })
    return declaration


def _validate_downgrade(approval: dict) -> None:
    if not isinstance(approval, dict):
        raise InvalidCharacterError("approved_downgrade must be an object")
    missing = [key for key in ("approved_by", "reason", "approved_at")
               if not approval.get(key)]
    if missing:
        raise ReferencePassingBlockedError(
            f"downgrade approval is missing {missing}; unapproved fallback is forbidden")
    if len(str(approval["reason"]).strip()) < 10:
        raise ReferencePassingBlockedError(
            "downgrade reason must explain the approved fallback (>= 10 chars)")


def validate_binding_declaration(
    declaration: dict,
    *,
    catalog_entry_fn: Optional[Callable[[str, str], Optional[dict]]] = None,
    approved_downgrade: Optional[dict] = None,
    require_reference: bool = True,
) -> dict:
    """CHAR-05: verify an actual video request's reference parameters.

    Re-reads the capability catalog and the declared paths so a prompt-only
    claim of identity cannot pass. Returns a normalised verdict; raises on
    unsupported / prompt-only / unapproved degradation.
    """
    if not isinstance(declaration, dict):
        raise InvalidCharacterError("binding declaration must be an object")
    binding = declaration.get("binding")
    operation = declaration.get("operation") or "image_to_video"
    if not binding:
        raise InvalidCharacterError("binding declaration requires 'binding'")
    entry = _binding_entry(binding, operation, catalog_entry_fn)

    if declaration.get("prompt_only_reference") is True:
        raise ReferencePassingBlockedError(
            f"{binding}: reference images cannot be carried by prompt text")
    if declaration.get("reference_channel", "parameters") != "parameters":
        raise ReferencePassingBlockedError(
            f"{binding}: reference_channel must be 'parameters'")

    images = list(declaration.get("reference_images") or [])
    resolved_images = _reference_paths(images, None)
    first_frame = declaration.get("first_frame")
    resolved_first = _reference_paths([first_frame], None)[0] if first_frame else None

    max_refs = entry.get("reference_images_max")
    supports_refs = isinstance(max_refs, int) and max_refs > 0
    supports_first = entry.get("first_frame_conditioning") is True
    problems: list[str] = []
    if resolved_images and not supports_refs:
        problems.append(f"{binding}/{operation}: reference_images_max={max_refs!r}")
    if supports_refs and len(resolved_images) > int(max_refs):
        problems.append(
            f"{len(resolved_images)} reference images exceed max {max_refs}")
    if resolved_first and not supports_first:
        problems.append(f"{binding}/{operation}: first_frame_conditioning=false")
    if require_reference and not resolved_images and not resolved_first:
        problems.append("declaration carries neither reference images nor a first frame")

    if problems:
        if approved_downgrade is None:
            raise ReferencePassingBlockedError("; ".join(problems))
        _validate_downgrade(approved_downgrade)
        return {
            "status": "DEGRADED",
            "identity_status": UNKNOWN,
            "binding": binding,
            "operation": operation,
            "reference_images": resolved_images,
            "first_frame": resolved_first,
            "blockers": problems,
            "downgrade": copy.deepcopy(approved_downgrade),
        }
    return {
        "status": "OK",
        "identity_status": None,
        "binding": binding,
        "operation": operation,
        "reference_images": resolved_images,
        "first_frame": resolved_first,
        "blockers": [],
        "downgrade": None,
    }


# ---------------------------------------------------------------------------
# CHAR-01 / 08 / 09: character registry backed by the asset library
# ---------------------------------------------------------------------------


class CharacterRegistry:
    """First-class character entities stored through :class:`AssetLibrary`.

    ``<library>/<kind>/<character_id>/asset.json`` holds the card (the source
    of truth); :class:`AssetLibrary` keys the face vector by embedder in its
    independent identity store. A small append-only usage ledger next to the
    library records cross-episode reuse (CHAR-09) without mutating asset.json.
    """

    def __init__(
        self,
        library_dir: Path,
        *,
        schema_path: Path = DEFAULT_SCHEMA_PATH,
    ):
        self.library_dir = Path(library_dir)
        self.library = AssetLibrary(self.library_dir)
        self.schema_path = Path(schema_path)
        self.thresholds = FaceThresholdConfig(
            self.library_dir / "characters" / "face_thresholds.json")
        self.usage_path = self.library_dir / "characters" / "usage.jsonl"

    # -- usage ledger (append-only sidecar) ---------------------------------

    def _usage_records(self) -> list[dict]:
        if not self.usage_path.is_file():
            return []
        records = []
        for line in self.usage_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    def used_in_episodes(self, character_id: str) -> list[str]:
        episodes: list[str] = []
        for record in self._usage_records():
            if record.get("character_id") == character_id:
                episode = record.get("episode")
                if episode is not None and episode not in episodes:
                    episodes.append(episode)
        return episodes

    # -- register / read ----------------------------------------------------

    def register(
        self,
        character_id: str,
        name: str,
        canonical_description: str,
        *,
        wardrobe: Optional[Iterable[dict]] = None,
        voice_id: Optional[str] = None,
        face_embedding: Optional[Sequence[float]] = None,
        embedder_model: Optional[str] = None,
        age_range: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Register a character card + optional identity vector (CHAR-01)."""
        if face_embedding is not None and not embedder_model:
            raise InvalidCharacterError("face_embedding requires embedder_model")

        card: dict[str, Any] = {
            "version": "1.0",
            "character_id": character_id,
            "name": name,
            "canonical_description": canonical_description,
        }
        wardrobe_items = _normalise_wardrobe(wardrobe)
        if wardrobe_items:
            card["wardrobe"] = wardrobe_items
        if voice_id:
            card["voice_id"] = voice_id
        if age_range:
            card["age_range"] = age_range
        if metadata:
            card["metadata"] = copy.deepcopy(metadata)
        if face_embedding is not None:
            # Reference only: the vector itself lives in the independent,
            # embedder-keyed identity store, never inline in the card.
            card["face_embedding_ref"] = (
                f"identity/{_model_slug(embedder_model)}/"
                f"{IDENTITY_STORE_VERSION}/embeddings.npy")
        validate_card(card, schema_path=self.schema_path)

        stored_metadata: dict[str, Any] = {
            "asset_type": "character",
            "role": ROLE_CARD,
            "character_id": character_id,
            "tags": ["character", character_id],
            "card": card,
        }
        if embedder_model:
            stored_metadata["embedder_model"] = embedder_model
        if metadata:
            for key, value in metadata.items():
                stored_metadata.setdefault(key, copy.deepcopy(value))
        self.library.register(
            CHARACTER_KIND,
            character_id,
            stored_metadata,
            embedding=face_embedding,
            embedder_model=embedder_model,
            lineage={"asset_type": "character", "derived_from": [], "used_in": []},
        )
        return self.card(character_id)

    def _record(self, character_id: str) -> dict:
        try:
            record = self.library.get(character_id)
        except KeyError as exc:
            raise CharacterNotFoundError(character_id) from exc
        metadata = record.get("metadata") or {}
        if metadata.get("asset_type") != "character":
            raise CharacterNotFoundError(
                f"{character_id!r} is registered but is not a character card")
        return record

    def card(self, character_id: str) -> dict:
        """Return the schema-valid card with cross-episode usage merged."""
        record = self._record(character_id)
        card = copy.deepcopy((record.get("metadata") or {}).get("card") or {})
        episodes = self.used_in_episodes(character_id)
        if episodes or "used_in_episodes" in card:
            card["used_in_episodes"] = episodes
        validate_card(card, schema_path=self.schema_path)
        return card

    def get(self, character_id: str) -> dict:
        """Return the full asset-library record for a character."""
        return copy.deepcopy(self._record(character_id))

    def lineage(self, character_id: str) -> dict:
        self._record(character_id)
        return self.library.lineage(character_id)

    def query(
        self,
        *,
        text: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        vector: Optional[Sequence[float]] = None,
        embedder_model: Optional[str] = None,
        min_sim: float = 0.0,
    ) -> list[dict]:
        """Query character cards by text/tags or by face vector (CHAR-01)."""
        records = self.library.query(
            CHARACTER_KIND, tags=tags, text=text, vector=vector,
            embedder_model=embedder_model, min_sim=min_sim)
        results: list[dict] = []
        for record in records:
            metadata = record.get("metadata") or {}
            if metadata.get("asset_type") != "character":
                continue
            card = copy.deepcopy(metadata.get("card") or {})
            episodes = self.used_in_episodes(card.get("character_id"))
            if episodes or "used_in_episodes" in card:
                card["used_in_episodes"] = episodes
            validate_card(card, schema_path=self.schema_path)
            item = {"character_id": card.get("character_id"),
                    "name": card.get("name"), "card": card}
            if "score" in record:
                item["score"] = record["score"]
            results.append(item)
        return results

    # -- CHAR-07 ------------------------------------------------------------

    def threshold_for(
        self,
        character_id: str,
        *,
        binding: Optional[str],
        embedder_model: Optional[str],
    ) -> dict:
        """Resolve the face threshold for this character + binding/embedder."""
        card = self.card(character_id)
        if embedder_model:
            override = _threshold_from_card(card, embedder_model)
            if override is not None:
                return override
        return self.thresholds.resolve(binding, embedder_model)

    def calibrate_threshold(
        self,
        binding: str,
        embedder_model: str,
        threshold: float,
        *,
        evidence: str = "",
    ) -> dict:
        return self.thresholds.calibrate(
            binding, embedder_model, threshold, evidence=evidence)

    # -- CHAR-08 ------------------------------------------------------------

    def register_shot_reference(
        self,
        character_id: str,
        shot_id: str,
        path: str,
        *,
        approved: bool = True,
        metadata: Optional[dict] = None,
    ) -> str:
        """Record an approved historical shot usable as a comparison reference."""
        self._record(character_id)
        asset_id = f"{character_id}__{shot_id}"
        stored = {
            "asset_type": "shot_reference",
            "role": ROLE_HISTORICAL_SHOT,
            "character_id": character_id,
            "shot_id": shot_id,
            "path": _safe_relpath(path, "shot reference path"),
            "approved": bool(approved),
            "tags": ["character_reference", character_id, shot_id],
        }
        for key, value in (metadata or {}).items():
            stored.setdefault(key, copy.deepcopy(value))
        self.library.register(CHARACTER_KIND, asset_id, stored)
        if approved:
            self.library.promote(asset_id)
        return asset_id

    def build_ref_context(
        self,
        character_id: str,
        *,
        shots: Optional[Iterable[dict]] = None,
        base_dir: Optional[Path] = None,
    ) -> dict:
        """CHAR-08: accessible wardrobe/historical references or a blocker.

        ``shots`` may add candidate historical shots
        (``{"shot_id", "path", "approved"}``); approved shots registered via
        :meth:`register_shot_reference` are always included.
        """
        card = self.card(character_id)
        wardrobe: list[dict] = []
        for item in card.get("wardrobe") or []:
            path = _resolve_accessible(item["path"], base_dir)
            wardrobe.append({**item, "resolved_path": str(path),
                             "accessible": path.is_file()})

        historical: list[dict] = []
        seen: set[str] = set()
        for shot in shots or []:
            raw = _safe_relpath(shot.get("path"), "shot path")
            path = _resolve_accessible(raw, base_dir)
            entry = {"shot_id": shot.get("shot_id"), "path": raw,
                     "resolved_path": str(path), "approved": bool(shot.get("approved")),
                     "accessible": path.is_file(), "source": "caller"}
            historical.append(entry)
            seen.add(str(path))
        for record in self.library.query(CHARACTER_KIND, character_id=character_id):
            metadata = record.get("metadata") or {}
            if metadata.get("role") != ROLE_HISTORICAL_SHOT:
                continue
            path = _resolve_accessible(metadata["path"], base_dir)
            if str(path) in seen:
                continue
            seen.add(str(path))
            historical.append({
                "shot_id": metadata.get("shot_id"),
                "path": metadata.get("path"),
                "resolved_path": str(path),
                "approved": bool(metadata.get("approved")),
                "accessible": path.is_file(),
                "source": "asset_library",
            })

        accessible_wardrobe = [w for w in wardrobe if w["accessible"]]
        approved_shots = [s for s in historical if s["accessible"] and s["approved"]]
        blockers: list[str] = []
        if not wardrobe:
            blockers.append("no_wardrobe_defined")
        elif not accessible_wardrobe:
            blockers.append("no_accessible_wardrobe")
        if not approved_shots and not accessible_wardrobe:
            blockers.append("no_reference_for_review")
        return {
            "character_id": character_id,
            "canonical_description": card.get("canonical_description"),
            "wardrobe": wardrobe,
            "historical_shots": historical,
            "first_shot_reference": accessible_wardrobe[0] if accessible_wardrobe else None,
            "accessible": bool(accessible_wardrobe or approved_shots),
            "blocked": bool(blockers),
            "blockers": blockers,
        }

    def assert_ref_context_ready(self, character_id: str, **kwargs: Any) -> dict:
        """Return ref_context, raising when review must be blocked (CHAR-08)."""
        context = self.build_ref_context(character_id, **kwargs)
        if context["blocked"]:
            raise ReferenceUnavailableError(
                f"character {context['character_id']!r} has no accessible reference: "
                f"{', '.join(context['blockers'])}")
        return context

    # -- CHAR-09 ------------------------------------------------------------

    def reuse_character(self, character_id: str, episode: str) -> dict:
        """Reuse an existing card in a new episode without regeneration.

        No wardrobe generation or image call exists on this path; the card is
        read back unchanged and the episode is appended to the usage ledger.
        """
        card = self.card(character_id)
        if not isinstance(episode, str) or not episode.strip():
            raise InvalidCharacterError("episode must be a non-empty string")
        existing = self.used_in_episodes(character_id)
        if episode not in existing:
            with _USAGE_LOCK:
                self.usage_path.parent.mkdir(parents=True, exist_ok=True)
                with self.usage_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "character_id": character_id,
                        "episode": episode,
                        "event": "reuse",
                        "at": _now(),
                    }, ensure_ascii=False) + "\n")
        reused = self.card(character_id)
        return {
            "character_id": character_id,
            "episode": episode,
            "reused": True,
            "wardrobe_generation_calls": 0,
            "card": reused,
        }