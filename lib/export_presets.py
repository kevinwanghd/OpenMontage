"""DEL-02/DEL-03 export presets and duration variants for short drama.

DEL-02: TikTok / YouTube Shorts / Instagram Reels have different duration
ceilings, safe areas, encoding constraints and cover requirements. One master
production is rendered into one variant per preset; each variant gets a
machine spec-compliance verdict and a QC gate.

DEL-03: the 120s master may also be re-created as a 90s *creative* variant. The
variant is derived by the declared beat-map scaling function (BEAT-01/05,
``beat-scale-v1``) -- it is never a truncated 120s cut. Because machine checks
cannot prove that a shorter story still reads as a complete narrative with its
cliffhanger intact, the variant carries explicit human-confirmation fields.

M0 contract (铁律): no renderer, no ffprobe subprocess, no platform API and no
spend. Callers supply already-known metadata/specs as data; this module only
normalizes and judges them. UNKNOWN is always reported as UNKNOWN, never PASS.

Platform rules are engineering defaults and are marked ``UNCONFIRMED``: the
official rule text still has to be checked before publishing (DEL-09 / A1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from lib import beat_map as _beat_map
from lib.delivery import (
    DEGRADED,
    FAIL,
    NOT_APPLICABLE,
    PASS,
    UNKNOWN,
    TIKTOK_SAFE_AREA,
    parse_source_resolution,
    rect_in_safe_area,
)

SCHEMA_RULE_VERIFICATION = "UNCONFIRMED"

#: Duration ceiling differences are the main reason one master cannot ship
#: unchanged to every platform. Values are declared engineering defaults.
PLATFORM_EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "platform": "tiktok",
        "label": "TikTok",
        "preset_version": "tiktok-vertical-v1",
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "duration_seconds": {"min": 15.0, "max": 180.0},
        "fps_range": (24.0, 60.0),
        "video_codecs": ("h264", "hevc"),
        "audio_codecs": ("aac",),
        "containers": ("mp4", "mov"),
        "video_bitrate_kbps": {"min": 4000, "recommended": 8000},
        "cover": {
            "width": 1080,
            "height": 1920,
            "aspect_ratio": "9:16",
            "required": True,
        },
        "title_max_chars": 2200,
        "hashtag_max": 30,
        "calibrated": TIKTOK_SAFE_AREA.get("calibrated", False),
        "safe_area": TIKTOK_SAFE_AREA,
    },
    "youtube_shorts": {
        "platform": "youtube_shorts",
        "label": "YouTube Shorts",
        "preset_version": "shorts-vertical-v1",
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "duration_seconds": {"min": 15.0, "max": 180.0},
        "fps_range": (24.0, 60.0),
        "video_codecs": ("h264", "hevc", "vp9"),
        "audio_codecs": ("aac", "opus"),
        "containers": ("mp4", "mov", "webm"),
        "video_bitrate_kbps": {"min": 4000, "recommended": 10000},
        "cover": {
            "width": 1080,
            "height": 1920,
            "aspect_ratio": "9:16",
            "required": True,
        },
        "title_max_chars": 100,
        "hashtag_max": 15,
        "calibrated": False,
        "safe_area": {
            "preset": "shorts-9x16",
            "version": "2026.11-p1-provisional",
            "calibrated": False,
            "canvas": {"width": 1080, "height": 1920},
            "content_rect": {"x": 54, "y": 154, "width": 972, "height": 1344},
            "subtitle_rect": {"x": 54, "y": 1138, "width": 972, "height": 360},
            "blocked_regions": [
                {
                    "name": "right_interaction_rail",
                    "rect": {"x": 950, "y": 154, "width": 130, "height": 1344},
                },
                {
                    "name": "bottom_caption_music",
                    "rect": {"x": 0, "y": 1498, "width": 1080, "height": 422},
                },
                {
                    "name": "top_status_bar",
                    "rect": {"x": 0, "y": 0, "width": 1080, "height": 154},
                },
            ],
        },
    },
    "instagram_reels": {
        "platform": "instagram_reels",
        "label": "Instagram Reels",
        "preset_version": "reels-vertical-v1",
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "duration_seconds": {"min": 15.0, "max": 90.0},
        "fps_range": (24.0, 60.0),
        "video_codecs": ("h264", "hevc"),
        "audio_codecs": ("aac",),
        "containers": ("mp4", "mov"),
        "video_bitrate_kbps": {"min": 4000, "recommended": 8000},
        "cover": {
            "width": 1080,
            "height": 1920,
            "aspect_ratio": "9:16",
            "required": True,
        },
        "title_max_chars": 2200,
        "hashtag_max": 30,
        "calibrated": False,
        "safe_area": {
            "preset": "reels-9x16",
            "version": "2026.11-p1-provisional",
            "calibrated": False,
            "canvas": {"width": 1080, "height": 1920},
            "content_rect": {"x": 54, "y": 192, "width": 918, "height": 1344},
            "subtitle_rect": {"x": 54, "y": 1176, "width": 918, "height": 360},
            "blocked_regions": [
                {
                    "name": "right_interaction_rail",
                    "rect": {"x": 929, "y": 192, "width": 151, "height": 1344},
                },
                {
                    "name": "bottom_caption_music",
                    "rect": {"x": 0, "y": 1536, "width": 1080, "height": 384},
                },
                {
                    "name": "top_status_bar",
                    "rect": {"x": 0, "y": 0, "width": 1080, "height": 192},
                },
            ],
        },
    },
}

TARGET_PLATFORMS: tuple[str, ...] = tuple(PLATFORM_EXPORT_PRESETS)

#: DEL-03: creative duration variant target (A1: a creative choice, not a
#: platform ceiling).
CREATIVE_VARIANT_DURATION_S = 90.0


class ExportError(ValueError):
    """Raised for malformed export input (not for a failed compliance check)."""


@dataclass(frozen=True)
class ExportCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ExportReport:
    platform: str
    preset_version: str
    status: str
    checks: list[ExportCheck] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Only a fully passing spec check clears the DEL-02 QC gate."""
        return self.status == PASS

    def check(self, name: str) -> Optional[ExportCheck]:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "preset_version": self.preset_version,
            "status": self.status,
            "qc_gate": "PASS" if self.ok else "BLOCKED",
            "unknowns": list(self.unknowns),
            "rule_verification": SCHEMA_RULE_VERIFICATION,
            "checks": [c.to_dict() for c in self.checks],
        }


