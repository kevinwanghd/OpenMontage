"""Cross-episode asset library adapter for the short-drama pipeline (LIB-01, F10).

The library is a thin adapter over three stores with deliberately separate
responsibilities:

- ``<library_dir>/<kind>/<asset_id>/asset.json`` — the **source of truth** for
  metadata, status, lineage and version history. Status changes (promote /
  retire) rewrite this file; nothing is ever deleted, and events are appended.
- ``<library_dir>/corpus/`` — a real :class:`lib.corpus.Corpus` used for
  text/tag retrieval. The corpus is append-only; retiring an asset is a
  tombstone (an ``asset.json`` status) filtered at query time, never a row
  delete, because deleting rows would break row<->embedding alignment.
- ``<library_dir>/identity/<embedder_model>/v1/`` — identity vectors (faces,
  voices, ...) in their own versioned store, keyed by embedder model. They are
  intentionally **not** written into the corpus's 512-d visual space: identity
  embeddings live in a different space and would poison visual retrieval.

``kind`` is restricted to ``{"video", "image"}`` so the corpus row model stays
valid (audio assets carry a metadata sidecar under one of those kinds).

M0: local files only, zero external API calls. Text search uses a deterministic
local bag-of-words hash embedder (``local-hash-v1``) as an offline placeholder
for CLIP; swapping in a real text embedder is a version bump, not an API change.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from lib.corpus import EMBED_DIM, ClipRecord, Corpus

KINDS = ("image", "video")
ASSET_VERSION = "1.0"
IDENTITY_STORE_VERSION = "v1"
TEXT_INDEX_MODEL = "local-hash-v1"

_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")
_TOKEN = re.compile(r"[a-z0-9]+")
_NUMERIC = (int, float)


class AssetLibraryError(Exception):
    """Base asset-library error."""


class AssetNotFoundError(AssetLibraryError, KeyError):
    """asset_id is not present in the library."""


class DuplicateAssetError(AssetLibraryError):
    """asset_id already exists; assets are never silently overwritten."""


class InvalidAssetError(AssetLibraryError):
    """Invalid kind / id / embedding / query combination."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_segment(value: str, field: str) -> str:
    """Reject ids that could escape their directory (path traversal)."""
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise InvalidAssetError(
            f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]*, got {value!r}")
    return value


def _model_slug(model: str) -> str:
    """Map an embedder model name to a safe directory segment.

    The original spelling is kept in the store manifest so a slug collision
    between two distinct models is detected instead of silently merged.
    """
    if not isinstance(model, str) or not model.strip():
        raise InvalidAssetError("embedder_model must be a non-empty string")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("._-")
    if not slug:
        raise InvalidAssetError(f"embedder_model {model!r} has no usable characters")
    return slug


def _as_unit_vector(value: Any) -> np.ndarray:
    """Validate a 1-D finite vector and L2-normalise it for cosine math."""
    vec = np.asarray(value, dtype=np.float32)
    if vec.ndim != 1:
        raise InvalidAssetError(f"embedding must be 1-D, got shape {vec.shape}")
    if vec.size == 0:
        raise InvalidAssetError("embedding must not be empty")
    if not np.all(np.isfinite(vec)):
        raise InvalidAssetError("embedding contains NaN or infinite values")
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        raise InvalidAssetError("embedding must not be the zero vector")
    return vec / norm


