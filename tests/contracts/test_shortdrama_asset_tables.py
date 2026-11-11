"""WP-I contract tests: LIB-02 seven asset tables + LIB-06 rejection vault.

These guard the semantic layer added on top of ``lib.asset_library``:

- the seven typed tables exist with ``library/<table>/<asset_id>/{asset.json,media/}``
  plus an append-only per-table index at ``library/index/<table>.jsonl``;
- every table supports typed registration and retrieval without touching the
  corpus (semantic assets are records, not video/image rows);
- LIB-06 rejections store reason code + prompt + model + media, validate the
  canonical reason-code enum, and accumulate queryable negative samples;
- promote moves candidate -> reusable through ``asset.json`` + index only, and
  the original ``AssetLibrary`` media-kind contract keeps working unchanged.
"""
import json

import numpy as np
import pytest

from lib.asset_library import (
    ALL_KINDS,
    KINDS,
    SEMANTIC_KINDS,
    AssetLibrary,
    AssetLibraryError,
    AssetNotFoundError,
    DuplicateAssetError,
    InvalidAssetError,
)
from lib.asset_tables import ASSET_TABLES, AssetTables


@pytest.fixture
def tables(tmp_path):
    return AssetTables(tmp_path / "library", ensure_tables=True)


def _write_media(tmp_path, name, payload=b"media-bytes"):
    path = tmp_path / name
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# LIB-02 — seven tables: directories, register, query, index
# ---------------------------------------------------------------------------

def test_seven_tables_match_the_registry():
    assert ASSET_TABLES == SEMANTIC_KINDS == (
        "characters", "scenes", "props", "voices", "music", "shots",
        "rejections")
    assert set(ALL_KINDS) == set(KINDS) | set(ASSET_TABLES)


def test_ensure_tables_creates_seven_directories_and_indexes(tables):
    paths = tables.ensure_tables()
    assert set(paths) == set(ASSET_TABLES)
    for table in ASSET_TABLES:
        assert tables.table_dir(table).is_dir()
        index = tables.table_index_path(table)
        assert index.is_file()
        assert index.parent == tables.library_dir / "index"
        assert tables.table_index(table) == {}


def test_register_and_query_each_semantic_table(tables):
    for number, table in enumerate(ASSET_TABLES):
        asset_id = f"{table}_001"
        if table == "rejections":
            tables.record_rejection(
                asset_id, reason_code="ARTIFACT",
                prompt=f"prompt {table}", model="mock-v1")
        else:
            tables.register(table, asset_id, {"tags": [table, "seed"]})

        # LIB-02 directory contract: asset.json + media/ beside it.
        asset_dir = tables.library_dir / table / asset_id
        assert (asset_dir / "asset.json").is_file()
        assert (asset_dir / "media").is_dir()

        # The append-only index records the registration.
        rows = tables.table_index(table)
        assert rows[asset_id]["event"] == "register"
        assert rows[asset_id]["status"] == "active"
        assert rows[asset_id]["table"] == table
        assert json.loads(
            (tables.library_dir / "index" / f"{table}.jsonl")
            .read_text(encoding="utf-8").splitlines()[-1]
        )["asset_id"] == asset_id

        record = tables.get(asset_id)
        assert record["kind"] == table
        assert record["status"] == "active"
        assert record["metadata"]["media"] == []

    # Tag queries stay table-scoped and deterministic.
    assert [a["asset_id"] for a in tables.query("props", tags=["props"])] == [
        "props_001"]
    assert tables.query("scenes", tags=["props"]) == []


def test_semantic_text_query_is_local_and_deterministic(tables):
    tables.register("characters", "ch_ling",
                    {"name": "Ling", "description": "warm kitchen night"})
    tables.register("characters", "ch_chen",
                    {"name": "Chen", "description": "street chase at noon"})

    hits = tables.query("characters", text="warm kitchen", min_sim=0.2)
    assert [a["asset_id"] for a in hits] == ["ch_ling"]
    assert hits[0]["score"] > 0
    assert tables.query("characters", text="submarine", min_sim=0.2) == []

    # Text search never creates a corpus: semantic tables are typed records.
    assert not tables.corpus_dir.exists()


def test_semantic_tables_support_identity_vectors(tables):
    tables.register("characters", "face_a", {"tags": ["lead"]},
                    embedding=[1.0, 0.0], embedder_model="face-v1")
    tables.register("characters", "face_b", {"tags": ["support"]},
                    embedding=[0.0, 1.0], embedder_model="face-v1")

    hits = tables.query("characters", vector=[0.95, 0.05],
                        embedder_model="face-v1", min_sim=0.9)
    assert [a["asset_id"] for a in hits] == ["face_a"]
    assert hits[0]["score"] > 0.99
    # The identity store stayed separate from any corpus visual bank.
    assert (tables.identity_dir / "face-v1" / "v1" / "embeddings.npy").is_file()
    assert not tables.corpus_dir.exists()


