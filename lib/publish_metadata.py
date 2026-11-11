"""DEL-06 per-episode publish metadata package (title / tags / cover / time).

Every episode (and every platform variant) gets one explicit package. A package
is ``READY`` only when all required metadata is present and well-formed; missing
fields are ``INCOMPLETE`` (listed, never invented), invalid values are
``BLOCKED``. Cover geometry is checked against the DEL-02 export preset.

Important scope limit: a complete metadata package is **not** permission to
publish. Platform-rule confirmation (DEL-09), content compliance (DEL-04) and
account/monetization eligibility (A6) remain UNKNOWN/UNCONFIRMED here and are
carried as explicit fields so no caller can mistake this for a publish gate.

M0: local metadata only -- no platform API, no upload, no scheduling service.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from lib.export_presets import SCHEMA_RULE_VERIFICATION, preset_for

UNKNOWN = "UNKNOWN"

STATUS_READY = "READY"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_BLOCKED = "BLOCKED"

REQUIRED_FIELDS = ("title", "tags", "cover", "publish_at")


class PublishMetadataError(ValueError):
    """A publish metadata package request is malformed."""


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_tags(tags: Any, max_tags: int) -> tuple[list[str], list[str], list[str]]:
    """Return (tags, missing, issues) without inventing any tag."""
    if tags is None:
        return [], ["tags"], []
    if isinstance(tags, str):
        raw = [tags]
    elif isinstance(tags, Iterable):
        raw = list(tags)
    else:
        return [], [], ["tags must be a list of strings"]

    out: list[str] = []
    seen: set[str] = set()
    issues: list[str] = []
    for tag in raw:
        if not isinstance(tag, str):
            issues.append(f"tag {tag!r} is not a string")
            continue
        cleaned = tag.strip().lstrip("#").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    if not out:
        return [], ["tags"], issues
    if len(out) > max_tags:
        issues.append(f"too many tags: {len(out)} > {max_tags}")
    return out, [], issues


def _normalize_cover(cover: Any, spec: Mapping[str, Any], platform: str) -> tuple[Optional[dict[str, Any]], list[str], list[str]]:
    if cover is None:
        return None, ["cover"], []
    if not isinstance(cover, Mapping):
        return None, [], [f"cover must be a mapping, got {type(cover).__name__}"]
    missing: list[str] = []
    issues: list[str] = []
    width, height = cover.get("width"), cover.get("height")
    if width is None or height is None:
        missing.append("cover.width/height")
    else:
        try:
            ok = (float(width), float(height)) == (float(spec["width"]), float(spec["height"]))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            issues.append(
                f"cover {width}x{height} does not match {platform} preset "
                f"{spec['width']}x{spec['height']}"
            )
    if not str(cover.get("path") or "").strip() and cover.get("frame_time_s") is None:
        missing.append("cover.path or cover.frame_time_s")
    if cover.get("frame_time_s") is not None:
        frame = cover.get("frame_time_s")
        if isinstance(frame, bool) or not isinstance(frame, (int, float)) or frame < 0:
            issues.append("cover.frame_time_s must be a non-negative number")
    normalized = {
        "width": width,
        "height": height,
        "aspect_ratio": spec["aspect_ratio"],
        "path": str(cover.get("path") or "") or None,
        "frame_time_s": cover.get("frame_time_s"),
        "caption": str(cover.get("caption") or "") or None,
    }
    return normalized, missing, issues


def build_publish_package(
    episode_id: str,
    platform: str,
    *,
    title: Any = None,
    tags: Any = None,
    cover: Any = None,
    publish_at: Any = None,
    account: Optional[str] = None,
    description: Optional[str] = None,
    language: str = "en",
    timezone_name: Optional[str] = None,
    ai_disclosure: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build one DEL-06 metadata package; never fabricates missing fields."""
    episode = str(episode_id or "").strip()
    if not episode:
        raise PublishMetadataError("episode_id is required")
    try:
        spec = preset_for(platform)
    except Exception as exc:  # ExportError
        raise PublishMetadataError(f"unknown platform preset: {platform!r}") from exc

    missing: list[str] = []
    issues: list[str] = []

    title_text = str(title).strip() if title is not None else ""
    if not title_text:
        missing.append("title")
    elif len(title_text) > int(spec["title_max_chars"]):
        issues.append(
            f"title too long: {len(title_text)} > {spec['title_max_chars']} "
            f"for {platform}"
        )

    normalized_tags, tag_missing, tag_issues = _normalize_tags(tags, int(spec["hashtag_max"]))
    missing.extend(tag_missing)
    issues.extend(tag_issues)

    normalized_cover, cover_missing, cover_issues = _normalize_cover(cover, spec["cover"], platform)
    missing.extend(cover_missing)
    issues.extend(cover_issues)

    publish_text = str(publish_at).strip() if publish_at is not None else ""
    if not publish_text:
        missing.append("publish_at")
    elif _parse_timestamp(publish_text) is None:
        issues.append("publish_at must be an ISO-8601 timestamp")

    if account is not None and not str(account).strip():
        missing.append("account")

    if issues:
        status = STATUS_BLOCKED
    elif missing:
        status = STATUS_INCOMPLETE
    else:
        status = STATUS_READY

    return {
        "version": "1.0",
        "episode_id": episode,
        "platform": platform,
        "preset_version": spec["preset_version"],
        "account": str(account).strip() if account is not None else None,
        "title": title_text or None,
        "title_max_chars": spec["title_max_chars"],
        "description": str(description).strip() if description else None,
        "tags": normalized_tags,
        "hashtag_max": spec["hashtag_max"],
        "cover": normalized_cover,
        "publish_at": publish_text or None,
        "timezone": timezone_name,
        "language": language,
        "ai_disclosure": dict(ai_disclosure) if isinstance(ai_disclosure, Mapping) else UNKNOWN,
        "rule_verification": SCHEMA_RULE_VERIFICATION,
        "compliance_status": UNKNOWN,
        "pre_publish_status": UNKNOWN,
        "monetization_eligibility": "UNVERIFIED",
        "status": status,
        "metadata_complete": status == STATUS_READY,
        # A complete metadata package is not a publish approval (DEL-09/A6).
        "publish_ready": False,
        "missing": missing,
        "issues": issues,
        "human_confirmation_required": True,
    }