def _hash_text_embedding(text: str) -> np.ndarray:
    """Deterministic offline text embedding (signed feature hashing).

    Identical token bags produce identical vectors; shared tokens raise cosine
    similarity. This is the M0 placeholder for CLIP text embeddings and is
    explicitly versioned as ``local-hash-v1`` rather than pretending to be a
    semantic model.
    """
    vec = np.zeros(EMBED_DIM, dtype=np.float32)
    for token in _TOKEN.findall(text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % EMBED_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign
    norm = float(np.linalg.norm(vec))
    if norm:
        vec /= norm
    return vec


def _search_text(metadata: dict) -> str:
    """Flatten metadata into the corpus tag document for text retrieval.

    Keys are sorted and numeric vectors are skipped so the indexed text is
    deterministic and an inline embedding can never leak into the tag channel.
    """
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key in sorted(value):
                walk(value[key])
        elif isinstance(value, (list, tuple)):
            if value and len(value) > 8 and all(isinstance(v, _NUMERIC) for v in value):
                return  # looks like an inline embedding, not searchable text
            for item in value:
                walk(item)

    walk(metadata)
    return " ".join(parts)


def _as_tag_set(tags: Any) -> set[str]:
    if tags is None:
        return set()
    if isinstance(tags, str):
        tags = [tags]
    return {str(tag).strip().lower() for tag in tags}


def _asset_tag_set(asset: dict) -> set[str]:
    metadata = asset.get("metadata") or {}
    return _as_tag_set(metadata.get("tags"))


def _jsonable(value: Any, field: str) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise InvalidAssetError(f"{field} must be JSON-serialisable: {exc}") from exc
    return copy.deepcopy(value)


class AssetLibrary:
    """Single source of truth for cross-episode assets.

    Usage::

        lib = AssetLibrary(Path("library"))
        lib.register("image", "ch_ling", {"character_id": "ch_ling",
                      "tags": ["lead"]}, embedding=face_vec,
                      embedder_model="facenet512")
        lib.query("image", character_id="ch_ling")
        lib.query("image", vector=face_vec, embedder_model="facenet512")
    """

    _lock = threading.Lock()  # in-process single-writer discipline (ORC-10)

    def __init__(self, library_dir: Path):
        self.library_dir = Path(library_dir)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def corpus_dir(self) -> Path:
        return self.library_dir / "corpus"

    @property
    def identity_dir(self) -> Path:
        return self.library_dir / "identity"

    def _asset_path(self, kind: str, asset_id: str) -> Path:
        return self.library_dir / kind / asset_id / "asset.json"

    def _identity_store_dir(self, embedder_model: str) -> Path:
        return self.identity_dir / _model_slug(embedder_model) / IDENTITY_STORE_VERSION

    def _corpus(self) -> Corpus:
        corp = Corpus(self.corpus_dir)
        corp.load()
        return corp

    # ------------------------------------------------------------------
    # Asset JSON (source of truth)
    # ------------------------------------------------------------------

    def _read_asset(self, kind: str, asset_id: str) -> Optional[dict]:
        path = self._asset_path(kind, asset_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _find_asset(self, asset_id: str) -> Optional[dict]:
        for kind in KINDS:
            rec = self._read_asset(kind, asset_id)
            if rec is not None:
                return rec
        return None

    def _write_asset(self, record: dict) -> None:
        path = self._asset_path(record["kind"], record["asset_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _iter_assets(self, kind: str) -> Iterable[dict]:
        base = self.library_dir / kind
        if not base.is_dir():
            return
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / "asset.json"
            if path.is_file():
                yield json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Identity store (independent, versioned by embedder model)
    # ------------------------------------------------------------------

    def _load_identity_store(self, embedder_model: str) -> tuple[list[dict], np.ndarray]:
        store = self._identity_store_dir(embedder_model)
        index_path = store / "index.jsonl"
        embed_path = store / "embeddings.npy"
        manifest_path = store / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("embedder_model") != embedder_model:
                raise InvalidAssetError(
                    f"identity store slug collision: {manifest.get('embedder_model')!r} "
                    f"and {embedder_model!r} map to the same directory")
        if not index_path.is_file() or not embed_path.is_file():
            return [], np.zeros((0, 0), dtype=np.float32)

        records = []
        for line in index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        matrix = np.load(embed_path)
        if matrix.ndim != 2:
            matrix = matrix.reshape(0, 0)
        n = min(len(records), matrix.shape[0])
        return records[:n], matrix[:n]

    def _store_identity(
        self,
        asset_id: str,
        kind: str,
        embedding: Any,
        embedder_model: str,
    ) -> dict:
        vec = _as_unit_vector(embedding)
        records, matrix = self._load_identity_store(embedder_model)
        dim = int(vec.shape[0])
        if matrix.shape[0] and matrix.shape[1] != dim:
            raise InvalidAssetError(
                f"embedding dim {dim} does not match embedder_model "
                f"{embedder_model!r} store dim {matrix.shape[1]}")
        if any(rec.get("asset_id") == asset_id for rec in records):
            raise DuplicateAssetError(
                f"asset {asset_id!r} already has a vector under {embedder_model!r}")

        store = self._identity_store_dir(embedder_model)
        store.mkdir(parents=True, exist_ok=True)
        row = len(records)
        new_matrix = vec.reshape(1, -1) if row == 0 else np.vstack([matrix, vec.reshape(1, -1)])

        # Embeddings first, index second: index.jsonl is the source of truth and
        # load() truncates to the shorter length, so a crash leaves a harmless
        # trailing npy row rather than a dangling index entry.
        tmp = store / "embeddings.npy.tmp"
        with open(tmp, "wb") as fh:
            np.save(fh, new_matrix.astype(np.float32))
        tmp.replace(store / "embeddings.npy")

        entry = {"asset_id": asset_id, "kind": kind, "row": row, "dim": dim,
                 "registered_at": _now()}
        with (store / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        manifest = {
            "store_version": IDENTITY_STORE_VERSION,
            "embedder_model": embedder_model,
            "dim": dim,
            "rows": row + 1,
            "updated_at": _now(),
        }
        (store / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "embedder_model": embedder_model,
            "store_version": IDENTITY_STORE_VERSION,
            "dim": dim,
            "row": row,
            "path": (store / "embeddings.npy").relative_to(self.library_dir).as_posix(),
        }

    # ------------------------------------------------------------------
    # Corpus index (text/tag retrieval only; visual channel untouched)
    # ------------------------------------------------------------------

    def _index_in_corpus(self, record: dict) -> None:
        corp = self._corpus()
        asset_id = record["asset_id"]
        if corp.has(asset_id):
            return  # append-only and idempotent by id
        search_text = _search_text(record.get("metadata") or {})
        corp.add(
            ClipRecord(
                clip_id=asset_id,
                source="asset_library",
                source_id=asset_id,
                source_url="",
                local_path=f"{record['kind']}/{asset_id}",
                kind=record["kind"],
                source_tags=search_text,
                added_at=time.time(),
            ),
            # The asset library never writes into the corpus visual channel:
            # visual vectors come from clip_embedder, identity vectors live in
            # their own store. A zero row keeps alignment without pollution.
            np.zeros(EMBED_DIM, dtype=np.float32),
            _hash_text_embedding(search_text),
        )
        corp.save()

    # ------------------------------------------------------------------
    # Retrieval helpers
    # ------------------------------------------------------------------

    def _text_scores(self, kind: str, text: str) -> list[tuple[str, float]]:
        corp = self._corpus()
        ranked = corp.rank_by_text(
            _hash_text_embedding(text),
            k=max(1, len(corp.records)),
            tag_weight=1.0,  # tag channel only; visual channel is intentionally zero
            kind=kind,
        )
        scored = [(rec.clip_id, float(score)) for rec, score in ranked]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    def _vector_scores(
        self, kind: str, vector: Any, embedder_model: str
    ) -> list[tuple[str, float]]:
        vec = _as_unit_vector(vector)
        records, matrix = self._load_identity_store(embedder_model)
        if not records:
            return []
        if matrix.shape[1] != vec.shape[0]:
            raise InvalidAssetError(
                f"query vector dim {vec.shape[0]} does not match "
                f"embedder_model {embedder_model!r} store dim {matrix.shape[1]}")
        sims = matrix @ vec
        scored = [
            (rec["asset_id"], float(sim))
            for rec, sim in zip(records, sims)
            if rec.get("kind") == kind
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    def _matches(
        self,
        asset: dict,
        *,
        character_id: Optional[str],
        tags: set[str],
        include_retired: bool,
    ) -> bool:
        if not include_retired and asset.get("status") == "retired":
            return False
        if character_id is not None:
            metadata = asset.get("metadata") or {}
            if str(metadata.get("character_id")) != str(character_id):
                return False
        if tags and not tags.issubset(_asset_tag_set(asset)):
            return False
        return True

    def _validate_kind(self, kind: str) -> None:
        if kind not in KINDS:
            raise InvalidAssetError(f"kind must be one of {KINDS}, got {kind!r}")

    # ------------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------------

    def register(
        self,
        kind: str,
        asset_id: str,
        metadata: dict,
        *,
        embedding=None,
        embedder_model=None,
        lineage=None,
    ) -> str:
        """Register a new asset. Returns ``asset_id``.

        ``embedding`` is an identity vector (face/voice/...), stored in the
        independent store keyed by ``embedder_model``; it is never mixed into
        the corpus visual space. Registration is append-only: re-registering an
        existing id raises instead of overwriting (NFR-10).
        """
        self._validate_kind(kind)
        _safe_segment(asset_id, "asset_id")
        if not isinstance(metadata, dict):
            raise InvalidAssetError("metadata must be a dict")
        if embedding is not None and not embedder_model:
            raise InvalidAssetError("embedding requires embedder_model")
        metadata = _jsonable(metadata, "metadata")
        if lineage is not None:
            lineage = _jsonable(lineage, "lineage")

        with self._lock:
            if self._find_asset(asset_id) is not None:
                raise DuplicateAssetError(f"asset {asset_id!r} already exists")

            record = {
                "version": ASSET_VERSION,
                "asset_id": asset_id,
                "kind": kind,
                "status": "active",
                "metadata": metadata,
                "lineage": lineage,
                "identity": None,
                "created_at": _now(),
                "updated_at": _now(),
                "events": [{"event": "register", "at": _now()}],
            }
            if embedding is not None:
                record["identity"] = self._store_identity(
                    asset_id, kind, embedding, embedder_model)
            self._write_asset(record)
            self._index_in_corpus(record)

        return asset_id

    def query(
        self,
        kind: str,
        *,
        character_id=None,
        tags=None,
        text=None,
        vector=None,
        embedder_model=None,
        min_sim=0.0,
        include_retired=False,
    ) -> list[dict]:
        """Return matching assets, ranked by similarity when one is supplied.

        ``text`` searches the corpus tag channel (built-in ``local-hash-v1``);
        ``vector`` searches the identity store for the given ``embedder_model``.
        Without either, results are metadata/tag matches ordered by asset_id.
        Retired assets are excluded unless ``include_retired=True``.
        """
        self._validate_kind(kind)
        if text is not None and vector is not None:
            raise InvalidAssetError("pass either text or vector, not both")
        if vector is not None and not embedder_model:
            raise InvalidAssetError("vector query requires embedder_model")
        if text is not None and embedder_model is not None:
            raise InvalidAssetError(
                "embedder_model applies to vector queries only; "
                f"text search always uses {TEXT_INDEX_MODEL}")
        requested_tags = _as_tag_set(tags)

        if vector is not None:
            candidates = self._vector_scores(kind, vector, embedder_model)
        elif text is not None:
            candidates = self._text_scores(kind, text)
        else:
            candidates = [(asset["asset_id"], None) for asset in self._iter_assets(kind)]
            candidates.sort(key=lambda item: item[0])

        results: list[dict] = []
        for asset_id, score in candidates:
            asset = self._read_asset(kind, asset_id)
            if asset is None:
                continue  # index drift: asset.json is the truth
            if not self._matches(
                asset,
                character_id=character_id,
                tags=requested_tags,
                include_retired=include_retired,
            ):
                continue
            if score is not None and score < min_sim:
                continue
            item = copy.deepcopy(asset)
            if score is not None:
                item["score"] = score
            results.append(item)
        return results

    def promote(self, asset_id: str) -> None:
        """Mark a candidate asset as reusable (LIB-04). Idempotent."""
        _safe_segment(asset_id, "asset_id")
        with self._lock:
            record = self._find_asset(asset_id)
            if record is None:
                raise AssetNotFoundError(asset_id)
            if record.get("status") == "retired":
                raise AssetLibraryError(f"cannot promote retired asset {asset_id!r}")
            if record.get("status") == "promoted":
                return
            now = _now()
            record["status"] = "promoted"
            record["promoted_at"] = now
            record["updated_at"] = now
            record.setdefault("events", []).append({"event": "promote", "at": now})
            self._write_asset(record)

    def retire(self, asset_id: str) -> None:
        """Tombstone an asset: keep every byte, exclude it from default queries."""
        _safe_segment(asset_id, "asset_id")
        with self._lock:
            record = self._find_asset(asset_id)
            if record is None:
                raise AssetNotFoundError(asset_id)
            if record.get("status") == "retired":
                return
            now = _now()
            record["prior_status"] = record.get("status", "active")
            record["status"] = "retired"
            record["retired_at"] = now
            record["updated_at"] = now
            record.setdefault("events", []).append({"event": "retire", "at": now})
            self._write_asset(record)

    def lineage(self, asset_id: str) -> dict:
        """Return origin/provenance for an asset (LIB-03).

        The registered lineage dict is returned flattened, with ``asset_id``,
        ``kind``, ``derived_from`` and ``used_in`` present as defaults so
        callers never have to special-case unknown provenance.
        """
        _safe_segment(asset_id, "asset_id")
        record = self._find_asset(asset_id)
        if record is None:
            raise AssetNotFoundError(asset_id)
        out = copy.deepcopy(record.get("lineage") or {})
        out.setdefault("asset_id", asset_id)
        out.setdefault("kind", record["kind"])
        out.setdefault("derived_from", [])
        out.setdefault("used_in", [])
        return out

    def get(self, asset_id: str) -> dict:
        """Return the full asset record (including retired assets)."""
        _safe_segment(asset_id, "asset_id")
        record = self._find_asset(asset_id)
        if record is None:
            raise AssetNotFoundError(asset_id)
        return copy.deepcopy(record)