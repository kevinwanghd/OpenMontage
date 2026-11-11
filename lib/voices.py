"""Voice registry for the short-drama pipeline (AUD-09).

The requirements call for a voices table carrying ``voice_id`` / ``provider``
/ language / description / audition link, with registration and matching
queries that work across episodes. Persistence is delegated to
:class:`lib.asset_library.AssetLibrary`: audio assets are stored as a
metadata sidecar under the library's ``image`` kind (the library intentionally
restricts its top-level kinds to image/video so corpus rows stay valid), and
voice-specific fields live in the asset metadata under ``asset_type="voice"``.

M0: local metadata only. No TTS provider call, no network, no audio file is
written; an audition link is stored verbatim if the caller supplies one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from lib.asset_library import (
    AssetLibrary,
    AssetNotFoundError,
    DuplicateAssetError,
    InvalidAssetError,
)

ASSET_TYPE = "voice"
DEFAULT_STORAGE_KIND = "image"
DEFAULT_LANGUAGE = "en-US"

_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")


class VoiceError(Exception):
    """Base voice-registry error."""


class VoiceNotFoundError(VoiceError, KeyError):
    """voice_id is not registered."""


class DuplicateVoiceError(VoiceError):
    """voice_id already exists; voices are never silently overwritten."""


class InvalidVoiceError(VoiceError):
    """Malformed voice fields or storage kind."""


@dataclass(frozen=True)
class Voice:
    """A reusable voice-table row."""

    voice_id: str
    provider: str
    language: str
    description: str
    preview_url: Optional[str] = None
    tags: tuple[str, ...] = ()


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidVoiceError(f"{field} must be a non-empty string, got {value!r}")
    return value.strip()


def _normalize_tags(tags: Optional[Iterable[str]]) -> tuple[str, ...]:
    if tags is None:
        return ()
    if isinstance(tags, str):
        tags = [tags]
    try:
        return tuple(sorted({str(tag).strip().lower() for tag in tags if str(tag).strip()}))
    except TypeError as exc:
        raise InvalidVoiceError(f"tags must be an iterable of strings: {exc}") from exc


class VoiceRegistry:
    """Register and query reusable voices on top of the asset library.

    Usage::

        registry = VoiceRegistry(AssetLibrary(Path("library")))
        registry.register("v_nova_en", "elevenlabs", "en-US",
                          "warm, low narrator", preview_url="auditions/nova.mp3")
        registry.query(language="en-US", text="narrator")
    """

    def __init__(self, library: AssetLibrary, *, kind: str = DEFAULT_STORAGE_KIND):
        if kind not in ("image", "video"):
            raise InvalidVoiceError(
                f"voice storage kind must be 'image' or 'video', got {kind!r}")
        self.library = library
        self.kind = kind

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def register(
        self,
        voice_id: str,
        provider: str,
        language: str = DEFAULT_LANGUAGE,
        description: str = "",
        *,
        preview_url: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> str:
        """Register a voice row. Returns ``voice_id``; duplicates raise."""
        if not isinstance(voice_id, str) or not _SAFE_SEGMENT.fullmatch(voice_id):
            raise InvalidVoiceError(
                f"voice_id must match [A-Za-z0-9][A-Za-z0-9._-]*, got {voice_id!r}")
        provider = _require_text(provider, "provider")
        language = _require_text(language, "language")
        if not _LANGUAGE.fullmatch(language):
            raise InvalidVoiceError(
                f"language must be a BCP-47-ish code such as 'en-US', got {language!r}")
        description = _require_text(description, "description")
        if preview_url is not None:
            preview_url = _require_text(preview_url, "preview_url")

        metadata = {
            "asset_type": ASSET_TYPE,
            "voice_id": voice_id,
            "provider": provider,
            "language": language,
            "description": description,
            "preview_url": preview_url,
            "tags": list(_normalize_tags(tags)),
        }
        try:
            self.library.register(self.kind, voice_id, metadata)
        except DuplicateAssetError as exc:
            raise DuplicateVoiceError(str(exc)) from exc
        except InvalidAssetError as exc:
            raise InvalidVoiceError(str(exc)) from exc
        return voice_id

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _to_voice(self, record: dict) -> Optional[Voice]:
        metadata = record.get("metadata") or {}
        if metadata.get("asset_type") != ASSET_TYPE:
            return None
        return Voice(
            voice_id=metadata.get("voice_id") or record["asset_id"],
            provider=str(metadata.get("provider", "")),
            language=str(metadata.get("language", "")),
            description=str(metadata.get("description", "")),
            preview_url=metadata.get("preview_url"),
            tags=tuple(_normalize_tags(metadata.get("tags"))),
        )

    def get(self, voice_id: str) -> Voice:
        """Return one voice row (including retired ones)."""
        if not isinstance(voice_id, str) or not _SAFE_SEGMENT.fullmatch(voice_id):
            raise InvalidVoiceError(f"invalid voice_id {voice_id!r}")
        try:
            record = self.library.get(voice_id)
        except AssetNotFoundError as exc:
            raise VoiceNotFoundError(voice_id) from exc
        voice = self._to_voice(record)
        if voice is None:
            raise VoiceNotFoundError(voice_id)
        return voice

    def query(
        self,
        *,
        provider: Optional[str] = None,
        language: Optional[str] = None,
        text: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        include_retired: bool = False,
    ) -> list[Voice]:
        """Return matching voices, ranked by text relevance when supplied.

        ``text`` searches the asset-library corpus (deterministic local hash
        embedder); provider/language/tag fields are filtered exactly.
        Retired voices are excluded unless ``include_retired=True``.
        """
        provider = _require_text(provider, "provider") if provider is not None else None
        language = _require_text(language, "language") if language is not None else None
        requested_tags = set(_normalize_tags(tags))
        if text is not None:
            text = _require_text(text, "text")

        if text is not None:
            records = self.library.query(
                self.kind, text=text, include_retired=include_retired)
            records = [record for record in records if record.get("score", 0.0) > 0.0]
        else:
            records = self.library.query(
                self.kind, include_retired=include_retired)

        matches: list[tuple[int, Voice]] = []
        for order, record in enumerate(records):
            voice = self._to_voice(record)
            if voice is None:
                continue
            if provider is not None and voice.provider != provider:
                continue
            if language is not None and voice.language != language:
                continue
            if requested_tags and not requested_tags.issubset(set(voice.tags)):
                continue
            matches.append((order, voice))

        if text is None:
            matches.sort(key=lambda item: item[1].voice_id)
        else:
            # library.query already ordered by descending score; keep that
            # stable order while sorting ties by voice_id.
            matches.sort(key=lambda item: item[1].voice_id)
            matches.sort(key=lambda item: item[0])
        return [voice for _, voice in matches]

    def all(self, *, include_retired: bool = False) -> list[Voice]:
        """Return every registered voice (cross-episode reuse helper)."""
        return self.query(include_retired=include_retired)