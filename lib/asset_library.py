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
# LIB-02: the seven *semantic* asset tables. Unlike ``KINDS`` (the corpus media
# kinds used for video/image retrieval), these name what an asset *is*. They
# live under the same ``library/<kind>/<asset_id>/`` layout but are not corpus
# rows; :mod:`lib.asset_tables` layers them on top without changing this class.
SEMANTIC_KINDS = (
    "characters",
    "scenes",
    "props",
    "voices",
    "music",
    "shots",
    "rejections",
)
ALL_KINDS = KINDS + SEMANTIC_KINDS
ASSET_VERSION = "1.0"
IDENTITY_STORE_VERSION = "v1"
TEXT_INDEX_MODEL = "local-hash-v1"
SHOTS_INDEX_VERSION = "1.0"

# LIB-03: every lineage record must answer these questions. A field that does
# not apply, or that the provider did not return, is stored as ``None`` with an
# explicit reason in ``null_reasons`` — never omitted, never guessed.
LINEAGE_FIELDS = (
    "project",
    "episode",
    "source",
    "rights",
    "tool",
    "model",
    "generation_params",
)
LINEAGE_NULL_REASONS_KEY = "null_reasons"
NULL_REASON_NOT_REPORTED = "not_reported"
LINEAGE_TRACE_DEFAULT_DEPTH = 16

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

    @property
    def status_index_path(self) -> Path:
        """Append-only status journal (asset.json stays the source of truth)."""
        return self.library_dir / "index" / "status.jsonl"

    @property
    def shots_index_path(self) -> Path:
        """Append-only accepted-shot table for cross-episode reuse (LIB-04)."""
        return self.library_dir / "shots" / "index.jsonl"

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
    # Status journal (derived index, never a second source of truth)
    # ------------------------------------------------------------------

    def _append_status(
        self, record: dict, *, event: str, prior_status: Optional[str]
    ) -> None:
        """Append one status transition to ``index/status.jsonl``.

        ``asset.json`` remains the single source of truth; this journal is a
        derived, append-only convenience for cross-episode dashboards. Nothing
        here mutates corpus rows (LIB-04: status lives in asset.json + index,
        never as a corpus row mutation).
        """
        path = self.status_index_path
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "asset_id": record["asset_id"],
            "kind": record["kind"],
            "event": event,
            "status": record.get("status"),
            "prior_status": prior_status,
            "at": _now(),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def status_index(self) -> dict[str, dict]:
        """Latest status journal row per asset (empty before the first write)."""
        out: dict[str, dict] = {}
        path = self.status_index_path
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict) and "asset_id" in row:
                out[str(row["asset_id"])] = row
        return out

    # ------------------------------------------------------------------
    # Shots table (accepted whole shots, cross-episode reuse; LIB-04)
    # ------------------------------------------------------------------

    def _read_shots(self) -> list[dict]:
        path = self.shots_index_path
        if not path.is_file():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows

    def record_accepted_shot(
        self,
        *,
        shot_id: str,
        project: str,
        episode: str,
        asset_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        promote: bool = False,
    ) -> dict:
        """Append an accepted whole shot to the shots table (LIB-04).

        Idempotent by ``(project, episode, shot_id)``; re-recording returns the
        existing row instead of duplicating it. ``promote=True`` also moves the
        backing candidate asset to reusable status in ``asset.json``. The table
        is append-only and never touches the corpus.
        """
        _safe_segment(shot_id, "shot_id")
        _safe_segment(project, "project")
        _safe_segment(episode, "episode")
        if asset_id is not None:
            _safe_segment(asset_id, "asset_id")
        if metadata is not None:
            metadata = _jsonable(metadata, "metadata")

        with self._lock:
            if asset_id is not None and self._find_asset(asset_id) is None:
                raise AssetNotFoundError(asset_id)
            if promote and asset_id is not None:
                self._promote_locked(asset_id)

            for row in self._read_shots():
                if (
                    row.get("project") == project
                    and row.get("episode") == episode
                    and row.get("shot_id") == shot_id
                ):
                    return copy.deepcopy(row)

            row = {
                "table_version": SHOTS_INDEX_VERSION,
                "shot_id": shot_id,
                "project": project,
                "episode": episode,
                "asset_id": asset_id,
                "status": "accepted",
                "metadata": metadata or {},
                "recorded_at": _now(),
            }
            path = self.shots_index_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return copy.deepcopy(row)

    def list_shots(
        self,
        *,
        project: Optional[str] = None,
        episode: Optional[str] = None,
        exclude_episode: Optional[str] = None,
        status: Optional[str] = "accepted",
    ) -> list[dict]:
        """Read the shots table with optional filters, in registration order."""
        rows = []
        for row in self._read_shots():
            if project is not None and row.get("project") != project:
                continue
            if episode is not None and row.get("episode") != episode:
                continue
            if exclude_episode is not None and row.get("episode") == exclude_episode:
                continue
            if status is not None and row.get("status") != status:
                continue
            rows.append(copy.deepcopy(row))
        return rows

    def reusable_shots(
        self, project: str, *, exclude_episode: Optional[str] = None
    ) -> list[dict]:
        """Accepted shots from earlier episodes, reusable by the next one."""
        return self.list_shots(
            project=project, exclude_episode=exclude_episode, status="accepted"
        )

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
            if not isinstance(lineage, dict):
                raise InvalidAssetError("lineage must be a dict")
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
            self._append_status(record, event="register", prior_status=None)
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
            self._promote_locked(asset_id)

    def _promote_locked(self, asset_id: str) -> None:
        """Promote with the library lock already held."""
        record = self._find_asset(asset_id)
        if record is None:
            raise AssetNotFoundError(asset_id)
        if record.get("status") == "retired":
            raise AssetLibraryError(f"cannot promote retired asset {asset_id!r}")
        if record.get("status") == "promoted":
            return
        prior_status = record.get("status")
        now = _now()
        record["status"] = "promoted"
        record["promoted_at"] = now
        record["updated_at"] = now
        record.setdefault("events", []).append({"event": "promote", "at": now})
        self._write_asset(record)
        self._append_status(record, event="promote", prior_status=prior_status)

    def retire(self, asset_id: str) -> None:
        """Tombstone an asset: keep every byte, exclude it from default queries."""
        _safe_segment(asset_id, "asset_id")
        with self._lock:
            record = self._find_asset(asset_id)
            if record is None:
                raise AssetNotFoundError(asset_id)
            if record.get("status") == "retired":
                return
            prior_status = record.get("status", "active")
            now = _now()
            record["prior_status"] = prior_status
            record["status"] = "retired"
            record["retired_at"] = now
            record["updated_at"] = now
            record.setdefault("events", []).append({"event": "retire", "at": now})
            self._write_asset(record)
            self._append_status(record, event="retire", prior_status=prior_status)

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

    def lineage_record(self, asset_id: str) -> dict:
        """Full LIB-03 lineage with every applicable field explicit.

        Unlike :meth:`lineage`, this is the strict shape the contract tests and
        downstream UI read: every field in :data:`LINEAGE_FIELDS` is present.
        A field that does not apply, or that the provider did not return, is
        ``None`` and carries a reason in ``null_reasons`` — unknown values are
        never fabricated.
        """
        record = self.get(asset_id)
        raw = record.get("lineage") or {}
        reasons = raw.get(LINEAGE_NULL_REASONS_KEY) or {}
        out: dict[str, Any] = {
            "asset_id": asset_id,
            "kind": record["kind"],
            "version": record.get("version"),
            "status": record.get("status"),
            "derived_from": list(raw.get("derived_from") or []),
            "used_in": list(raw.get("used_in") or []),
            LINEAGE_NULL_REASONS_KEY: {},
        }
        for field in LINEAGE_FIELDS:
            value = raw.get(field)
            if value is None:
                out[field] = None
                reason = reasons.get(field) if isinstance(reasons, dict) else None
                out[LINEAGE_NULL_REASONS_KEY][field] = (
                    reason if isinstance(reason, str) and reason.strip()
                    else NULL_REASON_NOT_REPORTED
                )
            else:
                out[field] = copy.deepcopy(value)

        # Preserve additional caller-supplied lineage keys (e.g. shot, seed)
        # so provenance is not silently dropped.
        reserved = {"derived_from", "used_in", LINEAGE_NULL_REASONS_KEY}
        for key, value in raw.items():
            if key in reserved or key in LINEAGE_FIELDS:
                continue
            out[key] = copy.deepcopy(value)
        return out

    def trace_lineage(
        self, asset_id: str, *, max_depth: int = LINEAGE_TRACE_DEFAULT_DEPTH
    ) -> dict:
        """Walk ``derived_from`` links and report the source chain (LIB-03).

        Returns ``{"root", "chain", "missing"}``: ``chain`` is breadth-first
        (the asset itself first), each entry carrying the asset's version and
        provenance; ``missing`` lists referenced ids with no asset record.
        Cycles terminate at ``max_depth`` and visited-set deduplication.
        """
        _safe_segment(asset_id, "asset_id")
        if max_depth < 0:
            raise InvalidAssetError("max_depth must be >= 0")
        # A missing root is an error; missing *parents* are reported in
        # ``missing`` so a partially-pruned history is still auditable.
        self.lineage_record(asset_id)

        visited: set[str] = set()
        chain: list[dict] = []
        missing: list[str] = []
        queue: list[tuple[str, int]] = [(asset_id, 0)]
        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            try:
                lineage = self.lineage_record(current)
            except AssetNotFoundError:
                missing.append(current)
                continue
            chain.append({
                "asset_id": current,
                "version": lineage["version"],
                "project": lineage["project"],
                "episode": lineage["episode"],
                "source": lineage["source"],
                "derived_from": lineage["derived_from"],
            })
            if depth >= max_depth:
                continue
            for parent in lineage["derived_from"]:
                if isinstance(parent, str) and parent not in visited:
                    queue.append((parent, depth + 1))
        return {"root": asset_id, "chain": chain, "missing": missing}

    def get(self, asset_id: str) -> dict:
        """Return the full asset record (including retired assets)."""
        _safe_segment(asset_id, "asset_id")
        record = self._find_asset(asset_id)
        if record is None:
            raise AssetNotFoundError(asset_id)
        return copy.deepcopy(record)