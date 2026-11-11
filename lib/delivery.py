"""Deterministic delivery validation for the short-drama vertical pipeline.

Implements DEL-01 (ffprobe-metadata compliance for 1080x1920 delivery, with the
480P-draft upscale path marked DEGRADED), VIS-06 (versioned TikTok vertical
safe-area preset checked against subtitle rectangles and face bboxes) and
LOC-02 (English subtitle style limits that follow overseas short-video habit).

M0 contract: this module never shells out to ffprobe, never touches a network
API, and never spends money. Callers pass a supplied metadata document (real
ffprobe JSON or a normalized subset) and receive a structured report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from lib.subtitle import (
    MAX_CUE_DURATION_S,
    MAX_LINE_CHARS,
    MAX_LINES,
    MAX_WORDS_PER_CUE,
    TIKTOK_9X16,
)

PASS = "PASS"
DEGRADED = "DEGRADED"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "N/A"

PLAYBOOK_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "playbooks" / "short-drama-realistic.yaml"
)

_CANVAS_WIDTH = 1080
_CANVAS_HEIGHT = 1920


def _px_x(fraction: float) -> int:
    return int(round(fraction * _CANVAS_WIDTH))


def _px_y(fraction: float) -> int:
    return int(round(fraction * _CANVAS_HEIGHT))


#: VIS-06: the TikTok preset is versioned and derives from the single
#: normalized definition in :mod:`lib.subtitle` (WP-N), so subtitle burn-in and
#: delivery-side checks can never drift apart. A preset change must produce a
#: new ``version`` (and update the playbook) so past deliveries stay auditable.
_LEFT, _RIGHT = _px_x(TIKTOK_9X16.x_min), _px_x(TIKTOK_9X16.x_max)
_TOP, _BOTTOM = _px_y(TIKTOK_9X16.y_min), _px_y(TIKTOK_9X16.y_max)
_SUBTITLE_HEIGHT = 360

TIKTOK_SAFE_AREA: dict[str, Any] = {
    "preset": TIKTOK_9X16.name,
    "version": TIKTOK_9X16.version,
    "calibrated": TIKTOK_9X16.calibrated,
    "canvas": {"width": _CANVAS_WIDTH, "height": _CANVAS_HEIGHT},
    "content_rect": {
        "x": _LEFT,
        "y": _TOP,
        "width": _RIGHT - _LEFT,
        "height": _BOTTOM - _TOP,
    },
    "subtitle_rect": {
        "x": _LEFT,
        "y": _BOTTOM - _SUBTITLE_HEIGHT,
        "width": _RIGHT - _LEFT,
        "height": _SUBTITLE_HEIGHT,
    },
    "blocked_regions": [
        {
            "name": "right_interaction_rail",
            "rect": {
                "x": _RIGHT,
                "y": _TOP,
                "width": _CANVAS_WIDTH - _RIGHT,
                "height": _BOTTOM - _TOP,
            },
        },
        {
            "name": "bottom_caption_music",
            "rect": {
                "x": 0,
                "y": _BOTTOM,
                "width": _CANVAS_WIDTH,
                "height": _CANVAS_HEIGHT - _BOTTOM,
            },
        },
        {
            "name": "top_status_bar",
            "rect": {"x": 0, "y": 0, "width": _CANVAS_WIDTH, "height": _TOP},
        },
    ],
}

#: DEL-01 platform delivery presets. The short-drama pipeline targets 60-120s.
PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "platform": "tiktok",
        "preset_version": "tiktok-vertical-v1",
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "video_codecs": ("h264", "hevc"),
        "audio_codecs": ("aac",),
        "fps_range": (24.0, 60.0),
        "duration_seconds": {"min": 60.0, "target": 120.0, "max": 180.0},
        "containers": ("mp4", "mov"),
        "safe_area": TIKTOK_SAFE_AREA,
    },
}

_PRESET_RESOLUTIONS: dict[str, tuple[int, int]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1080, 1920),
}


class DeliveryError(ValueError):
    """Raised for malformed delivery input (not for a failed check)."""


class PlaybookError(DeliveryError):
    """Raised when the short-drama playbook is missing required content."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class DeliveryReport:
    platform: str
    status: str
    checks: list[Check] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Delivery may ship when every check passed (degradation is allowed)."""
        return self.status in (PASS, DEGRADED)

    def check(self, name: str) -> Optional[Check]:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": self.status,
            "degraded": list(self.degraded),
            "checks": [c.to_dict() for c in self.checks],
        }


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(Fraction(str(value).strip()))
    except (ValueError, ZeroDivisionError):
        return None


def _normalize_rect(rect: Mapping[str, Any]) -> dict[str, float]:
    if not isinstance(rect, Mapping):
        raise DeliveryError(f"rect must be a mapping, got {type(rect).__name__}")
    width = rect.get("width", rect.get("w"))
    height = rect.get("height", rect.get("h"))
    try:
        out = {
            "x": float(rect["x"]),
            "y": float(rect["y"]),
            "width": float(width),
            "height": float(height),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryError(f"rect needs numeric x/y/width/height: {rect!r}") from exc
    if out["width"] <= 0 or out["height"] <= 0:
        raise DeliveryError(f"rect must have positive size: {rect!r}")
    return out


def _contains(outer: Mapping[str, float], inner: Mapping[str, float]) -> bool:
    return (
        inner["x"] >= outer["x"]
        and inner["y"] >= outer["y"]
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"]
    )


def _overlap_area(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    left = max(a["x"], b["x"])
    top = max(a["y"], b["y"])
    right = min(a["x"] + a["width"], b["x"] + b["width"])
    bottom = min(a["y"] + a["height"], b["y"] + b["height"])
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def rect_in_safe_area(rect: Mapping[str, Any], safe_area: Optional[Mapping[str, Any]] = None) -> tuple[bool, list[str]]:
    """Return (ok, reasons) for a rectangle checked against the VIS-06 preset.

    The rect must sit inside the content rect and must not intersect any
    blocked platform UI region (right interaction rail, bottom caption/music
    strip, top status bar).
    """
    preset = safe_area or TIKTOK_SAFE_AREA
    target = _normalize_rect(rect)
    reasons: list[str] = []
    content = _normalize_rect(preset["content_rect"])
    if not _contains(content, target):
        reasons.append("outside content_rect")
    for region in preset.get("blocked_regions", []):
        if _overlap_area(_normalize_rect(region["rect"]), target) > 0:
            reasons.append(f"overlaps {region['name']}")
    return (not reasons, reasons)


def parse_source_resolution(value: Any) -> Optional[tuple[float, float]]:
    """Parse ``"854x480"``, ``"480p"`` or ``{"width":..., "height":...}``."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        width, height = _num(value.get("width")), _num(value.get("height"))
        if width and height:
            return (width, height)
        return None
    text = str(value).strip().lower()
    if text in _PRESET_RESOLUTIONS:
        width, height = _PRESET_RESOLUTIONS[text]
        return (float(width), float(height))
    if "x" in text:
        left, _, right = text.partition("x")
        width, height = _num(left), _num(right)
        if width and height:
            return (width, height)
    return None