def build_episode_packages(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build packages from supplied per-episode metadata entries.

    Each entry is ``{"episode_id", "platform", "title", "tags", "cover",
    "publish_at", ...}`` plus optional passthrough fields. Duplicate
    ``(episode_id, platform)`` pairs are reported, not silently merged.
    """
    packages: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            issues.append(f"[{index}] entry must be a mapping")
            continue
        episode_id = str(entry.get("episode_id") or "").strip()
        platform = str(entry.get("platform") or "").strip()
        if not episode_id or not platform:
            issues.append(f"[{index}] episode_id and platform are required")
            continue
        key = (episode_id, platform)
        if key in seen:
            issues.append(f"[{index}] duplicate package for {episode_id}/{platform}")
            continue
        seen.add(key)
        package = build_publish_package(
            episode_id,
            platform,
            title=entry.get("title"),
            tags=entry.get("tags"),
            cover=entry.get("cover"),
            publish_at=entry.get("publish_at"),
            account=entry.get("account"),
            description=entry.get("description"),
            language=str(entry.get("language", "en")),
            timezone_name=entry.get("timezone"),
            ai_disclosure=entry.get("ai_disclosure"),
        )
        packages.append(package)
    return {
        "ok": not issues and all(p["status"] == STATUS_READY for p in packages),
        "packages": packages,
        "issues": issues,
    }


def build_release_schedule(
    packages: Iterable[Mapping[str, Any]],
    *,
    cadence_hours: Optional[float] = None,
) -> dict[str, Any]:
    """Order packages by publish time and flag collisions for one platform.

    The same episode may legitimately ship to several platforms at once, so a
    duplicate timestamp is only a problem for two *different* episodes on the
    same platform (where it splits the audience).
    """
    issues: list[str] = []
    rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for package in packages:
        platform = str(package.get("platform") or "")
        episode = str(package.get("episode_id") or "")
        moment = _parse_timestamp(package.get("publish_at"))
        if not platform or not episode:
            issues.append("package missing episode_id/platform")
            continue
        key = (episode, platform)
        if key in seen_keys:
            issues.append(f"duplicate package for {episode}/{platform}")
            continue
        seen_keys.add(key)
        if package.get("status") != STATUS_READY:
            issues.append(f"{episode}/{platform}: package status {package.get('status')}")
        if moment is None:
            issues.append(f"{episode}/{platform}: publish_at missing/invalid")
            continue
        rows.append(
            {
                "episode_id": episode,
                "platform": platform,
                "publish_at": package.get("publish_at"),
                "_moment": moment,
                "title": package.get("title"),
            }
        )

    rows.sort(key=lambda row: (row["_moment"], row["platform"], row["episode_id"]))

    collisions: set[tuple[str, str]] = set()
    for i, row in enumerate(rows):
        for other in rows[i + 1 :]:
            if other["_moment"] != row["_moment"]:
                break
            if (
                other["platform"] == row["platform"]
                and other["episode_id"] != row["episode_id"]
            ):
                pair = tuple(sorted((row["episode_id"], other["episode_id"])))
                collisions.add((row["platform"], pair[0] + "|" + pair[1]))
    for platform, pair in sorted(collisions):
        issues.append(f"{platform}: publish time collision between {pair.replace('|', ' and ')}")

    gaps: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        gaps.append((current["_moment"] - previous["_moment"]).total_seconds())
    if cadence_hours is not None:
        minimum_gap = float(cadence_hours) * 3600.0
        if any(gap < minimum_gap for gap in gaps):
            issues.append(f"gap below requested cadence of {cadence_hours}h")

    schedule = [
        {key: value for key, value in row.items() if key != "_moment"} for row in rows
    ]
    return {
        "ok": not issues,
        "schedule": schedule,
        "issues": issues,
        "cadence_hours": cadence_hours,
        "gaps_seconds": gaps,
        "rule_verification": SCHEMA_RULE_VERIFICATION,
    }


def save_publish_manifest(path: Path | str, packages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Persist the packages in a stable, publish-time-ordered manifest."""
    document = build_release_schedule(packages)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "ok": document["ok"],
        "issues": document["issues"],
        "packages": list(packages),
        "schedule": document["schedule"],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def load_publish_manifest(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise PublishMetadataError(f"publish manifest not found: {target}")
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise PublishMetadataError("publish manifest root must be an object")
    return dict(document)


__all__ = [
    "PublishMetadataError",
    "REQUIRED_FIELDS",
    "STATUS_BLOCKED",
    "STATUS_INCOMPLETE",
    "STATUS_READY",
    "UNKNOWN",
    "build_episode_packages",
    "build_publish_package",
    "build_release_schedule",
    "load_publish_manifest",
    "save_publish_manifest",
]