def preset_for(platform: str) -> dict[str, Any]:
    try:
        return PLATFORM_EXPORT_PRESETS[platform]
    except KeyError as exc:
        raise ExportError(f"unknown export preset: {platform!r}") from exc


# --- metadata normalization -------------------------------------------------


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Accept ffprobe-shaped or flat supplied metadata (read-only reuse)."""
    if not isinstance(metadata, Mapping):
        raise ExportError(f"metadata must be a mapping, got {type(metadata).__name__}")

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
            raw_fps = video.get("avg_frame_rate") or video.get("r_frame_rate")
            if isinstance(raw_fps, str) and "/" in raw_fps:
                numerator, _, denominator = raw_fps.partition("/")
                fps = _num(numerator)
                den = _num(denominator)
                out["fps"] = fps / den if fps and den else None
            else:
                out["fps"] = _num(raw_fps)
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


def _check_cover(cover: Optional[Mapping[str, Any]], spec: Mapping[str, Any]) -> ExportCheck:
    required = spec.get("required", True)
    if cover is None:
        return ExportCheck(
            "cover",
            FAIL if required else NOT_APPLICABLE,
            "cover spec missing from supplied variant" if required else "cover optional",
        )
    if not isinstance(cover, Mapping):
        return ExportCheck("cover", UNKNOWN, f"cover must be a mapping, got {type(cover).__name__}")
    width, height = _num(cover.get("width")), _num(cover.get("height"))
    if width is None or height is None:
        return ExportCheck("cover", UNKNOWN, "cover width/height missing")
    expected_w, expected_h = float(spec["width"]), float(spec["height"])
    if (width, height) != (expected_w, expected_h):
        return ExportCheck(
            "cover",
            FAIL,
            f"cover {int(width)}x{int(height)} != {int(expected_w)}x{int(expected_h)}",
        )
    return ExportCheck("cover", PASS, f"{int(width)}x{int(height)}")


# --- DEL-02 spec compliance -------------------------------------------------


def evaluate_export(
    platform: str,
    metadata: Mapping[str, Any],
    *,
    subtitle_rect: Optional[Mapping[str, Any]] = None,
    face_rects: Optional[Iterable[Mapping[str, Any]]] = None,
    cover: Optional[Mapping[str, Any]] = None,
) -> ExportReport:
    """Judge one supplied variant against its platform preset (DEL-02).

    A missing fact is ``UNKNOWN`` and blocks the QC gate; it is never silently
    treated as a pass. ``rule_verification`` stays ``UNCONFIRMED`` regardless of
    the spec verdict because official platform rules must still be checked.
    """
    preset = preset_for(platform)
    normalized = _normalize_metadata(metadata)
    checks: list[ExportCheck] = []

    width, height = normalized["width"], normalized["height"]
    expected = (float(preset["width"]), float(preset["height"]))
    if width is None or height is None:
        checks.append(ExportCheck("resolution", UNKNOWN, "width/height missing"))
    elif (width, height) == expected:
        checks.append(ExportCheck("resolution", PASS, f"{int(width)}x{int(height)}"))
    else:
        checks.append(
            ExportCheck(
                "resolution",
                FAIL,
                f"{int(width)}x{int(height)} != {int(expected[0])}x{int(expected[1])}",
            )
        )

    if width and height:
        target_ratio = expected[0] / expected[1]
        if abs(width / height - target_ratio) <= 0.005:
            checks.append(ExportCheck("aspect_ratio", PASS, preset["aspect_ratio"]))
        else:
            checks.append(
                ExportCheck("aspect_ratio", FAIL, f"{width:.0f}:{height:.0f} is not 9:16")
            )

    duration = normalized["duration_seconds"]
    bounds = preset["duration_seconds"]
    if duration is None:
        checks.append(ExportCheck("duration", UNKNOWN, "duration missing"))
    elif bounds["min"] <= duration <= bounds["max"]:
        checks.append(
            ExportCheck(
                "duration",
                PASS,
                f"{duration:.2f}s within {bounds['min']:.0f}-{bounds['max']:.0f}s",
            )
        )
    else:
        checks.append(
            ExportCheck(
                "duration",
                FAIL,
                f"{duration:.2f}s outside {bounds['min']:.0f}-{bounds['max']:.0f}s "
                f"(needs a re-created variant, not truncation)",
            )
        )

    fps = normalized["fps"]
    low, high = preset["fps_range"]
    if fps is None:
        checks.append(ExportCheck("fps", UNKNOWN, "frame rate missing"))
    elif low <= fps <= high:
        checks.append(ExportCheck("fps", PASS, f"{fps:.2f} fps"))
    else:
        checks.append(ExportCheck("fps", FAIL, f"{fps:.2f} fps outside {low:.0f}-{high:.0f}"))

    video_ok = _codec_ok(normalized["video_codec"], preset["video_codecs"])
    if video_ok is None:
        checks.append(ExportCheck("video_codec", UNKNOWN, "video codec missing"))
    elif video_ok:
        checks.append(ExportCheck("video_codec", PASS, str(normalized["video_codec"])))
    else:
        checks.append(
            ExportCheck(
                "video_codec",
                FAIL,
                f"{normalized['video_codec']} not in {list(preset['video_codecs'])}",
            )
        )

    if normalized["has_audio"] is False or normalized["has_audio"] is None:
        checks.append(ExportCheck("audio_stream", FAIL, "no audio stream"))
    else:
        checks.append(ExportCheck("audio_stream", PASS, "audio stream present"))
        audio_ok = _codec_ok(normalized["audio_codec"], preset["audio_codecs"])
        if audio_ok is None:
            checks.append(ExportCheck("audio_codec", UNKNOWN, "audio codec missing"))
        elif audio_ok:
            checks.append(ExportCheck("audio_codec", PASS, str(normalized["audio_codec"])))
        else:
            checks.append(
                ExportCheck(
                    "audio_codec",
                    FAIL,
                    f"{normalized['audio_codec']} not in {list(preset['audio_codecs'])}",
                )
            )

    container = normalized["container"]
    if container is None:
        checks.append(ExportCheck("container", NOT_APPLICABLE, "container not supplied"))
    elif any(token in str(container).lower() for token in preset["containers"]):
        checks.append(ExportCheck("container", PASS, str(container)))
    else:
        checks.append(ExportCheck("container", FAIL, f"{container} unsupported"))

    source = parse_source_resolution(normalized["source_resolution"])
    if source is None:
        checks.append(
            ExportCheck("upscale", NOT_APPLICABLE, "no source resolution supplied")
        )
    elif source[0] < expected[0] or source[1] < expected[1]:
        checks.append(
            ExportCheck(
                "upscale",
                DEGRADED,
                f"source {int(source[0])}x{int(source[1])} upscaled to "
                f"{int(expected[0])}x{int(expected[1])}",
            )
        )
    else:
        checks.append(ExportCheck("upscale", PASS, "native target resolution"))

    area = preset["safe_area"]
    if subtitle_rect is not None:
        ok, reasons = rect_in_safe_area(subtitle_rect, area)
        checks.append(
            ExportCheck(
                "subtitle_safe_area",
                PASS if ok else FAIL,
                f"inside {area['preset']}@{area['version']}" if ok else "; ".join(reasons),
            )
        )
    for index, face in enumerate(face_rects or []):
        ok, reasons = rect_in_safe_area(face, area)
        checks.append(
            ExportCheck(
                f"face_{index}_safe_area",
                PASS if ok else FAIL,
                f"inside {area['preset']}" if ok else "; ".join(reasons),
            )
        )

    checks.append(_check_cover(cover, preset["cover"]))

    statuses = [c.status for c in checks if c.status != NOT_APPLICABLE]
    if any(s in (FAIL, UNKNOWN) for s in statuses):
        status = FAIL
    elif any(s == DEGRADED for s in statuses):
        status = DEGRADED
    else:
        status = PASS
    unknowns = [c.name for c in checks if c.status == UNKNOWN]
    return ExportReport(
        platform=platform,
        preset_version=preset["preset_version"],
        status=status,
        checks=checks,
        unknowns=unknowns,
    )


# --- DEL-02 one production, several variants --------------------------------


def _duration_verdict(preset: Mapping[str, Any], duration_s: float) -> tuple[bool, Optional[float]]:
    bounds = preset["duration_seconds"]
    if bounds["min"] <= duration_s <= bounds["max"]:
        return True, None
    return False, min(max(duration_s, bounds["min"]), bounds["max"])


def build_variants(
    master: Mapping[str, Any],
    *,
    platforms: Iterable[str] = TARGET_PLATFORMS,
    subtitle_rect: Optional[Mapping[str, Any]] = None,
    face_rects: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Plan one export variant per target platform from a single master.

    ``master`` supplies at least ``duration_s`` (and optionally a cover spec).
    A platform whose duration ceiling is shorter than the master is marked
    ``requires_reoptimization`` with ``truncated: False`` -- the fix is a
    re-created variant (DEL-03), never cutting the tail off the master.
    """
    if not isinstance(master, Mapping):
        raise ExportError("master must be a mapping")
    duration = _num(master.get("duration_s", master.get("duration_seconds")))
    if duration is None or duration <= 0:
        raise ExportError("master.duration_s must be a positive number")
    cover = master.get("cover")

    variants: list[dict[str, Any]] = []
    for platform in platforms:
        preset = preset_for(platform)
        ok, recommended = _duration_verdict(preset, duration)
        variant = {
            "platform": platform,
            "label": preset["label"],
            "preset_version": preset["preset_version"],
            "master_duration_s": duration,
            "duration_s": duration,
            "duration_ok": ok,
            "requires_reoptimization": not ok,
            "truncated": False,
            "recommended_duration_s": recommended,
            "spec": preset,
            "rule_verification": SCHEMA_RULE_VERIFICATION,
            # Planning only: without supplied render metadata the QC gate runs
            # later on the real artifact (UNKNOWN is never a pass).
            "compliance": None,
        }
        if ok and isinstance(master.get("metadata"), Mapping):
            variant["compliance"] = evaluate_export(
                platform,
                {"duration_seconds": duration, **dict(master["metadata"])},
                subtitle_rect=subtitle_rect,
                face_rects=face_rects,
                cover=cover,
            ).to_dict()
        variants.append(variant)
    return {
        "master_duration_s": duration,
        "variants": variants,
        "all_duration_ok": all(v["duration_ok"] for v in variants),
        "human_confirmation_required": True,
    }