def _normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise DeliveryError(f"metadata must be a mapping, got {type(metadata).__name__}")

    out: dict[str, Any] = {
        "width": None,
        "height": None,
        "fps": None,
        "duration_seconds": None,
        "video_codec": None,
        "audio_codec": None,
        "has_audio": None,
        "container": None,
        "source_resolution": None,
    }

    streams = metadata.get("streams")
    if isinstance(streams, list):
        video = next(
            (s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "audio"),
            None,
        )
        if video is not None:
            out["width"] = _num(video.get("width"))
            out["height"] = _num(video.get("height"))
            out["fps"] = _num(video.get("avg_frame_rate") or video.get("r_frame_rate"))
            out["video_codec"] = video.get("codec_name")
            out["duration_seconds"] = _num(video.get("duration"))
        out["has_audio"] = audio is not None
        if audio is not None:
            out["audio_codec"] = audio.get("codec_name")
        fmt = metadata.get("format") or {}
        if isinstance(fmt, Mapping):
            if out["duration_seconds"] is None:
                out["duration_seconds"] = _num(fmt.get("duration"))
            out["container"] = fmt.get("format_name")
    else:
        out["width"] = _num(metadata.get("width"))
        out["height"] = _num(metadata.get("height"))
        out["fps"] = _num(metadata.get("fps"))
        out["duration_seconds"] = _num(
            metadata.get("duration_seconds", metadata.get("duration"))
        )
        out["video_codec"] = metadata.get("video_codec")
        out["audio_codec"] = metadata.get("audio_codec")
        out["has_audio"] = metadata.get(
            "has_audio", metadata.get("audio_codec") is not None
        )
        out["container"] = metadata.get("container")

    out["source_resolution"] = metadata.get(
        "source_resolution", metadata.get("draft_resolution")
    )
    return out