def test_semantic_registration_copies_media_beside_the_record(tables, tmp_path):
    source = _write_media(tmp_path, "scene_wide.png", b"png-bytes")
    tables.register("scenes", "sc_kitchen", {"tags": ["kitchen"]},
                    media=[source])

    record = tables.get("sc_kitchen")
    assert record["metadata"]["media"] == ["scene_wide.png"]
    copied = tables.media_files("scenes", "sc_kitchen")
    assert copied == [tables.library_dir / "scenes" / "sc_kitchen"
                      / "media" / "scene_wide.png"]
    assert copied[0].read_bytes() == b"png-bytes"
    # Only the file name is stored, not the caller's absolute path.
    assert str(tmp_path) not in json.dumps(record["metadata"])

    with pytest.raises(InvalidAssetError):
        tables.register("scenes", "sc_bad", {}, media=[tmp_path / "missing.png"])


def test_media_kind_registration_still_uses_the_corpus(tables):
    tables.register("image", "img_001", {"tags": ["still"]})
    tables.register("characters", "ch_ling", {"tags": ["lead"]})

    assert [a["asset_id"] for a in tables.query("image", tags=["still"])] == [
        "img_001"]
    assert tables.query("image", tags=["lead"]) == []
    corpus_index = (tables.corpus_dir / "index.jsonl").read_text(encoding="utf-8")
    assert "img_001" in corpus_index
    assert "ch_ling" not in corpus_index

    # The base class still rejects semantic kinds; only this layer accepts them.
    base = AssetLibrary(tables.library_dir)
    with pytest.raises(InvalidAssetError):
        base.register("characters", "another", {})


# ---------------------------------------------------------------------------
# LIB-06 — rejections vault
# ---------------------------------------------------------------------------

def test_rejections_store_code_prompt_model_and_media(tables, tmp_path):
    clip = _write_media(tmp_path, "sh_001_v1.mp4", b"rejected-clip")
    tables.record_rejection(
        "rej_0001",
        reason_code="FACE_MISMATCH",
        prompt="lead walks into the kitchen, face consistent",
        model="mock-video-v1",
        media=[clip],
        stage="shot_gen",
        metadata={"review_id": "rv_001", "candidate_version": "v1"},
        lineage={"project": "drama", "episode": "ep01", "source": "generated"},
    )

    record = tables.get("rej_0001")
    assert record["kind"] == "rejections"
    metadata = record["metadata"]
    assert metadata["reason_code"] == "FACE_MISMATCH"
    assert metadata["prompt"].startswith("lead walks")
    assert metadata["model"] == "mock-video-v1"
    assert metadata["stage"] == "shot_gen"
    assert metadata["review_id"] == "rv_001"
    assert metadata["media"] == ["sh_001_v1.mp4"]
    assert tables.media_files("rejections", "rej_0001")[0].read_bytes() == \
        b"rejected-clip"
    assert record["lineage"]["episode"] == "ep01"


def test_rejections_query_by_reason_code_and_accumulate_negatives(tables):
    tables.record_rejection("rej_001", reason_code="FACE_MISMATCH",
                            prompt="p1", model="mock-v1")
    tables.record_rejection("rej_002", reason_code="ARTIFACT",
                            prompt="p2", model="mock-v1")
    tables.record_rejection("rej_003", reason_code="face_mismatch",
                            prompt="p3", model="mock-v2")

    face = tables.query_rejections(reason_code="FACE_MISMATCH")
    assert [row["asset_id"] for row in face] == ["rej_001", "rej_003"]
    # Lowercase input is normalised to the canonical enum.
    assert [row["asset_id"] for row in
            tables.query_rejections(reason_code="face_mismatch")] == [
        "rej_001", "rej_003"]
    assert [row["asset_id"] for row in
            tables.query_rejections(reason_code="ARTIFACT")] == ["rej_002"]

    assert [row["asset_id"] for row in
            tables.negative_samples(reason_code="FACE_MISMATCH")] == [
        "rej_001", "rej_003"]
    assert len(tables.negative_samples()) == 3
    assert tables.rejection_counts() == {"ARTIFACT": 1, "FACE_MISMATCH": 2}

    # Generic metadata filters share the same path.
    assert [row["asset_id"] for row in
            tables.query_rejections(metadata={"model": "mock-v2"})] == ["rej_003"]