# --- DEL-03 creative duration variant (re-built, never truncated) ------------


def build_duration_variant(
    master_beat_map: Mapping[str, Any],
    target_duration_s: float = CREATIVE_VARIANT_DURATION_S,
) -> dict[str, Any]:
    """Re-create the retention skeleton for ``target_duration_s``.

    Returns ``{"variant": <beat map artifact>, "derivation": {...}}``. The
    variant is built by :func:`lib.beat_map.build_beat_map` at the target
    duration, so segment ratios and hook/turn counts follow the declared
    scaling function. ``derivation.truncated`` is always ``False``.

    Narrative completeness and cliffhanger preservation remain human
    confirmations: machine structure proves shape, not story quality.
    """
    problems = _beat_map.validate_beat_map(dict(master_beat_map))
    if problems:
        raise ExportError("master beat map invalid: " + "; ".join(problems))
    target = float(target_duration_s)
    try:
        variant = _beat_map.build_beat_map(target)
    except _beat_map.BeatMapError as exc:
        raise ExportError(str(exc)) from exc

    source_duration = float(master_beat_map["duration_s"])
    return {
        "variant": variant,
        "derivation": {
            "source_duration_s": source_duration,
            "target_duration_s": target,
            "scaling_rule": _beat_map.SCALING_RULE_ID,
            "truncated": False,
            "source_segment_ratios": list(_beat_map.segment_ratios(source_duration)),
            "target_segment_ratios": list(_beat_map.segment_ratios(target)),
            "human_confirmation_required": True,
            "human_confirmation": {
                "narrative_complete": "unconfirmed",
                "cliffhanger_preserved": "unconfirmed",
                "hook_valid": "unconfirmed",
                "note": "Machine structure is not narrative validity; a human must confirm.",
            },
            "checks": [],
        },
    }


