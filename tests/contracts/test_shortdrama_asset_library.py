"""T4 contract tests for lib/asset_library.py (LIB-01, F10).

Guards the corpus adaptation contract: asset.json is the source of truth,
retirement is a tombstone (never a delete), identity vectors live in their own
versioned store keyed by embedder+model, and the corpus keeps serving text/tag
retrieval without its 512-d visual channel being polluted.
"""
import json

import numpy as np
import pytest

from lib.asset_library import (
    AssetLibrary,
    AssetLibraryError,
    AssetNotFoundError,
    DuplicateAssetError,
    InvalidAssetError,
)


@pytest.fixture
def library(tmp_path):
    return AssetLibrary(tmp_path / "library")


def _register_character(library, asset_id="ch_ling", **metadata):
    md = {"character_id": asset_id, "name": "Ling", "tags": ["female", "lead"]}
    md.update(metadata)
    return library.register("image", asset_id, md)


# --- register / get / truth source ---

def test_register_returns_id_and_writes_asset_json(library):
    assert _register_character(library) == "ch_ling"
    path = library.library_dir / "image" / "ch_ling" / "asset.json"
    assert path.is_file()
    record = library.get("ch_ling")
    assert record["kind"] == "image"
    assert record["status"] == "active"
    assert record["version"] == "1.0"
    assert record["metadata"]["name"] == "Ling"
    assert [e["event"] for e in record["events"]] == ["register"]


def test_register_rejects_duplicate_instead_of_overwriting(library):
    _register_character(library)
    with pytest.raises(DuplicateAssetError):
        _register_character(library, name="overwritten")
    assert library.get("ch_ling")["metadata"]["name"] == "Ling"


def test_register_validates_kind_and_path_safe_id(library):
    with pytest.raises(InvalidAssetError):
        library.register("audio", "voice_01", {})
    with pytest.raises(InvalidAssetError):
        library.register("image", "../escape", {})
    with pytest.raises(InvalidAssetError):
        library.register("image", "ok", {"bad": {1, 2}})


def test_get_unknown_raises(library):
    with pytest.raises(AssetNotFoundError):
        library.get("nope")


# --- metadata / tag queries (assets without vectors) ---

def test_query_by_character_id_and_tags(library):
    _register_character(library)
    library.register("image", "sc_kitchen", {"tags": ["kitchen", "night"]})
    library.register("image", "ch_chen", {"character_id": "ch_chen", "tags": ["male"]})

    assert [a["asset_id"] for a in library.query("image", character_id="ch_ling")] == ["ch_ling"]
    # All requested tags must be present (AND semantics).
    assert [a["asset_id"] for a in library.query("image", tags=["female", "lead"])] == ["ch_ling"]
    assert library.query("image", tags=["female", "male"]) == []


def test_query_is_kind_scoped_and_deterministically_ordered(library):
    library.register("image", "img_a", {"tags": ["x"]})
    library.register("image", "img_b", {"tags": ["x"]})
    library.register("video", "vid_a", {"tags": ["x"]})
    assert [a["asset_id"] for a in library.query("image")] == ["img_a", "img_b"]
    assert [a["asset_id"] for a in library.query("video")] == ["vid_a"]


def test_missing_vector_falls_back_to_tag_query(library):
    # No embedding supplied at all: the asset must still be discoverable.
    library.register("video", "vo_001", {"character_id": "ch_ling",
                                         "tags": ["voice", "english"]})
    hits = library.query("video", character_id="ch_ling", tags=["english"])
    assert [a["asset_id"] for a in hits] == ["vo_001"]
    assert "score" not in hits[0]


