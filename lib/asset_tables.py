"""Seven semantic asset tables + the rejection vault (LIB-02, LIB-06).

This module sits *on top of* :class:`lib.asset_library.AssetLibrary` and does
not change its contract. ``AssetLibrary`` keeps the corpus media kinds
``{"image", "video"}``; this layer adds the seven typed tables that say what an
asset **is**:

    characters / scenes / props / voices / music / shots / rejections

Directory layout (LIB-02)::

    <library>/<table>/<asset_id>/asset.json   # metadata/status/version truth
    <library>/<table>/<asset_id>/media/       # media copied beside the record
    <library>/index/<table>.jsonl             # append-only table index

Two deliberate separations:

- **Semantic assets are never corpus rows.** The corpus indexes video/image
  clips for retrieval; a character card or a rejected clip is a typed record.
  Text search over a semantic table therefore runs locally over each record's
  metadata with the same deterministic ``local-hash-v1`` embedder the library
  uses, keeping M0 offline.
- **``library/shots/index.jsonl`` stays the accepted-shot table** (LIB-04). The
  semantic ``shots`` table's index lives at ``library/index/shots.jsonl`` so the
  two never share a file.

LIB-06 rejection vault: a rejection is an asset in the ``rejections`` table
carrying a canonical reason code (``lib.review.REASON_CODES`` — the same enum
the review layer validates against), the rejected prompt, the model, and
optionally the rejected media. Registrations are append-only per ``asset_id``;
rejections accumulate and are queryable by reason code as negative samples for
AUTO-01 training data.

M0: local files only, zero external API calls.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from lib.asset_library import (
    ALL_KINDS,
    KINDS,
    SEMANTIC_KINDS,
    TEXT_INDEX_MODEL,
    AssetLibrary,
    AssetNotFoundError,
    InvalidAssetError,
    _as_tag_set,
    _hash_text_embedding,
    _now,
    _safe_segment,
    _search_text,
)
from lib.review import REASON_CODES

ASSET_TABLES = SEMANTIC_KINDS
TABLE_INDEX_VERSION = "1.0"
REJECTION_REQUIRED_FIELDS = ("reason_code", "prompt", "model")
_MEDIA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")


class AssetTables(AssetLibrary):
    """The seven semantic tables layered over :class:`AssetLibrary`.

    Usage::

        tables = AssetTables(Path("library"), ensure_tables=True)
        tables.register("characters", "ch_ling", {"tags": ["lead"], ...},
                        embedding=face_vec, embedder_model="facenet512")
        tables.promote("ch_ling")
        tables.record_rejection("rej_0001", reason_code="FACE_MISMATCH",
                                prompt="...", model="mock-v1",
                                media=[Path("shots/sh_001_v1.mp4")])
        tables.query_rejections(reason_code="FACE_MISMATCH")
    """

    def __init__(self, library_dir: Path, *, ensure_tables: bool = False):
        super().__init__(library_dir)
        if ensure_tables:
            self.ensure_tables()

    # ------------------------------------------------------------------
    # Paths / table registry
    # ------------------------------------------------------------------

    @property
    def table_index_dir(self) -> Path:
        return self.library_dir / "index"

    def ensure_tables(self) -> dict[str, Path]:
        """Create the seven table directories and their (empty) indexes."""
        out: dict[str, Path] = {}
        for table in ASSET_TABLES:
            path = self.table_dir(table)
            path.mkdir(parents=True, exist_ok=True)
            index = self.table_index_path(table)
            index.parent.mkdir(parents=True, exist_ok=True)
            if not index.exists():
                index.touch()
            out[table] = path
        return out

    def table_dir(self, table: str) -> Path:
        self._validate_table(table)
        return self.library_dir / table

    def table_index_path(self, table: str) -> Path:
        self._validate_table(table)
        return self.table_index_dir / f"{table}.jsonl"

    def media_dir(self, kind: str, asset_id: str) -> Path:
        """``library/<kind>/<asset_id>/media`` for any (media or semantic) kind."""
        self._validate_kind(kind)
        _safe_segment(asset_id, "asset_id")
        return self.library_dir / kind / asset_id / "media"

    def media_files(self, kind: str, asset_id: str) -> list[Path]:
        """Absolute paths of the media copied beside an asset record."""
        self._validate_kind(kind)
        _safe_segment(asset_id, "asset_id")
        record = self._read_asset(kind, asset_id)
        if record is None:
            raise AssetNotFoundError(asset_id)
        names = (record.get("metadata") or {}).get("media") or []
        base = self.media_dir(kind, asset_id)
        return [base / str(name) for name in names]

    # ------------------------------------------------------------------
    # Kind / lookup overrides (backward compatible with AssetLibrary)
    # ------------------------------------------------------------------

    def _validate_kind(self, kind: str) -> None:
        if kind not in ALL_KINDS:
            raise InvalidAssetError(f"kind must be one of {ALL_KINDS}, got {kind!r}")

    def _validate_table(self, table: str) -> None:
        if table not in ASSET_TABLES:
            raise InvalidAssetError(
                f"table must be one of {ASSET_TABLES}, got {table!r}")

    def _find_asset(self, asset_id: str) -> Optional[dict]:
        """Find one asset across all kinds; ids are unique across the library.

        Media kinds come first so legacy ``image``/``video`` records keep
        resolving exactly as they did in :class:`AssetLibrary`.
        """
        found = []
        for kind in ALL_KINDS:
            record = self._read_asset(kind, asset_id)
            if record is not None:
                found.append(record)
        if len(found) > 1:
            kinds = ", ".join(sorted(str(record.get("kind")) for record in found))
            raise InvalidAssetError(
                f"asset_id {asset_id!r} exists in multiple tables ({kinds}); "
                "ids must be unique across the library")
        return found[0] if found else None

    def _index_in_corpus(self, record: dict) -> None:
        # Semantic tables are typed records, not corpus video/image rows.
        if record["kind"] in ASSET_TABLES:
            return
        super()._index_in_corpus(record)

    # ------------------------------------------------------------------
    # Registration (with media)
    # ------------------------------------------------------------------

    def register(
        self,
        kind: str,
        asset_id: str,
        metadata: dict,
        *,
        media: Optional[Iterable[Any]] = None,
        embedding=None,
        embedder_model=None,
        lineage=None,
    ) -> str:
        """Register an asset, copying any supplied media beside its record.

        ``media`` accepts local file paths; each file is copied into
        ``library/<kind>/<asset_id>/media/`` and recorded by name (never by
        absolute path) in ``metadata["media"]``. For ``rejections`` the record
        must carry canonical ``reason_code``/``prompt``/``model`` fields.
        """
        self._validate_kind(kind)
        if not isinstance(metadata, dict):
            raise InvalidAssetError("metadata must be a dict")
        metadata = copy.deepcopy(metadata)
        if kind == "rejections":
            metadata = self._validate_rejection_metadata(metadata)

        media_plan = self._plan_media(media)
        if media_plan:
            metadata["media"] = [name for _, name in media_plan]
        elif kind in ASSET_TABLES:
            metadata.setdefault("media", [])

        staging = self._stage_media(media_plan)
        try:
            asset_id = super().register(
                kind,
                asset_id,
                metadata,
                embedding=embedding,
                embedder_model=embedder_model,
                lineage=lineage,
            )
        except Exception:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            raise

        if media_plan or kind in ASSET_TABLES:
            destination = self.media_dir(kind, asset_id)
            destination.mkdir(parents=True, exist_ok=True)
            if media_plan:
                for name in metadata["media"]:
                    (staging / name).replace(destination / name)
                shutil.rmtree(staging, ignore_errors=True)

        if kind in ASSET_TABLES:
            self._append_table_index(
                kind, asset_id, event="register", status="active",
                prior_status=None, metadata=metadata)
        return asset_id

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

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
        reason_code=None,
        metadata=None,
    ) -> list[dict]:
        """Query media kinds or semantic tables with the same result shape.

        On top of :meth:`AssetLibrary.query` this accepts ``reason_code`` (a
        canonical review code) and ``metadata`` (top-level exact-match
        filters). Semantic tables search their metadata locally; they never
        consult the corpus.
        """
        self._validate_kind(kind)
        reason_filter = self._normalise_reason_filter(reason_code)
        filters = dict(metadata or {})
        if reason_filter is not None:
            filters["reason_code"] = reason_filter

        if kind in KINDS:
            results = super().query(
                kind,
                character_id=character_id,
                tags=tags,
                text=text,
                vector=vector,
                embedder_model=embedder_model,
                min_sim=min_sim,
                include_retired=include_retired,
            )
            return self._filter_by_metadata(results, filters)

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
            candidates = self._semantic_text_scores(kind, text)
        else:
            candidates = [
                (asset["asset_id"], None) for asset in self._iter_assets(kind)]
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
            if not self._metadata_matches(asset, filters):
                continue
            if score is not None and score < min_sim:
                continue
            item = copy.deepcopy(asset)
            if score is not None:
                item["score"] = score
            results.append(item)
        return results

    def _semantic_text_scores(self, kind: str, text: str) -> list[tuple[str, float]]:
        """Local deterministic text ranking over a semantic table's metadata."""
        if not isinstance(text, str) or not text.strip():
            raise InvalidAssetError("text must be a non-empty string")
        query_vec = _hash_text_embedding(text)
        scored: list[tuple[str, float]] = []
        for asset in self._iter_assets(kind):
            document = _search_text(asset.get("metadata") or {})
            if not document.strip():
                continue
            score = float(np.dot(query_vec, _hash_text_embedding(document)))
            scored.append((asset["asset_id"], score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    @staticmethod
    def _metadata_matches(asset: dict, filters: dict) -> bool:
        if not filters:
            return True
        metadata = asset.get("metadata") or {}
        return all(metadata.get(key) == value for key, value in filters.items())

    def _filter_by_metadata(self, results: list[dict], filters: dict) -> list[dict]:
        if not filters:
            return results
        return [item for item in results if self._metadata_matches(item, filters)]

    @staticmethod
    def _normalise_reason_filter(reason_code: Any) -> Optional[str]:
        if reason_code is None:
            return None
        if not isinstance(reason_code, str):
            raise InvalidAssetError("reason_code must be a string")
        code = reason_code.strip().upper()
        if code not in REASON_CODES:
            raise InvalidAssetError(f"unknown reason_code {reason_code!r}")
        return code

    # ------------------------------------------------------------------
    # LIB-06 rejections vault
    # ------------------------------------------------------------------

    def record_rejection(
        self,
        asset_id: str,
        *,
        reason_code: str,
        prompt: str,
        model: str,
        media: Optional[Iterable[Any]] = None,
        metadata: Optional[dict] = None,
        lineage=None,
        embedding=None,
        embedder_model=None,
        stage: Optional[str] = None,
        note: Optional[str] = None,
    ) -> str:
        """Store one rejected candidate in the rejections vault (LIB-06).

        The reason code is validated against the canonical
        ``lib.review.REASON_CODES`` enum so the vault cannot drift from the
        review layer. Prompt/model are required (they are what a regeneration
        policy needs); media is optional because text-stage rejections have no
        artifact of their own. Re-recording an existing id raises rather than
        overwriting history.
        """
        fields: dict[str, Any] = dict(metadata or {})
        fields.update({"reason_code": reason_code, "prompt": prompt, "model": model})
        if stage is not None:
            fields["stage"] = stage
        if note is not None:
            fields["note"] = note
        fields.setdefault(
            "tags", ["rejection", str(reason_code).strip().upper()])
        return self.register(
            "rejections",
            asset_id,
            fields,
            media=media,
            embedding=embedding,
            embedder_model=embedder_model,
            lineage=lineage,
        )

    def query_rejections(
        self,
        *,
        reason_code: Optional[str] = None,
        tags=None,
        text: Optional[str] = None,
        include_retired: bool = False,
        metadata: Optional[dict] = None,
    ) -> list[dict]:
        """Rejected assets, optionally filtered by canonical reason code."""
        return self.query(
            "rejections",
            reason_code=reason_code,
            tags=tags,
            text=text,
            include_retired=include_retired,
            metadata=metadata,
        )

    def negative_samples(self, reason_code: Optional[str] = None) -> list[dict]:
        """Accumulated rejection records (AUTO-01 negative samples), oldest first."""
        rows = self.query_rejections(reason_code=reason_code)
        return sorted(
            rows, key=lambda row: (str(row.get("created_at") or ""), row["asset_id"]))

    def rejection_counts(self) -> dict[str, int]:
        """Count registered rejections per reason code (retired ones included)."""
        counts: dict[str, int] = {}
        for row in self.query("rejections", include_retired=True):
            code = (row.get("metadata") or {}).get("reason_code")
            if code:
                counts[str(code)] = counts.get(str(code), 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _validate_rejection_metadata(metadata: dict) -> dict:
        fields = copy.deepcopy(metadata)
        for field in REJECTION_REQUIRED_FIELDS:
            value = fields.get(field)
            if not isinstance(value, str) or not value.strip():
                raise InvalidAssetError(
                    f"rejection requires a non-empty {field!r}")
        code = fields["reason_code"].strip().upper()
        if code not in REASON_CODES:
            raise InvalidAssetError(f"unknown reason_code {fields['reason_code']!r}")
        fields["reason_code"] = code
        fields["prompt"] = fields["prompt"].strip()
        fields["model"] = fields["model"].strip()
        fields.setdefault("media", [])
        return fields

    # ------------------------------------------------------------------
    # Status changes: asset.json + derived table index, never corpus rows
    # ------------------------------------------------------------------

    def promote(self, asset_id: str) -> None:
        """Candidate -> reusable. Idempotent; semantic tables get an index row."""
        record = self.get(asset_id)
        kind = record["kind"]
        prior_status = record.get("status")
        already = prior_status == "promoted"
        super().promote(asset_id)
        if already or kind not in ASSET_TABLES:
            return
        self._append_table_index(
            kind, asset_id, event="promote", status="promoted",
            prior_status=prior_status)

    def retire(self, asset_id: str) -> None:
        """Tombstone an asset; semantic tables journal the transition too."""
        record = self.get(asset_id)
        kind = record["kind"]
        prior_status = record.get("status")
        already = prior_status == "retired"
        super().retire(asset_id)
        if already or kind not in ASSET_TABLES:
            return
        self._append_table_index(
            kind, asset_id, event="retire", status="retired",
            prior_status=prior_status)

    def _append_table_index(
        self,
        table: str,
        asset_id: str,
        *,
        event: str,
        status: Optional[str],
        prior_status: Optional[str],
        metadata: Optional[dict] = None,
    ) -> None:
        """Append one transition to ``library/index/<table>.jsonl``.

        ``asset.json`` remains the single source of truth; this is a derived,
        append-only index, mirroring the library's global status journal.
        """
        path = self.table_index_path(table)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "table_version": TABLE_INDEX_VERSION,
            "table": table,
            "asset_id": asset_id,
            "event": event,
            "status": status,
            "prior_status": prior_status,
            "at": _now(),
        }
        if table == "rejections":
            code = (metadata or {}).get("reason_code")
            if code:
                row["reason_code"] = code
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def table_index(self, table: str) -> dict[str, dict]:
        """Latest table-index row per asset (empty before the first write)."""
        self._validate_table(table)
        out: dict[str, dict] = {}
        path = self.table_index_path(table)
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
    # Media helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_media(media: Optional[Iterable[Any]]) -> list[tuple[Path, str]]:
        if media is None:
            return []
        if isinstance(media, (str, Path)):
            media = [media]
        plan: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for item in media:
            source = Path(item)
            if not source.is_file():
                raise InvalidAssetError(f"media file does not exist: {source}")
            name = source.name
            if not _MEDIA_NAME.fullmatch(name):
                raise InvalidAssetError(
                    f"media file name {name!r} is not a safe file name")
            if name in seen:
                raise InvalidAssetError(f"duplicate media file name {name!r}")
            seen.add(name)
            plan.append((source, name))
        return plan

    def _stage_media(self, plan: list[tuple[Path, str]]) -> Optional[Path]:
        """Copy media into a staging dir before the record is committed."""
        if not plan:
            return None
        staging = self.library_dir / ".staging" / uuid.uuid4().hex
        staging.mkdir(parents=True, exist_ok=True)
        try:
            for source, name in plan:
                shutil.copy2(source, staging / name)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return staging