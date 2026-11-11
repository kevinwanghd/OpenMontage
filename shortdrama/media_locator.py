"""Media path resolution with root confinement (UI-02, §10.1).

Serves three surfaces without trusting caller-supplied paths:

- ``/media/{episode}/{path}`` and ``/thumb/{episode}/{path}`` —
  confined to the episode/project directory under ``projects_dir``;
- ``/media/library/{kind}/{asset_id}/{path}`` — confined to
  ``library_dir/<kind>/<asset_id>``.

Traversal, absolute paths, drive letters, ADS colons and symlink escapes are
rejected before a file is opened. M0 is local-only and read-only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from shortdrama.errors import ApiError, ErrorCode

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _forbidden(what: str, context: dict) -> ApiError:
    return ApiError(
        ErrorCode.MEDIA_PATH_FORBIDDEN,
        what,
        "pass a relative path inside the episode/library root",
        recovery="fix-then-retry",
        context=context,
        http_status=403,
    )


def _segment(value: str, label: str) -> str:
    """Validate one path segment (episode id, kind, asset id)."""
    if (
        not value
        or value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or ":" in value
    ):
        raise _forbidden(f"{label} '{value}' is not a single safe path segment", {label: value})
    return value


def _relative_parts(rel_path: str) -> Path:
    """Validate a relative media path and return it as a relative Path."""
    if not rel_path or not isinstance(rel_path, str) or "\x00" in rel_path:
        raise _forbidden(f"invalid media path '{rel_path}'", {"path": rel_path})
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise _forbidden(f"absolute media path '{rel_path}' is not allowed", {"path": rel_path})
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        raise _forbidden(f"empty media path '{rel_path}'", {"path": rel_path})
    if any(part == ".." or ":" in part for part in parts):
        raise _forbidden(f"media path '{rel_path}' escapes its root", {"path": rel_path})
    return Path(*parts)


class MediaLocator:
    """Resolve and confine media paths for the API."""

    def __init__(self, projects_dir: Path, library_dir: Optional[Path] = None) -> None:
        self.projects_dir = Path(projects_dir).resolve()
        self.library_dir = Path(library_dir).resolve() if library_dir else self.projects_dir / "_library"

    # --- roots ------------------------------------------------------------

    def episode_root(self, episode_id: str) -> Path:
        _segment(episode_id, "episode_id")
        direct = self.projects_dir / episode_id
        if direct.is_dir():
            return direct
        if self.projects_dir.is_dir():
            for project_dir in sorted(p for p in self.projects_dir.iterdir() if p.is_dir()):
                candidate = project_dir / episode_id
                if candidate.is_dir():
                    return candidate
        manifest = self._manifest_for(episode_id)
        if manifest:
            path, data = manifest
            sibling = path.parent / data.get("episode_id", "")
            return sibling if sibling.is_dir() else path.parent
        raise ApiError(
            ErrorCode.MEDIA_NOT_FOUND,
            f"episode '{episode_id}' has no media root",
            "check the episode id and that its project directory exists",
            context={"episode_id": episode_id},
            http_status=404,
        )

    def library_asset_root(self, kind: str, asset_id: str) -> Path:
        _segment(kind, "kind")
        _segment(asset_id, "asset_id")
        return self.library_dir / kind / asset_id

    # --- locate -----------------------------------------------------------

    def locate_episode(self, episode_id: str, rel_path: str, *, must_exist: bool = True) -> Path:
        return self._confine(self.episode_root(episode_id), rel_path, kind="episode", must_exist=must_exist)

    def locate_thumbnail(self, episode_id: str, rel_path: str, *, must_exist: bool = True) -> Path:
        # M0 reserves the thumbnail surface; it resolves against the same root.
        return self.locate_episode(episode_id, rel_path, must_exist=must_exist)

    def locate_library(self, kind: str, asset_id: str, rel_path: str, *, must_exist: bool = True) -> Path:
        return self._confine(
            self.library_asset_root(kind, asset_id), rel_path, kind="library", must_exist=must_exist
        )

    # --- internals --------------------------------------------------------

    @staticmethod
    def _confine(root: Path, rel_path: str, *, kind: str, must_exist: bool) -> Path:
        rel = _relative_parts(rel_path)
        root_resolved = root.resolve()
        target = (root_resolved / rel).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            raise _forbidden(
                f"media path '{rel_path}' escapes the {kind} root",
                {"path": rel_path, "root": str(root_resolved)},
            )
        if must_exist and not target.is_file():
            raise ApiError(
                ErrorCode.MEDIA_NOT_FOUND,
                f"media '{rel_path}' not found in {kind} root",
                "check the media path; generated assets appear after the assets stage",
                recovery="do-X-first",
                context={"path": rel_path},
                http_status=404,
            )
        return target

    def _manifest_for(self, episode_id: str) -> Optional[tuple[Path, dict]]:
        if not self.projects_dir.is_dir():
            return None
        paths = sorted(self.projects_dir.glob("*/episode_manifest.json"))
        paths += sorted(self.projects_dir.glob("*/*/episode_manifest.json"))
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("episode_id") == episode_id:
                return path, data
        return None