def test_text_query_uses_corpus_without_identity_vectors(library):
    library.register("video", "sh_001", {"tags": ["warm kitchen"],
                                         "description": "warm kitchen at night"})
    library.register("video", "sh_002", {"tags": ["street chase"]})
    hits = library.query("video", text="warm kitchen", min_sim=0.5)
    assert [a["asset_id"] for a in hits] == ["sh_001"]
    assert hits[0]["score"] > 0.85  # shared tokens dominate; extra tokens dilute
    # The corpus really is the retrieval engine: rows + tag bank exist on disk.
    index = (library.corpus_dir / "index.jsonl").read_text(encoding="utf-8")
    assert "sh_001" in index and "sh_002" in index
    tag_emb = np.load(library.corpus_dir / "tag_embeddings.npy")
    visual_emb = np.load(library.corpus_dir / "embeddings.npy")
    assert tag_emb.shape == (2, 512)
    assert not np.allclose(tag_emb[0], 0.0)
    # Visual channel stays untouched (zeros) — no identity/visual pollution.
    assert np.allclose(visual_emb, 0.0)


def test_text_min_sim_filters(library):
    library.register("video", "sh_001", {"tags": ["warm kitchen"]})
    assert library.query("video", text="warm kitchen", min_sim=0.99)
    assert library.query("video", text="submarine", min_sim=0.1) == []


# --- identity vectors: independent, versioned, model-keyed ---

def test_vector_query_hits_same_embedder_model(library):
    library.register("image", "face_a", {}, embedding=[1.0, 0.0], embedder_model="face-v1")
    library.register("image", "face_b", {}, embedding=[0.0, 1.0], embedder_model="face-v1")
    hits = library.query("image", vector=[0.95, 0.05], embedder_model="face-v1", min_sim=0.9)
    assert [a["asset_id"] for a in hits] == ["face_a"]
    assert hits[0]["score"] > 0.99


def test_vectors_are_isolated_by_embedder_model(library):
    library.register("image", "face_a", {}, embedding=[1.0, 0.0], embedder_model="face-v1")
    library.register("image", "voice_a", {}, embedding=[1.0, 0.0], embedder_model="voice-v1")
    hits = library.query("image", vector=[1.0, 0.0], embedder_model="face-v1")
    assert [a["asset_id"] for a in hits] == ["face_a"]


def test_identity_store_is_versioned_and_separate_from_corpus(library):
    library.register("image", "face_a", {}, embedding=np.array([3.0, 4.0]),
                     embedder_model="face-v1")
    store = library.library_dir / "identity" / "face-v1" / "v1"
    assert (store / "manifest.json").is_file()
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "store_version": "v1",
        "embedder_model": "face-v1",
        "dim": 2,
        "rows": 1,
        "updated_at": manifest["updated_at"],
    }
    record = library.get("face_a")
    assert record["identity"]["embedder_model"] == "face-v1"
    assert record["identity"]["store_version"] == "v1"
    assert record["identity"]["path"] == "identity/face-v1/v1/embeddings.npy"
    # Stored L2-normalised, and never written into the 512-d corpus visual bank.
    vectors = np.load(store / "embeddings.npy")
    assert vectors.shape == (1, 2)
    assert vectors[0] == pytest.approx([0.6, 0.8])
    visual = np.load(library.corpus_dir / "embeddings.npy")
    assert visual.shape == (1, 512) and np.allclose(visual, 0.0)


def test_vector_query_requires_model_and_matching_dim(library):
    library.register("image", "face_a", {}, embedding=[1.0, 0.0], embedder_model="face-v1")
    with pytest.raises(InvalidAssetError):
        library.query("image", vector=[1.0, 0.0])
    with pytest.raises(InvalidAssetError):
        library.query("image", vector=[1.0, 0.0, 0.0], embedder_model="face-v1")
    with pytest.raises(InvalidAssetError):
        library.register("image", "face_b", {}, embedding=[1.0, 0.0, 0.0],
                         embedder_model="face-v1")


def test_register_rejects_invalid_embedding_inputs(library):
    with pytest.raises(InvalidAssetError):
        library.register("image", "a", {}, embedding=[1.0, 0.0])  # no model
    with pytest.raises(InvalidAssetError):
        library.register("image", "a", {}, embedding=[0.0, 0.0],
                         embedder_model="face-v1")
    with pytest.raises(InvalidAssetError):
        library.register("image", "a", {}, embedding=[[1.0, 0.0]],
                         embedder_model="face-v1")