def _codec_ok(codec: Any, allowed: Iterable[str]) -> Optional[bool]:
    if codec is None:
        return None
    return str(codec).strip().lower() in {c.lower() for c in allowed}


def validate_delivery(
    metadata: Mapping[str, Any],
    *,
    platform: str = "tiktok",
    subtitle_rect: Optional[Mapping[str, Any]] = None,
    face_rects: Optional[Iterable[Mapping[str, Any]]] = None,
    safe_area: Optional[Mapping[str, Any]] = None,
) -> DeliveryReport:
    """Validate supplied ffprobe metadata against the platform preset.

    ``metadata`` accepts real ffprobe output (``streams`` + ``format``) or a
    flat dict (``width``, ``height``, ``duration_seconds``, ``fps``,
    ``video_codec``, ``audio_codec``, ``has_audio``, ``source_resolution``).

    A source/draft resolution below the delivery target marks the upscale path
    DEGRADED (lost: ``native_1080p``) rather than failing DEL-01.
    """
    if platform not in PLATFORM_PRESETS:
        raise DeliveryError(f"unknown platform preset: {platform!r}")
    preset = PLATFORM_PRESETS[platform]
    area = safe_area or preset["safe_area"]
    normalized = _normalize_metadata(metadata)

    checks: list[Check] = []
    degraded: list[str] = []

    # --- canvas ---------------------------------------------------------
    width, height = normalized["width"], normalized["height"]
    expected = (float(preset["width"]), float(preset["height"]))
    if width is None or height is None:
        checks.append(Check("resolution", UNKNOWN, "width/height missing from metadata"))
    elif (width, height) == expected:
        checks.append(Check("resolution", PASS, f"{int(width)}x{int(height)}"))
    else:
        checks.append(
            Check(
                "resolution",
                FAIL,
                f"{int(width)}x{int(height)} != {int(expected[0])}x{int(expected[1])}",
            )
        )

    if width and height:
        ratio = width / height
        target_ratio = expected[0] / expected[1]
        if abs(ratio - target_ratio) <= 0.005:
            checks.append(Check("aspect_ratio", PASS, preset["aspect_ratio"]))
        else:
            checks.append(
                Check("aspect_ratio", FAIL, f"{width:.0f}:{height:.0f} is not 9:16")
            )

    # --- duration -------------------------------------------------------
    duration = normalized["duration_seconds"]
    bounds = preset["duration_seconds"]
    if duration is None:
        checks.append(Check("duration", UNKNOWN, "duration missing from metadata"))
    elif bounds["min"] <= duration <= bounds["max"]:
        checks.append(
            Check(
                "duration",
                PASS,
                f"{duration:.2f}s within {bounds['min']:.0f}-{bounds['max']:.0f}s",
            )
        )
    else:
        checks.append(
            Check(
                "duration",
                FAIL,
                f"{duration:.2f}s outside {bounds['min']:.0f}-{bounds['max']:.0f}s",
            )
        )

    # --- frame rate -----------------------------------------------------
    fps = normalized["fps"]
    low, high = preset["fps_range"]
    if fps is None:
        checks.append(Check("fps", UNKNOWN, "frame rate missing from metadata"))
    elif low <= fps <= high:
        checks.append(Check("fps", PASS, f"{fps:.2f} fps"))
    else:
        checks.append(Check("fps", FAIL, f"{fps:.2f} fps outside {low:.0f}-{high:.0f}"))

    # --- codecs ---------------------------------------------------------
    video_ok = _codec_ok(normalized["video_codec"], preset["video_codecs"])
    if video_ok is None:
        checks.append(Check("video_codec", UNKNOWN, "video codec missing"))
    elif video_ok:
        checks.append(Check("video_codec", PASS, str(normalized["video_codec"])))
    else:
        checks.append(
            Check(
                "video_codec",
                FAIL,
                f"{normalized['video_codec']} not in {list(preset['video_codecs'])}",
            )
        )

    if normalized["has_audio"] is False or normalized["has_audio"] is None:
        checks.append(Check("audio_stream", FAIL, "no audio stream in metadata"))
    else:
        checks.append(Check("audio_stream", PASS, "audio stream present"))
        audio_ok = _codec_ok(normalized["audio_codec"], preset["audio_codecs"])
        if audio_ok is None:
            checks.append(Check("audio_codec", UNKNOWN, "audio codec missing"))
        elif audio_ok:
            checks.append(Check("audio_codec", PASS, str(normalized["audio_codec"])))
        else:
            checks.append(
                Check(
                    "audio_codec",
                    FAIL,
                    f"{normalized['audio_codec']} not in {list(preset['audio_codecs'])}",
                )
            )

    container = normalized["container"]
    if container is None:
        checks.append(Check("container", NOT_APPLICABLE, "container not supplied"))
    elif any(token in str(container).lower() for token in preset["containers"]):
        checks.append(Check("container", PASS, str(container)))
    else:
        checks.append(Check("container", FAIL, f"{container} is not mp4/mov"))

    # --- DEL-01 upscale path -------------------------------------------
    source = parse_source_resolution(normalized["source_resolution"])
    if source is None:
        checks.append(
            Check(
                "upscale",
                NOT_APPLICABLE,
                "no source/draft resolution supplied; native path assumed",
            )
        )
    elif source[0] < expected[0] or source[1] < expected[1]:
        degraded.append("native_1080p")
        checks.append(
            Check(
                "upscale",
                DEGRADED,
                f"source {int(source[0])}x{int(source[1])} upscaled to "
                f"{int(expected[0])}x{int(expected[1])}; lost native_1080p",
            )
        )
    else:
        checks.append(
            Check(
                "upscale",
                PASS,
                f"source {int(source[0])}x{int(source[1])} meets delivery target",
            )
        )

    # --- VIS-06 safe area ----------------------------------------------
    if subtitle_rect is not None:
        ok, reasons = rect_in_safe_area(subtitle_rect, area)
        checks.append(
            Check(
                "subtitle_safe_area",
                PASS if ok else FAIL,
                "inside TikTok preset" if ok else "; ".join(reasons),
            )
        )
    for index, face in enumerate(face_rects or []):
        ok, reasons = rect_in_safe_area(face, area)
        checks.append(
            Check(
                f"face_{index}_safe_area",
                PASS if ok else FAIL,
                "inside TikTok preset" if ok else "; ".join(reasons),
            )
        )

    status = _overall_status(checks)
    return DeliveryReport(
        platform=platform, status=status, checks=checks, degraded=degraded
    )


