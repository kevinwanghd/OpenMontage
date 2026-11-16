"""Music-library license enforcement (v2.5 AUD-10, D20).

sound_design may only use tracks registered in ``music_library/licenses.json``.
This module is the machine check behind that rule:

- the library index is structurally valid (required fields present, booleans);
- every music asset referenced by ``edit_decisions`` resolves to a registered
  track with ``commercial_use=true``;
- tracks with ``attribution_required=true`` carry their credit in the delivery
  rights text.

Pure local logic: no network, no spend. A missing library index is an error,
not an empty pass — "no music" must be an explicit decision, never a silent
fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

DEFAULT_LIBRARY_PATH = Path("music_library") / "licenses.json"

REQUIRED_TRACK_FIELDS = (
    "file",
    "title",
    "artist",
    "source",
    "license",
    "commercial_use",
    "attribution_required",
    "url",
    "downloaded",
)


class MusicLicenseError(ValueError):
    """The music library or a music reference cannot be trusted."""


def load(path: Optional[Path | str] = None) -> dict[str, Any]:
    """Load the library index; a missing file is a hard error (D20)."""
    library_path = Path(path) if path else DEFAULT_LIBRARY_PATH
    if not library_path.is_file():
        raise MusicLicenseError(
            f"music library index not found: {library_path} (D20: register tracks first)"
        )
    try:
        data = json.loads(library_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise MusicLicenseError(f"music library index is not valid JSON: {error}") from error
    if not isinstance(data, Mapping):
        raise MusicLicenseError("music library index must be a JSON object")
    return dict(data)


def validate_library(data: Mapping[str, Any]) -> list[str]:
    """Return structural problems; an empty list means the index is usable."""
    problems: list[str] = []
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        return ["licenses.json needs a tracks list"]
    seen: set[str] = set()
    for index, track in enumerate(tracks):
        if not isinstance(track, Mapping):
            problems.append(f"track #{index} must be an object")
            continue
        missing = [field for field in REQUIRED_TRACK_FIELDS if field not in track]
        if missing:
            problems.append(f"track #{index} missing {', '.join(missing)}")
            continue
        file_name = str(track.get("file") or "").strip()
        if not file_name:
            problems.append(f"track #{index} has an empty file")
            continue
        if file_name in seen:
            problems.append(f"track file '{file_name}' is registered twice")
        seen.add(file_name)
        for flag in ("commercial_use", "attribution_required"):
            if not isinstance(track.get(flag), bool):
                problems.append(f"track '{file_name}' {flag} must be a boolean")
    return problems


def _track_index(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(track.get("file")): track
        for track in data.get("tracks") or []
        if isinstance(track, Mapping) and track.get("file")
    }


def music_asset_ids(edit_decisions: Mapping[str, Any]) -> list[str]:
    """Collect music asset ids from audio.music (preferred) and legacy music."""
    ids: list[str] = []
    audio = edit_decisions.get("audio")
    if isinstance(audio, Mapping):
        music = audio.get("music")
        if isinstance(music, Mapping) and music.get("asset_id"):
            ids.append(str(music["asset_id"]))
    music = edit_decisions.get("music")
    if isinstance(music, Mapping) and music.get("asset_id"):
        ids.append(str(music["asset_id"]))
    return sorted(set(ids))


def check_edit_decisions(
    edit_decisions: Mapping[str, Any], library: Mapping[str, Any]
) -> list[str]:
    """Violations that block the cut: unregistered or non-commercial tracks."""
    index = _track_index(library)
    problems: list[str] = []
    for asset_id in music_asset_ids(edit_decisions):
        track = index.get(asset_id)
        if track is None:
            problems.append(f"music '{asset_id}' is not registered in licenses.json (D20)")
            continue
        if track.get("commercial_use") is not True:
            problems.append(
                f"music '{asset_id}' has commercial_use != true and cannot ship (AUD-10)"
            )
    return problems


def check_attribution(
    edit_decisions: Mapping[str, Any],
    library: Mapping[str, Any],
    rights_text: str,
) -> list[str]:
    """Every attribution_required track must appear in the delivery rights text."""
    index = _track_index(library)
    problems: list[str] = []
    for asset_id in music_asset_ids(edit_decisions):
        track = index.get(asset_id)
        if track is None or track.get("attribution_required") is not True:
            continue
        artist = str(track.get("artist") or "").strip()
        title = str(track.get("title") or "").strip()
        credit = f"{artist} - {title}".strip(" -")
        if not credit:
            # An empty credit must not silently satisfy the attribution check.
            problems.append(
                f"music '{asset_id}' requires attribution but has no artist/title "
                "to credit (AUD-10)"
            )
            continue
        if credit not in rights_text:
            problems.append(
                f"music '{asset_id}' requires attribution '{credit}' in the publish log (AUD-10)"
            )
    return problems


def audit(
    edit_decisions: Mapping[str, Any],
    library: Mapping[str, Any],
    rights_text: str = "",
) -> list[str]:
    """Full AUD-10 check: structural + registration + attribution."""
    return (
        validate_library(library)
        + check_edit_decisions(edit_decisions, library)
        + check_attribution(edit_decisions, library, rights_text)
    )