def test_rejections_validate_canonical_reason_codes_and_required_fields(tables):
    with pytest.raises(InvalidAssetError):
        tables.record_rejection("rej_bad", reason_code="NOT_A_CODE",
                                prompt="p", model="m")
    with pytest.raises(InvalidAssetError):
        tables.record_rejection("rej_bad", reason_code="ARTIFACT",
                                prompt="", model="m")
    with pytest.raises(InvalidAssetError):
        tables.record_rejection("rej_bad", reason_code="ARTIFACT",
                                prompt="p", model="")
    with pytest.raises(InvalidAssetError):
        tables.query_rejections(reason_code="NOT_A_CODE")

    # A text-stage rejection has no media of its own; that is not an error.
    tables.record_rejection("rej_text", reason_code="HOOK_WEAK",
                            prompt="weak hook", model="mock-llm")
    assert tables.get("rej_text")["metadata"]["media"] == []


def test_rejections_are_append_only_by_asset_id(tables):
    tables.record_rejection("rej_001", reason_code="ARTIFACT",
                            prompt="first", model="m")
    with pytest.raises(DuplicateAssetError):
        tables.record_rejection("rej_001", reason_code="ARTIFACT",
                                prompt="overwritten", model="m")
    assert tables.get("rej_001")["metadata"]["prompt"] == "first"


# ---------------------------------------------------------------------------
# LIB-04 promote on semantic tables — status in asset.json + index only
# ---------------------------------------------------------------------------

def test_promote_updates_semantic_status_and_index_without_corpus_rows(tables):
    tables.register("shots", "ast_shot_001", {"tags": ["hook"]})
    record_path = tables.library_dir / "shots" / "ast_shot_001" / "asset.json"

    tables.promote("ast_shot_001")

    record = tables.get("ast_shot_001")
    assert record["status"] == "promoted"
    assert record["promoted_at"]
    assert [event["event"] for event in record["events"]] == [
        "register", "promote"]
    assert tables.table_index("shots")["ast_shot_001"]["event"] == "promote"
    assert tables.table_index("shots")["ast_shot_001"]["status"] == "promoted"
    assert tables.table_index("shots")["ast_shot_001"]["prior_status"] == "active"
    # Global status journal still agrees with the per-table index.
    assert tables.status_index()["ast_shot_001"]["status"] == "promoted"
    # Still a typed record: nothing was written into the corpus.
    assert not tables.corpus_dir.exists()

    # Idempotent: no duplicate event, no duplicate index row.
    tables.promote("ast_shot_001")
    assert [event["event"] for event in
            tables.get("ast_shot_001")["events"]] == ["register", "promote"]
    rows = [line for line in
            tables.table_index_path("shots").read_text(encoding="utf-8")
            .splitlines() if line]
    assert len(rows) == 2
    assert record_path.is_file()

    with pytest.raises(AssetNotFoundError):
        tables.promote("missing_asset")


def test_retire_is_tombstone_and_blocks_promote_on_semantic_tables(tables):
    tables.register("props", "prop_knife", {"tags": ["prop"]})
    tables.retire("prop_knife")

    record = tables.get("prop_knife")
    assert record["status"] == "retired"
    assert record["prior_status"] == "active"
    assert tables.table_index("props")["prop_knife"]["status"] == "retired"
    assert tables.query("props") == []
    assert [row["asset_id"] for row in
            tables.query("props", include_retired=True)] == ["prop_knife"]
    with pytest.raises(AssetLibraryError):
        tables.promote("prop_knife")


def test_media_kind_promote_does_not_write_a_semantic_index(tables):
    tables.register("image", "img_001", {"tags": ["still"]})
    tables.promote("img_001")

    assert tables.get("img_001")["status"] == "promoted"
    assert tables.status_index()["img_001"]["status"] == "promoted"
    assert not (tables.library_dir / "index" / "image.jsonl").exists()
    assert not (tables.library_dir / "index" / "video.jsonl").exists()


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------

def test_semantic_query_validates_the_same_parameter_rules_as_the_base(tables):
    with pytest.raises(InvalidAssetError):
        tables.query("characters", text="x", vector=[1.0],
                     embedder_model="face-v1")
    with pytest.raises(InvalidAssetError):
        tables.query("characters", vector=[1.0])
    with pytest.raises(InvalidAssetError):
        tables.query("characters", text="x", embedder_model="face-v1")
    with pytest.raises(InvalidAssetError):
        tables.query("characters", text=123)
    with pytest.raises(InvalidAssetError):
        tables.query("not_a_table")
    with pytest.raises(InvalidAssetError):
        tables.table_index("not_a_table")


def test_semantic_kinds_support_vector_dimension_checks(tables):
    tables.register("voices", "voice_a", {}, embedding=[1.0, 0.0],
                    embedder_model="voice-v1")
    with pytest.raises(InvalidAssetError):
        tables.query("voices", vector=[1.0, 0.0, 0.0],
                     embedder_model="voice-v1")
    with pytest.raises(InvalidAssetError):
        tables.register("voices", "voice_b", {}, embedding=[1.0],
                        embedder_model="voice-v1")
    assert np.load(tables.identity_dir / "voice-v1" / "v1" /
                   "embeddings.npy").shape == (1, 2)