def _overall_status(checks: Iterable[Check]) -> str:
    statuses = [c.status for c in checks if c.status != NOT_APPLICABLE]
    if any(status == FAIL or status == UNKNOWN for status in statuses):
        return FAIL
    if any(status == DEGRADED for status in statuses):
        return DEGRADED
    return PASS


# --- playbook ---------------------------------------------------------------

_REQUIRED_PLAYBOOK_SECTIONS = (
    "identity",
    "delivery",
    "safe_area",
    "subtitles",
    "visual",
    "audio",
    "rendering",
)

_REQUIRED_RUNTIMES = ("remotion", "hyperframes", "ffmpeg")


def load_drama_playbook(path: Optional[Path] = None) -> dict[str, Any]:
    """Load and validate ``data/playbooks/short-drama-realistic.yaml``."""
    playbook_path = Path(path) if path is not None else PLAYBOOK_PATH
    if not playbook_path.is_file():
        raise PlaybookError(f"playbook not found: {playbook_path}")
    with open(playbook_path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, Mapping):
        raise PlaybookError(f"playbook root must be a mapping: {playbook_path}")
    errors = validate_drama_playbook(document)
    if errors:
        raise PlaybookError("; ".join(errors))
    return dict(document)


def validate_drama_playbook(document: Mapping[str, Any]) -> list[str]:
    """Return every schema/consistency error in a short-drama playbook."""
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["playbook root must be a mapping"]

    for section in _REQUIRED_PLAYBOOK_SECTIONS:
        if section not in document:
            errors.append(f"missing section: {section}")
    if errors:
        return errors

    delivery = document.get("delivery") or {}
    if not isinstance(delivery, Mapping):
        errors.append("delivery must be a mapping")
    else:
        if str(delivery.get("aspect_ratio", "")).strip() != "9:16":
            errors.append("delivery.aspect_ratio must be 9:16")
        resolution = delivery.get("resolution")
        if isinstance(resolution, Mapping):
            resolution = f"{resolution.get('width')}x{resolution.get('height')}"
        if str(resolution) != "1080x1920":
            errors.append("delivery.resolution must be 1080x1920")
        duration = delivery.get("duration_seconds") or {}
        if not isinstance(duration, Mapping):
            errors.append("delivery.duration_seconds must be a mapping")
        else:
            for key in ("min", "target", "max"):
                if not isinstance(duration.get(key), (int, float)):
                    errors.append(f"delivery.duration_seconds.{key} must be numeric")

    safe_area = document.get("safe_area") or {}
    if not isinstance(safe_area, Mapping):
        errors.append("safe_area must be a mapping")
    else:
        for key in ("preset", "version", "canvas", "content_rect", "subtitle_rect", "blocked_regions"):
            if key not in safe_area:
                errors.append(f"safe_area.{key} missing")
        if (
            "content_rect" in safe_area
            and "blocked_regions" in safe_area
            and "subtitle_rect" in safe_area
        ):
            try:
                ok, reasons = rect_in_safe_area(safe_area["subtitle_rect"], safe_area)
                if not ok:
                    errors.append(
                        "safe_area.subtitle_rect invalid: " + "; ".join(reasons)
                    )
            except DeliveryError as exc:
                errors.append(f"safe_area rect invalid: {exc}")

    subtitles = document.get("subtitles") or {}
    if not isinstance(subtitles, Mapping):
        errors.append("subtitles must be a mapping")
    else:
        # LOC-02 / AUD-06 thresholds live in lib/subtitle.py (WP-N); the
        # playbook must reuse them, not invent a second set.
        max_chars = subtitles.get("max_chars_per_line")
        if max_chars != MAX_LINE_CHARS:
            errors.append(
                f"subtitles.max_chars_per_line must equal lib.subtitle.MAX_LINE_CHARS ({MAX_LINE_CHARS})"
            )
        max_lines = subtitles.get("max_lines_per_cue")
        if max_lines != MAX_LINES:
            errors.append(
                f"subtitles.max_lines_per_cue must equal lib.subtitle.MAX_LINES ({MAX_LINES})"
            )
        max_words = subtitles.get("max_words_per_cue")
        if max_words != MAX_WORDS_PER_CUE:
            errors.append(
                f"subtitles.max_words_per_cue must equal lib.subtitle.MAX_WORDS_PER_CUE ({MAX_WORDS_PER_CUE})"
            )
        max_cue = subtitles.get("max_cue_seconds")
        if max_cue != MAX_CUE_DURATION_S:
            errors.append(
                f"subtitles.max_cue_seconds must equal lib.subtitle.MAX_CUE_DURATION_S ({MAX_CUE_DURATION_S})"
            )
        if not isinstance(subtitles.get("style"), Mapping):
            errors.append("subtitles.style must be a mapping")

    visual = document.get("visual") or {}
    if not isinstance(visual, Mapping):
        errors.append("visual must be a mapping")
    else:
        interval = visual.get("visual_change_interval_seconds") or {}
        if not isinstance(interval, Mapping):
            errors.append("visual.visual_change_interval_seconds must be a mapping")
        else:
            low, high = interval.get("min"), interval.get("max")
            if not isinstance(low, (int, float)) or low < 1:
                errors.append("visual change interval min must be >= 1s")
            if not isinstance(high, (int, float)) or high > 3:
                errors.append("visual change interval max must be <= 3s")
        hook = visual.get("hook_window_seconds")
        if not isinstance(hook, (int, float)) or not 0 < hook <= 3:
            errors.append("visual.hook_window_seconds must be <= 3")

    audio = document.get("audio") or {}
    if not isinstance(audio, Mapping):
        errors.append("audio must be a mapping")
    else:
        if audio.get("loudness_lufs") != -14:
            errors.append("audio.loudness_lufs must be -14")
        if audio.get("true_peak_dbtp") != -1:
            errors.append("audio.true_peak_dbtp must be -1")
        volume = audio.get("music_volume")
        if not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
            errors.append("audio.music_volume must be 0..1")

    rendering = document.get("rendering") or {}
    if not isinstance(rendering, Mapping):
        errors.append("rendering must be a mapping")
    else:
        runtimes = rendering.get("runtimes") or {}
        if not isinstance(runtimes, Mapping):
            errors.append("rendering.runtimes must be a mapping")
        else:
            for runtime in _REQUIRED_RUNTIMES:
                if not isinstance(runtimes.get(runtime), Mapping):
                    errors.append(f"rendering.runtimes.{runtime} missing")
        models = rendering.get("video_models") or {}
        if not isinstance(models, Mapping) or not models:
            errors.append("rendering.video_models must be a non-empty mapping")
        else:
            for model, params in models.items():
                if not isinstance(params, Mapping):
                    errors.append(f"rendering.video_models.{model} must be a mapping")
                elif "degraded" not in params:
                    errors.append(f"rendering.video_models.{model}.degraded missing")

    return errors