def test_text_plus_vector_is_rejected(library):
    with pytest.raises(InvalidAssetError):
        library.query("image", text="x", vector=[1.0], embedder_model="face-v1")


# --- lineage ---

def test_lineage_round_trips_and_defaults(library):
    library.register(
        "video", "sh_001", {"tags": ["hook"]},
        lineage={"project": "drama", "episode": "ep01", "shot": "sh_001",
                 "derived_from": ["sh_000"], "tool": "mock", "model": "mock-v1",
                 "seed": 42},
    )
    lineage = library.lineage("sh_001")
    assert lineage["project"] == "drama"
    assert lineage["episode"] == "ep01"
    assert lineage["derived_from"] == ["sh_000"]
    assert lineage["asset_id"] == "sh_001"
    assert lineage["kind"] == "video"

    library.register("image", "plain", {})
    plain = library.lineage("plain")
    assert plain == {"asset_id": "plain", "kind": "image",
                     "derived_from": [], "used_in": []}


def test_lineage_unknown_raises(library):
    with pytest.raises(AssetNotFoundError):
        library.lineage("nope")


# --- promote / retire (tombstone) ---

def test_promote_changes_status_and_keeps_query_visible(library):
    _register_character(library)
    library.promote("ch_ling")
    assert library.get("ch_ling")["status"] == "promoted"
    assert [a["asset_id"] for a in library.query("image")] == ["ch_ling"]
    # Idempotent: a second promote is a no-op and records no duplicate event.
    library.promote("ch_ling")
    events = [e["event"] for e in library.get("ch_ling")["events"]]
    assert events == ["register", "promote"]


def test_retire_is_tombstone_not_delete(library):
    _register_character(library)
    asset_path = library.library_dir / "image" / "ch_ling" / "asset.json"
    before = asset_path.read_text(encoding="utf-8")

    library.retire("ch_ling")

    assert asset_path.is_file()  # nothing deleted
    assert library.get("ch_ling")["status"] == "retired"
    assert library.get("ch_ling")["prior_status"] == "active"
    assert "retired_at" in library.get("ch_ling")
    assert len(asset_path.read_text(encoding="utf-8")) > len(before)
    # Default queries hide it; include_retired surfaces it again.
    assert library.query("image") == []
    assert library.query("image", tags=["female"]) == []
    assert [a["asset_id"] for a in library.query("image", include_retired=True)] == ["ch_ling"]
    # The corpus row stays (deleting it would break row<->embedding alignment).
    assert "ch_ling" in (library.corpus_dir / "index.jsonl").read_text(encoding="utf-8")


def test_retire_filters_text_and_vector_queries(library):
    library.register("image", "face_a", {"tags": ["warm kitchen"]},
                     embedding=[1.0, 0.0], embedder_model="face-v1")
    library.retire("face_a")
    assert library.query("image", text="warm kitchen") == []
    assert library.query("image", vector=[1.0, 0.0], embedder_model="face-v1") == []
    assert library.query("image", vector=[1.0, 0.0], embedder_model="face-v1",
                         include_retired=True)[0]["asset_id"] == "face_a"


def test_promote_retired_asset_is_refused(library):
    _register_character(library)
    library.retire("ch_ling")
    with pytest.raises(AssetLibraryError):
        library.promote("ch_ling")


def test_promote_and_retire_unknown_raise(library):
    with pytest.raises(AssetNotFoundError):
        library.promote("nope")
    with pytest.raises(AssetNotFoundError):
        library.retire("nope")


# --- isolation from caller mutation ---

def test_metadata_and_results_are_isolated_copies(library):
    metadata = {"tags": ["female"]}
    library.register("image", "ch_ling", metadata)
    metadata["tags"].append("mutated")
    assert library.get("ch_ling")["metadata"]["tags"] == ["female"]

    result = library.query("image")[0]
    result["metadata"]["tags"].append("mutated")
    assert library.get("ch_ling")["metadata"]["tags"] == ["female"]