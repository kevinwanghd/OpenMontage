"""Versioned vertical safe-area presets and the P0 machine check (VIS-06 / F18).

The P0 target platform is TikTok. The preset below models the two overlays
that cut into a 1080x1920 vertical frame:

- the right-side interaction rail (like / comment / share buttons), and
- the bottom caption + music strip.

Together with the top tab bar they define the content that must stay clear.
Only two things are machine-checked in P0:

1. the subtitle burn rectangles, and
2. every face bounding box that ``tools/analysis/face_tracker.py`` detected.

Everything else (logos, props, composition) stays a human check. The check is
fail-closed: an unconfirmed preset blocks publishing, because a preset that has
not been calibrated against the live UI is a guess, and a guessed safe area
would silently crop the first delivery.

Face boxes are normalised (0..1) in the face_tracker output; they are mapped
onto the delivery canvas through the preset dimensions, so a 480p draft and a
1080x1920 delivery agree on where the face is. Boxes are only accepted from a
face_tracker-shaped result -- hand-typed boxes are not a detection source.

M0: pure local geometry, zero external API calls.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

PLATFORM_TIKTOK = "tiktok"
PRESET_VERSION_V1 = "1.0"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FACE_SOURCE = "face_tracker"


class SafeAreaError(ValueError):
    """A preset, rectangle, or face-tracking input is invalid."""


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in delivery-canvas pixels."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SafeAreaError(f"rect {name} must be a number")
            if not math.isfinite(float(value)):
                raise SafeAreaError(f"rect {name} must be finite")
        if self.width <= 0 or self.height <= 0:
            raise SafeAreaError("rect width and height must be positive")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def contains(self, other: "Rect", *, tolerance: float = 0.0) -> bool:
        return (
            other.x >= self.x - tolerance
            and other.y >= self.y - tolerance
            and other.right <= self.right + tolerance
            and other.bottom <= self.bottom + tolerance
        )

    def intersects(self, other: "Rect") -> bool:
        # Edge-touching is not an overlap: the preset's bottom strip starts
        # exactly where the safe rectangle ends.
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "x": round(float(self.x), 3),
            "y": round(float(self.y), 3),
            "width": round(float(self.width), 3),
            "height": round(float(self.height), 3),
        }


def _coerce_rect(value: Rect | Mapping[str, Any] | Sequence[Any]) -> Rect:
    if isinstance(value, Rect):
        return value
    if isinstance(value, Mapping):
        try:
            return Rect(
                float(value["x"]),
                float(value["y"]),
                float(value["width"]),
                float(value["height"]),
            )
        except KeyError as exc:
            raise SafeAreaError(
                f"rect mapping is missing {exc.args[0]!r}"
            ) from None
        except (TypeError, ValueError) as exc:
            raise SafeAreaError(f"invalid rect mapping: {exc}") from None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return Rect(*(float(item) for item in value))
        except (TypeError, ValueError) as exc:
            raise SafeAreaError(f"invalid rect tuple: {exc}") from None
    raise SafeAreaError(
        f"rect must be Rect, mapping or 4-tuple, got {type(value).__name__}"
    )


@dataclass(frozen=True)
class SafeAreaPreset:
    """A calibrated, versioned safe-area definition.

    ``confirmed`` records whether the pixel geometry has been checked against
    the live target UI. ``source``/``calibrated_on``/``ui_surface`` are the
    provenance the publish gate needs; an unconfirmed preset can never pass it.
    """

    platform: str
    version: str
    canvas_width: int
    canvas_height: int
    safe_rect: Rect
    subtitle_rect: Rect
    blocked_zones: tuple[tuple[str, Rect], ...]
    confirmed: bool
    source: str
    calibrated_on: str | None
    ui_surface: str | None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.canvas_width <= 0 or self.canvas_height <= 0:
            raise SafeAreaError("canvas dimensions must be positive")
        canvas = Rect(0, 0, self.canvas_width, self.canvas_height)
        if not canvas.contains(self.safe_rect):
            raise SafeAreaError("safe_rect must lie inside the canvas")
        if not self.safe_rect.contains(self.subtitle_rect):
            raise SafeAreaError("subtitle_rect must lie inside safe_rect")
        if self.confirmed:
            if not (self.source and self.source.strip()):
                raise SafeAreaError("a confirmed preset needs a source")
            if not self.calibrated_on or not _DATE_RE.match(self.calibrated_on):
                raise SafeAreaError(
                    "a confirmed preset needs calibrated_on=YYYY-MM-DD"
                )
            if not self.ui_surface or not self.ui_surface.strip():
                raise SafeAreaError("a confirmed preset needs ui_surface")

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "version": self.version,
            "canvas": {
                "width": self.canvas_width,
                "height": self.canvas_height,
            },
            "safe_rect": self.safe_rect.as_dict(),
            "subtitle_rect": self.subtitle_rect.as_dict(),
            "blocked_zones": {
                name: rect.as_dict() for name, rect in self.blocked_zones
            },
            "confirmed": self.confirmed,
            "source": self.source,
            "calibrated_on": self.calibrated_on,
            "ui_surface": self.ui_surface,
            "notes": self.notes,
        }


#: P0 preset. The geometry is the internal model used until the first live
#: calibration; ``confirmed=False`` therefore blocks publish by design.
TIKTOK_SAFE_AREA_V1 = SafeAreaPreset(
    platform=PLATFORM_TIKTOK,
    version=PRESET_VERSION_V1,
    canvas_width=1080,
    canvas_height=1920,
    safe_rect=Rect(90, 260, 900, 1400),
    subtitle_rect=Rect(90, 1380, 900, 280),
    blocked_zones=(
        ("right_interaction_rail", Rect(960, 480, 120, 840)),
        ("bottom_caption_music", Rect(0, 1660, 1080, 260)),
        ("top_tab_bar", Rect(0, 0, 1080, 180)),
    ),
    confirmed=False,
    source="internal P0 model of TikTok vertical UI overlays",
    calibrated_on=None,
    ui_surface="TikTok vertical feed (P0 target)",
    notes=(
        "Calibrate against the live TikTok UI before the first publish; "
        "until then this preset is unconfirmed and blocks publishing."
    ),
)

_PRESETS: dict[str, dict[str, SafeAreaPreset]] = {
    PLATFORM_TIKTOK: {PRESET_VERSION_V1: TIKTOK_SAFE_AREA_V1},
}


def get_safe_area_preset(
    platform: str = PLATFORM_TIKTOK, version: str = PRESET_VERSION_V1
) -> SafeAreaPreset:
    """Return a registered safe-area preset, failing loudly when unknown."""
    key = (platform or "").strip().lower()
    versions = _PRESETS.get(key)
    if versions is None:
        known = ", ".join(sorted(_PRESETS)) or "none"
        raise SafeAreaError(f"unknown platform {platform!r}; registered: {known}")
    preset = versions.get(version)
    if preset is None:
        known = ", ".join(sorted(versions)) or "none"
        raise SafeAreaError(
            f"unknown preset version {version!r} for {key}; registered: {known}"
        )
    return preset


def confirm_safe_area_preset(
    preset: SafeAreaPreset,
    *,
    source: str,
    calibrated_on: str,
    ui_surface: str,
) -> SafeAreaPreset:
    """Return ``preset`` marked confirmed with the evidence the gate requires.

    Confirmation is deliberately an explicit, evidence-carrying action: there
    is no "assume confirmed" switch.
    """
    for name, value in (
        ("source", source),
        ("calibrated_on", calibrated_on),
        ("ui_surface", ui_surface),
    ):
        if not isinstance(value, str) or not value.strip():
            raise SafeAreaError(f"{name} must be a non-empty string")
    if not _DATE_RE.match(calibrated_on):
        raise SafeAreaError("calibrated_on must use YYYY-MM-DD")
    return replace(
        preset,
        confirmed=True,
        source=source.strip(),
        calibrated_on=calibrated_on,
        ui_surface=ui_surface.strip(),
    )


# --- subtitle check --------------------------------------------------------


def _violations_for_rect(
    rect: Rect,
    *,
    allowed: Rect,
    preset: SafeAreaPreset,
    allowed_code: str,
) -> list[str]:
    violations: list[str] = []
    if not allowed.contains(rect):
        violations.append(allowed_code)
    if not preset.safe_rect.contains(rect) and allowed_code not in violations:
        violations.append("outside_safe_area")
    for zone_name, zone in preset.blocked_zones:
        if rect.intersects(zone):
            violations.append(f"overlaps_blocked_zone:{zone_name}")
    return violations


def check_subtitle_rects(
    rects: Iterable[Rect | Mapping[str, Any] | Sequence[Any]] | None,
    preset: SafeAreaPreset,
) -> dict[str, Any]:
    """Check subtitle burn rectangles against the preset's subtitle zone.

    An empty/absent input is ``unknown`` rather than a pass: the P0 gate
    requires the actual burn geometry.
    """
    if rects is None:
        return {
            "status": "unknown",
            "geometry": "subtitle_rects",
            "rects": [],
            "violations": ["subtitle_geometry_missing"],
        }
    items = list(rects)
    results: list[dict[str, Any]] = []
    violations: list[str] = []
    for index, raw in enumerate(items):
        rect = _coerce_rect(raw)
        item_violations = _violations_for_rect(
            rect,
            allowed=preset.subtitle_rect,
            preset=preset,
            allowed_code="outside_subtitle_zone",
        )
        results.append({
            "index": index,
            "rect": rect.as_dict(),
            "violations": item_violations,
        })
        violations.extend(item_violations)

    return {
        "status": "fail" if violations else ("pass" if results else "unknown"),
        "geometry": "subtitle_rects",
        "rects": results,
        "violations": violations,
    }


# --- face check ------------------------------------------------------------


@dataclass(frozen=True)
class FaceBox:
    """A detected face mapped onto the delivery canvas."""

    frame_index: int
    timestamp_seconds: float | None
    bbox: Rect
    confidence: float | None
    normalized_bbox: dict[str, float]
    source: str
    video_width: int
    video_height: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "timestamp_seconds": self.timestamp_seconds,
            "bbox": self.bbox.as_dict(),
            "confidence": self.confidence,
            "normalized_bbox": dict(self.normalized_bbox),
            "source": self.source,
            "video_width": self.video_width,
            "video_height": self.video_height,
        }


def face_bboxes_from_tracking(
    tracking: Mapping[str, Any] | None,
    *,
    canvas_width: int,
    canvas_height: int,
    frame_indices: Iterable[int] | None = None,
) -> list[FaceBox]:
    """Extract detected faces from a face_tracker JSON result.

    face_tracker stores normalised ``x/y/width/height`` plus the source video
    dimensions. Boxes are scaled onto the delivery canvas here, and every
    returned box carries ``source="face_tracker"`` so later checks can prove
    the geometry came from detection rather than manual entry.

    Raises:
        SafeAreaError: the input is not a face_tracker result or a box is
            malformed.
    """
    if not isinstance(tracking, Mapping):
        raise SafeAreaError("face tracking result must be a mapping")
    for key in ("video_width", "video_height", "faces"):
        if key not in tracking:
            raise SafeAreaError(
                f"not a face_tracker result: missing {key!r}"
            )
    video_width = tracking["video_width"]
    video_height = tracking["video_height"]
    if (
        not isinstance(video_width, int)
        or isinstance(video_width, bool)
        or video_width <= 0
        or not isinstance(video_height, int)
        or isinstance(video_height, bool)
        or video_height <= 0
    ):
        raise SafeAreaError("face_tracker video dimensions must be positive ints")
    faces = tracking["faces"]
    if not isinstance(faces, (list, tuple)):
        raise SafeAreaError("face_tracker faces must be a list")
    if canvas_width <= 0 or canvas_height <= 0:
        raise SafeAreaError("canvas dimensions must be positive")

    wanted = set(frame_indices) if frame_indices is not None else None
    boxes: list[FaceBox] = []
    for index, face in enumerate(faces):
        if not isinstance(face, Mapping):
            raise SafeAreaError(f"face #{index + 1} must be a mapping")
        frame_index = face.get("frame_index")
        if not isinstance(frame_index, int) or isinstance(frame_index, bool):
            raise SafeAreaError(f"face #{index + 1} has no integer frame_index")
        if wanted is not None and frame_index not in wanted:
            continue

        bbox = face.get("bbox")
        if not isinstance(bbox, Mapping):
            raise SafeAreaError(f"face #{index + 1} has no bbox mapping")
        try:
            nx, ny, nw, nh = (
                float(bbox["x"]),
                float(bbox["y"]),
                float(bbox["width"]),
                float(bbox["height"]),
            )
        except KeyError as exc:
            raise SafeAreaError(
                f"face #{index + 1} bbox is missing {exc.args[0]!r}"
            ) from None
        except (TypeError, ValueError) as exc:
            raise SafeAreaError(
                f"face #{index + 1} bbox is not numeric: {exc}"
            ) from None
        if not all(math.isfinite(value) for value in (nx, ny, nw, nh)):
            raise SafeAreaError(f"face #{index + 1} bbox must be finite")
        if nw <= 0 or nh <= 0:
            raise SafeAreaError(
                f"face #{index + 1} bbox width/height must be positive"
            )

        pixel_rect = Rect(
            nx * canvas_width,
            ny * canvas_height,
            nw * canvas_width,
            nh * canvas_height,
        )
        timestamp = face.get("timestamp_seconds")
        if timestamp is not None:
            try:
                timestamp = float(timestamp)
            except (TypeError, ValueError) as exc:
                raise SafeAreaError(
                    f"face #{index + 1} timestamp_seconds is not numeric: {exc}"
                ) from None
        confidence = face.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise SafeAreaError(
                    f"face #{index + 1} confidence is not numeric: {exc}"
                ) from None

        boxes.append(
            FaceBox(
                frame_index=frame_index,
                timestamp_seconds=timestamp,
                bbox=pixel_rect,
                confidence=confidence,
                normalized_bbox={
                    "x": nx,
                    "y": ny,
                    "width": nw,
                    "height": nh,
                },
                source=_FACE_SOURCE,
                video_width=video_width,
                video_height=video_height,
            )
        )
    return boxes


def check_face_boxes(
    boxes: Iterable[FaceBox], preset: SafeAreaPreset
) -> dict[str, Any]:
    """Check detected face boxes against the safe rectangle and overlays."""
    items = list(boxes)
    results: list[dict[str, Any]] = []
    violations: list[str] = []
    for index, box in enumerate(items):
        if box.source != _FACE_SOURCE:
            raise SafeAreaError(
                f"face box #{index + 1} source must be {_FACE_SOURCE!r}, "
                f"got {box.source!r}"
            )
        item_violations = _violations_for_rect(
            box.bbox,
            allowed=preset.safe_rect,
            preset=preset,
            allowed_code="outside_safe_area",
        )
        results.append({
            "frame_index": box.frame_index,
            "bbox": box.bbox.as_dict(),
            "source": box.source,
            "violations": item_violations,
        })
        violations.extend(item_violations)

    return {
        "status": "fail" if violations else ("pass" if results else "unknown"),
        "geometry": "face_bboxes",
        "source": _FACE_SOURCE,
        "boxes": results,
        "violations": violations,
    }


# --- publish gate ----------------------------------------------------------


def assess_safe_area(
    *,
    preset: SafeAreaPreset,
    subtitle_rects: Iterable[Rect | Mapping[str, Any] | Sequence[Any]] | None = None,
    face_tracking: Mapping[str, Any] | None = None,
    expect_faces: bool = True,
    frame_indices: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Run the P0 safe-area gate and return an auditable report.

    Publishing is ready only when all of these hold:

    - the preset is confirmed against the live UI,
    - every subtitle burn rectangle is inside the preset's subtitle zone,
    - every detected face box is inside the safe rectangle and clear of the
      blocked overlays (or ``expect_faces=False`` for a shot with no people).

    Other visual elements remain a human check, so
    ``manual_review_required`` is always true.
    """
    blockers: list[str] = []

    confirmation_status = "pass" if preset.confirmed else "blocked"
    if not preset.confirmed:
        blockers.append("preset_unconfirmed")

    subtitles = check_subtitle_rects(subtitle_rects, preset)
    if subtitles["status"] == "fail":
        blockers.append("subtitle_outside_safe_area")
    elif subtitles["status"] == "unknown":
        blockers.append("subtitle_geometry_missing")

    if not expect_faces:
        faces: dict[str, Any] = {
            "status": "not_applicable",
            "geometry": "face_bboxes",
            "reason": "shot declared as having no expected faces",
            "boxes": [],
            "violations": [],
        }
    elif face_tracking is None:
        faces = {
            "status": "unknown",
            "geometry": "face_bboxes",
            "reason": "no face_tracker result supplied",
            "boxes": [],
            "violations": ["face_detection_unknown"],
        }
        blockers.append("face_detection_unknown")
    else:
        boxes = face_bboxes_from_tracking(
            face_tracking,
            canvas_width=preset.canvas_width,
            canvas_height=preset.canvas_height,
            frame_indices=frame_indices,
        )
        faces = check_face_boxes(boxes, preset)
        if faces["status"] == "fail":
            blockers.append("face_outside_safe_area")
        elif faces["status"] == "unknown":
            faces["reason"] = "face_tracker detected no faces in the sampled frames"
            blockers.append("face_detection_unknown")

    return {
        "status": "pass" if not blockers else "blocked",
        "publish_ready": not blockers,
        "blockers": blockers,
        "manual_review_required": True,
        "manual_elements": [
            "logos and text overlays",
            "props and scene composition",
            "target-UI visual inspection",
        ],
        "preset": preset.as_dict(),
        "checks": {
            "preset_confirmation": {
                "status": confirmation_status,
                "confirmed": preset.confirmed,
            },
            "subtitles": subtitles,
            "faces": faces,
        },
    }