def evaluate_duration_variant(
    variant: Mapping[str, Any],
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    platforms: Iterable[str] = TARGET_PLATFORMS,
    subtitle_rect: Optional[Mapping[str, Any]] = None,
    face_rects: Optional[Iterable[Mapping[str, Any]]] = None,
    cover: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Machine-check a duration variant: beat map, duration and per-preset fit.

    ``metadata`` is the supplied ffprobe-shaped document for the *rendered*
    variant. When it is omitted the report only checks the plan (beat map +
    duration fit) and marks the artifact gate ``UNKNOWN`` -- this function
    never invents render metadata to manufacture a PASS.

    Returns ``{"ok": bool, "beat_map_problems": [...], "variants": [...]}``.
    Human narrative/cliffhanger confirmation is still required when ``ok`` is
    true.
    """
    if not isinstance(variant, Mapping):
        raise ExportError("variant must be a mapping")
    beat_map_document = variant.get("variant", variant)
    if not isinstance(beat_map_document, Mapping):
        raise ExportError("variant beat map must be a mapping")

    problems = _beat_map.validate_beat_map(dict(beat_map_document))
    duration = _num(beat_map_document.get("duration_s"))
    if duration is None:
        problems.append("variant duration_s missing")

    reports: list[dict[str, Any]] = []
    for platform in platforms:
        preset = preset_for(platform)
        duration_ok, recommended = (
            _duration_verdict(preset, duration) if duration is not None else (False, None)
        )
        entry: dict[str, Any] = {
            "platform": platform,
            "preset_version": preset["preset_version"],
            "duration_s": duration,
            "duration_ok": duration_ok,
            "requires_reoptimization": not duration_ok,
            "recommended_duration_s": recommended,
            "truncated": False,
            "rule_verification": SCHEMA_RULE_VERIFICATION,
            "compliance": None,
        }
        if duration_ok and metadata is not None:
            entry["compliance"] = evaluate_export(
                platform,
                metadata,
                subtitle_rect=subtitle_rect,
                face_rects=face_rects,
                cover=cover,
            ).to_dict()
        reports.append(entry)

    artifact_checked = metadata is not None
    return {
        "ok": (
            not problems
            and all(r["duration_ok"] for r in reports)
            and all(
                r["compliance"] is None or r["compliance"]["status"] == PASS
                for r in reports
            )
        ),
        "duration_s": duration,
        "scaling_rule": _beat_map.SCALING_RULE_ID,
        "truncated": False,
        "artifact_checked": artifact_checked,
        "artifact_gate": UNKNOWN if not artifact_checked else (
            PASS
            if all(
                r["compliance"] is None or r["compliance"]["status"] == PASS
                for r in reports
            )
            else FAIL
        ),
        "beat_map_problems": problems,
        "variants": reports,
        "human_confirmation_required": True,
    }


__all__ = [
    "CREATIVE_VARIANT_DURATION_S",
    "ExportCheck",
    "ExportError",
    "ExportReport",
    "PLATFORM_EXPORT_PRESETS",
    "SCHEMA_RULE_VERIFICATION",
    "TARGET_PLATFORMS",
    "build_duration_variant",
    "build_variants",
    "evaluate_duration_variant",
    "evaluate_export",
    "preset_for",
]