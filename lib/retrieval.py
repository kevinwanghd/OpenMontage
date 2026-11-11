"""Library retrieval parameters + graceful degradation (LIB-05, D-1).

:class:`lib.asset_library.AssetLibrary` answers a single channel at a time
(text, identity vector, or metadata). This module is the thin contract layer
above it that the API/CLI expose:

- **One parameter object** for the D-1 contract
  (``text`` / ``image`` / ``character_id`` / ``min_sim`` / ``reason_code`` /
  ``page`` / ``page_size``), validated in one place instead of once per caller.
- **Paginated results** with ``total``/``page``/``page_size`` so the HTTP layer
  and CLI share the same shape.
- **Image search degradation.** An image query needs a CLIP-like embedder. On
  this machine (Arc 140T, CPU only; R6) the visual embedder can be too slow or
  missing. Rather than failing the query, the retriever measures the embed and
  falls back to tag retrieval (filename tokens + ``text``) with an explicit
  ``degraded`` flag and reason — no silent empty result.
- **Measured performance baseline** (NFR-04): every search records elapsed
  milliseconds and, for image queries, embed milliseconds, so a real baseline
  is a machine-checkable number rather than a claim.

M0: zero external APIs. The image embedder is injected; the default is "not
configured", which degrades to tag retrieval.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import numpy as np

from lib.asset_library import AssetLibrary, InvalidAssetError

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
DEFAULT_MAX_EMBED_SECONDS = 1.0

# Degradation reason codes (stable strings; surfaced to CLI/API).
DEGRADED_EMBEDDER_UNAVAILABLE = "image_embedder_unavailable"
DEGRADED_EMBEDDER_SLOW = "image_embedder_slow"
DEGRADED_EMBEDDER_FAILED = "image_embedder_failed"
DEGRADED_EMBEDDING_INVALID = "image_embedding_invalid"

ImageEmbedder = Callable[[Sequence[str]], np.ndarray]


def _filename_tags(path: Path) -> str:
    """Best-effort tag hint from an image filename for the degraded path.

    ``warm_kitchen_plate.png`` -> ``warm kitchen plate``. This is a real
    user-supplied signal (the filename), not an invented description, and it
    only feeds the tag channel — the visual channel is never faked.
    """
    stem = path.stem
    for sep in ("-", "_", "."):
        stem = stem.replace(sep, " ")
    return " ".join(stem.lower().split())


def _is_embedding(value: Any) -> bool:
    if isinstance(value, np.ndarray):
        return True
    return isinstance(value, (list, tuple)) and bool(value) and all(
        isinstance(item, (int, float)) for item in value
    )


@dataclass(frozen=True)
class LibraryQuery:
    """The D-1 library query contract (LIB-05).

    ``image`` accepts either a path (embedded through the injected embedder) or
    a precomputed 1-D embedding (requires ``embedder_model`` so the vector is
    compared against the matching identity store). ``text`` and ``image`` may
    be combined; scores are fused by taking the best channel score per asset.
    """

    text: Optional[str] = None
    image: Any = None
    character_id: Optional[str] = None
    min_sim: float = 0.0
    reason_code: Optional[str] = None
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    tags: Optional[Sequence[str]] = None
    include_retired: bool = False
    embedder_model: Optional[str] = None

    def validate(self) -> None:
        if not isinstance(self.page, int) or self.page < 1:
            raise InvalidAssetError(f"page must be an integer >= 1, got {self.page!r}")
        if (
            not isinstance(self.page_size, int)
            or self.page_size < 1
            or self.page_size > MAX_PAGE_SIZE
        ):
            raise InvalidAssetError(
                f"page_size must be an integer in [1, {MAX_PAGE_SIZE}], "
                f"got {self.page_size!r}")
        if not isinstance(self.min_sim, (int, float)) or not 0.0 <= float(self.min_sim) <= 1.0:
            raise InvalidAssetError(
                f"min_sim must be a number in [0, 1], got {self.min_sim!r}")
        if self.text is not None and not isinstance(self.text, str):
            raise InvalidAssetError("text must be a string")
        if self.character_id is not None and not isinstance(self.character_id, str):
            raise InvalidAssetError("character_id must be a string")
        if self.reason_code is not None and not isinstance(self.reason_code, str):
            raise InvalidAssetError("reason_code must be a string")
        if self.image is not None:
            if not _is_embedding(self.image) and not isinstance(self.image, (str, Path)):
                raise InvalidAssetError("image must be a path or a 1-D embedding")
            if not self.embedder_model:
                raise InvalidAssetError(
                    "image search requires embedder_model so the query vector "
                    "is compared against the matching store")
        if isinstance(self.image, (list, tuple, np.ndarray)) and not _is_embedding(self.image):
            raise InvalidAssetError("image embedding must be a non-empty 1-D numeric array")


@dataclass
class RetrievalResult:
    """Paginated retrieval response with explicit degradation metadata."""

    items: list[dict] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    mode: str = "metadata"
    degraded: bool = False
    degraded_reason: Optional[str] = None
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "mode": self.mode,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "elapsed_ms": self.elapsed_ms,
        }


class LibraryRetriever:
    """Parameter validation, channel fusion, pagination and degradation.

    Usage::

        retriever = LibraryRetriever(library)
        result = retriever.search("video", text="rain window", page_size=10)
        slow = retriever.search("video", image="warm_kitchen.png",
                                embedder_model="clip-vit-b32")
        slow.degraded  # True, degraded_reason == "image_embedder_unavailable"
    """

    def __init__(
        self,
        library: AssetLibrary,
        *,
        image_embedder: Optional[ImageEmbedder] = None,
        max_embed_seconds: float = DEFAULT_MAX_EMBED_SECONDS,
    ):
        if max_embed_seconds < 0:
            raise InvalidAssetError("max_embed_seconds must be >= 0")
        self.library = library
        self.image_embedder = image_embedder
        self.max_embed_seconds = float(max_embed_seconds)
        # Measured baselines (NFR-04 / R6).
        self.searches = 0
        self.total_elapsed_ms = 0.0
        self.last_elapsed_ms = 0.0
        self.last_embed_ms: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        kind: str,
        params: Optional[LibraryQuery] = None,
        **kwargs: Any,
    ) -> RetrievalResult:
        """Run one library query and return a paginated, explicit result."""
        if params is not None and kwargs:
            raise InvalidAssetError("pass a LibraryQuery or keyword fields, not both")
        if params is None:
            params = LibraryQuery(**kwargs)
        params.validate()

        started = time.perf_counter()
        self.last_embed_ms = None
        mode, degraded, degraded_reason, fallback_text = self._plan(params)

        texts = params.text
        if degraded and not texts:
            texts = fallback_text

        scores: dict[str, Optional[float]] = {}
        vector = self._pending_vector
        self._pending_vector = None

        if texts:
            self._merge_channel(scores, kind, text=texts, params=params)
        if vector is not None:
            self._merge_channel(
                scores, kind, vector=vector, params=params,
                embedder_model=params.embedder_model)
        if texts is None and vector is None:
            self._merge_channel(scores, kind, params=params)

        items = self._filter_and_rank(kind, scores, params)
        total = len(items)
        start = (params.page - 1) * params.page_size
        page_items = items[start:start + params.page_size]

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.searches += 1
        self.total_elapsed_ms += elapsed_ms
        self.last_elapsed_ms = elapsed_ms
        return RetrievalResult(
            items=page_items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            mode=mode,
            degraded=degraded,
            degraded_reason=degraded_reason,
            elapsed_ms=round(elapsed_ms, 3),
        )

    def stats(self) -> dict[str, Any]:
        """Measured retrieval baseline (NFR-04): counts and wall-clock."""
        return {
            "searches": self.searches,
            "total_elapsed_ms": round(self.total_elapsed_ms, 3),
            "last_elapsed_ms": round(self.last_elapsed_ms, 3),
            "last_embed_ms": (
                round(self.last_embed_ms, 3)
                if self.last_embed_ms is not None else None
            ),
            "image_embedder": "configured" if self.image_embedder else "absent",
            "max_embed_seconds": self.max_embed_seconds,
        }

    # ------------------------------------------------------------------
    # Planning: channel choice + image degradation
    # ------------------------------------------------------------------

    def _plan(
        self, params: LibraryQuery
    ) -> tuple[str, bool, Optional[str], Optional[str]]:
        """Decide the retrieval mode and, for images, resolve the vector.

        Returns ``(mode, degraded, degraded_reason, fallback_text)`` and stashes
        any resolved image vector on ``self._pending_vector`` for ``search``.
        """
        self._pending_vector = None
        if params.image is None:
            if params.text:
                return "text", False, None, None
            return "metadata", False, None, None

        vector, degraded, reason, fallback_text = self._resolve_image(
            params.image, params.embedder_model)
        if vector is not None:
            self._pending_vector = vector
            mode = "hybrid" if params.text else "image"
            return mode, False, None, None

        # Image channel unavailable/too slow: degrade to the tag channel.
        return "tags", True, reason or DEGRADED_EMBEDDER_UNAVAILABLE, fallback_text

    def _resolve_image(
        self, image: Any, embedder_model: Optional[str]
    ) -> tuple[Optional[np.ndarray], bool, Optional[str], Optional[str]]:
        if _is_embedding(image):
            return np.asarray(image, dtype=np.float32), False, None, None

        path = Path(image)
        fallback = _filename_tags(path)
        if self.image_embedder is None:
            return None, True, DEGRADED_EMBEDDER_UNAVAILABLE, fallback

        embed_started = time.perf_counter()
        try:
            matrix = self.image_embedder([str(path)])
        except (OSError, ValueError, RuntimeError, ImportError):
            self.last_embed_ms = (time.perf_counter() - embed_started) * 1000.0
            return None, True, DEGRADED_EMBEDDER_FAILED, fallback
        self.last_embed_ms = (time.perf_counter() - embed_started) * 1000.0

        if self.last_embed_ms / 1000.0 > self.max_embed_seconds:
            # R6: CPU CLIP on this machine can exceed the budget; tag search
            # keeps the library usable instead of blocking on the embedder.
            return None, True, DEGRADED_EMBEDDER_SLOW, fallback

        array = np.asarray(matrix, dtype=np.float32)
        if array.ndim == 2:
            if array.shape[0] != 1:
                return None, True, DEGRADED_EMBEDDING_INVALID, fallback
            array = array[0]
        if array.ndim != 1 or array.size == 0 or not np.all(np.isfinite(array)):
            return None, True, DEGRADED_EMBEDDING_INVALID, fallback
        return array, False, None, None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def _merge_channel(
        self,
        scores: dict[str, Optional[float]],
        kind: str,
        *,
        params: LibraryQuery,
        text: Optional[str] = None,
        vector: Optional[np.ndarray] = None,
        embedder_model: Optional[str] = None,
    ) -> None:
        query_kwargs: dict[str, Any] = {
            "character_id": params.character_id,
            "tags": params.tags,
            "include_retired": params.include_retired,
        }
        if text is not None:
            query_kwargs["text"] = text
        if vector is not None:
            query_kwargs["vector"] = vector
            query_kwargs["embedder_model"] = embedder_model

        for asset in self.library.query(kind, **query_kwargs):
            asset_id = asset["asset_id"]
            score = asset.get("score")
            if score is not None:
                score = float(score)
                # A similarity channel is also a filter: zero/negative cosine
                # means "no shared signal", not "weak hit".
                if score <= 0.0:
                    continue
            existing = scores.get(asset_id)
            if existing is None or (score is not None and score > existing):
                scores[asset_id] = score

    def _filter_and_rank(
        self,
        kind: str,
        scores: dict[str, Optional[float]],
        params: LibraryQuery,
    ) -> list[dict]:
        items: list[dict] = []
        for asset_id, score in scores.items():
            asset = self.library.get(asset_id)
            if asset.get("kind") != kind:
                continue
            metadata = asset.get("metadata") or {}
            if params.reason_code is not None and \
                    str(metadata.get("reason_code")) != params.reason_code:
                continue
            effective = score
            if effective is None and params.min_sim > 0:
                # Metadata-only rows carry no similarity; when the caller asks
                # for a minimum, use the recorded similarity if one exists and
                # otherwise exclude the row rather than pretending it matched.
                recorded = metadata.get("similarity")
                effective = float(recorded) if isinstance(recorded, (int, float)) else None
            if params.min_sim > 0 and (effective is None or effective < params.min_sim):
                continue
            if score is not None:
                asset["score"] = score
            items.append(asset)

        def sort_key(item: dict) -> tuple[int, float, str]:
            score = item.get("score")
            if score is None:
                return (1, 0.0, item["asset_id"])
            return (0, -float(score), item["asset_id"])

        items.sort(key=sort_key)
        return items


def query_library(
    library: AssetLibrary, kind: str, **params: Any
) -> dict[str, Any]:
    """Convenience wrapper returning the plain dict shape the API consumes."""
    retriever = LibraryRetriever(library)
    return retriever.search(kind, LibraryQuery(**params)).as_dict()