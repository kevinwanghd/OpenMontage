"""WP-X contract tests: LIB-03 / LIB-04 / LIB-05 / LIB-07 extensions.

Covers the pieces added on top of the LIB-01 adapter:

- LIB-03 strict lineage: every applicable field present, unknown values are an
  explicit ``None`` + reason (never fabricated), and provenance traces to the
  source version.
- LIB-04 cross-episode reuse: accepted whole shots enter the append-only shots
  table, and candidate -> reusable is a status change in ``asset.json`` plus
  the status journal — never a corpus row mutation.
- LIB-05 retrieval contract: the D-1 parameters, pagination, and explicit
  degradation to tag retrieval when the image embedder is missing or too slow.
- LIB-07 separation: content-hash cache (off by default, LRU cap) and the
  always-on semantic/tag library have independent keys, budgets and toggles.
"""
import hashlib
import json
import time

import numpy as np
import pytest

from lib.asset_cache import (
    AssetCache,
    asset_cache_enabled,
    default_asset_cache_dir,
    reset_default_asset_cache,
)
from lib.asset_library import (
    LINEAGE_FIELDS,
    NULL_REASON_NOT_REPORTED,
    AssetLibrary,
    AssetNotFoundError,
    InvalidAssetError,
)
from lib.retrieval import (
    DEGRADED_EMBEDDER_SLOW,
    DEGRADED_EMBEDDER_UNAVAILABLE,
    LibraryQuery,
    LibraryRetriever,
    query_library,
)


@pytest.fixture
def library(tmp_path):
    return AssetLibrary(tmp_path / "library")


# ---------------------------------------------------------------------------
# LIB-03 — lineage
# ---------------------------------------------------------------------------

def test_lineage_record_has_every_applicable_field(library):
    library.register(
        "video", "sh_001", {"tags": ["hook"]},
        lineage={
            "project": "drama",
            "episode": "ep01",
            "source": "generated",
            "rights": {"status": "owned", "evidence": "release_01.pdf"},
            "tool": "mock-image",
            "model": "mock-v1",
            "generation_params": {"seed": 42, "steps": 8},
            "derived_from": ["sh_000"],
            "used_in": ["ep01"],
            "shot": "sh_001",
        },
    )
    lineage = library.lineage_record("sh_001")

    assert set(LINEAGE_FIELDS).issubset(lineage)
    assert lineage["project"] == "drama"
    assert lineage["episode"] == "ep01"
    assert lineage["source"] == "generated"
    assert lineage["rights"] == {"status": "owned", "evidence": "release_01.pdf"}
    assert lineage["tool"] == "mock-image"
    assert lineage["model"] == "mock-v1"
    assert lineage["generation_params"] == {"seed": 42, "steps": 8}
    assert lineage["derived_from"] == ["sh_000"]
    assert lineage["used_in"] == ["ep01"]
    assert lineage["shot"] == "sh_001"  # extra provenance is preserved
    assert lineage["null_reasons"] == {}


def test_lineage_record_explicit_null_plus_reason_never_fabricates(library):
    library.register(
        "image", "ch_ling", {"character_id": "ch_ling"},
        lineage={
            "project": "drama",
            "episode": "ep01",
            "source": "mock",
            "null_reasons": {
                "rights": "provider does not return rights metadata",
                "model": "not_applicable: deterministic mock has no model id",
            },
        },
    )
    lineage = library.lineage_record("ch_ling")

    # Caller-declared N/A keeps its own reason.
    assert lineage["rights"] is None
    assert lineage["null_reasons"]["rights"] == "provider does not return rights metadata"
    assert lineage["model"] is None
    assert lineage["null_reasons"]["model"].startswith("not_applicable")
    # Missing fields are still present as null with a reason, never guessed.
    for field in ("tool", "generation_params"):
        assert lineage[field] is None
        assert lineage["null_reasons"][field] == NULL_REASON_NOT_REPORTED
    # Nothing was invented from the missing provider values.
    assert lineage["source"] == "mock"


def test_lineage_trace_follows_derived_from_and_versions(library):
    library.register("image", "src_face", {"tags": ["face"]},
                     lineage={"project": "drama", "episode": "ep01",
                              "source": "stock", "tool": "camera", "model": "raw"})
    library.register("image", "ch_ling", {"tags": ["lead"]},
                     lineage={"project": "drama", "episode": "ep01",
                              "source": "generated", "tool": "mock",
                              "model": "mock-v1",
                              "derived_from": ["src_face"]})

    trace = library.trace_lineage("ch_ling")
    assert trace["root"] == "ch_ling"
    assert [row["asset_id"] for row in trace["chain"]] == ["ch_ling", "src_face"]
    assert all(row["version"] for row in trace["chain"])
    assert trace["missing"] == []

    # A dangling parent is reported, not invented.
    library.register("image", "ch_ling_v2", {},
                     lineage={"derived_from": ["ch_ling", "ghost_parent"]})
    trace = library.trace_lineage("ch_ling_v2")
    assert trace["missing"] == ["ghost_parent"]
    assert {row["asset_id"] for row in trace["chain"]} == {
        "ch_ling_v2", "ch_ling", "src_face"}


def test_lineage_trace_terminates_on_cycle(library):
    library.register("image", "cyc_a", {}, lineage={"derived_from": ["cyc_b"]})
    library.register("image", "cyc_b", {}, lineage={"derived_from": ["cyc_a"]})
    trace = library.trace_lineage("cyc_a")
    assert {row["asset_id"] for row in trace["chain"]} == {"cyc_a", "cyc_b"}

    with pytest.raises(AssetNotFoundError):
        library.trace_lineage("missing_root")


# ---------------------------------------------------------------------------
# LIB-04 — promote + accepted-shot table
# ---------------------------------------------------------------------------

def test_promote_updates_asset_and_status_index_without_row_mutation(library):
    library.register("image", "ch_ling", {"character_id": "ch_ling"})
    corpus_index = library.corpus_dir / "index.jsonl"
    corpus_before = corpus_index.read_bytes()
    embeddings_before = (library.corpus_dir / "embeddings.npy").read_bytes()
    tag_before = (library.corpus_dir / "tag_embeddings.npy").read_bytes()

    library.promote("ch_ling")

    record = library.get("ch_ling")
    assert record["status"] == "promoted"
    assert record["promoted_at"]
    assert [e["event"] for e in record["events"]] == ["register", "promote"]
    assert library.status_index()["ch_ling"]["status"] == "promoted"
    assert library.status_index()["ch_ling"]["prior_status"] == "active"

    # Status lives in asset.json + the journal; corpus rows never move.
    assert corpus_index.read_bytes() == corpus_before
    assert (library.corpus_dir / "embeddings.npy").read_bytes() == embeddings_before
    assert (library.corpus_dir / "tag_embeddings.npy").read_bytes() == tag_before

    # Idempotent: no second event, no second journal row.
    library.promote("ch_ling")
    assert [e["event"] for e in library.get("ch_ling")["events"]] == [
        "register", "promote"]
    journal = [
        json.loads(line) for line in
        library.status_index_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert [row["event"] for row in journal] == ["register", "promote"]


def test_record_accepted_shot_enters_shots_table_for_next_episode(library):
    library.register("video", "ast_sh_001", {"tags": ["hook"]})
    library.promote("ast_sh_001")

    row = library.record_accepted_shot(
        project="drama", episode="ep01", shot_id="sh_001",
        asset_id="ast_sh_001", metadata={"duration_s": 3.5})
    assert row["status"] == "accepted"
    assert row["asset_id"] == "ast_sh_001"

    # Append-only table on disk.
    path = library.library_dir / "shots" / "index.jsonl"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8").strip())["shot_id"] == "sh_001"

    # Episode 1 has no earlier episode to reuse; episode 2 can reuse ep01.
    assert library.reusable_shots("drama", exclude_episode="ep01") == []
    reusable = library.reusable_shots("drama", exclude_episode="ep02")
    assert [r["shot_id"] for r in reusable] == ["sh_001"]

    # Re-recording the same shot is idempotent (no duplicate rows).
    library.record_accepted_shot(
        project="drama", episode="ep01", shot_id="sh_001", asset_id="ast_sh_001")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1


def test_record_accepted_shot_can_promote_and_validates_asset(library):
    library.register("video", "ast_candidate", {})
    library.record_accepted_shot(
        project="drama", episode="ep01", shot_id="sh_009",
        asset_id="ast_candidate", promote=True)
    assert library.get("ast_candidate")["status"] == "promoted"
    assert library.status_index()["ast_candidate"]["status"] == "promoted"

    with pytest.raises(AssetNotFoundError):
        library.record_accepted_shot(
            project="drama", episode="ep01", shot_id="sh_010",
            asset_id="ast_missing")
    with pytest.raises(InvalidAssetError):
        library.record_accepted_shot(
            project="../escape", episode="ep01", shot_id="sh_011")


# ---------------------------------------------------------------------------
# LIB-05 — retrieval parameters + tag degradation + measured baseline
# ---------------------------------------------------------------------------

def _register_shots(library):
    library.register("video", "sh_001", {"tags": ["warm kitchen"],
                                         "description": "warm kitchen at night",
                                         "reason_code": "FACE_MISMATCH"})
    library.register("video", "sh_002", {"tags": ["street chase"],
                                         "description": "street chase at noon"})
    library.register("video", "sh_003", {"tags": ["warm kitchen"],
                                         "description": "warm kitchen day"})
    library.register("image", "still_001", {"tags": ["warm kitchen"]})


def test_query_parameter_contract_rejects_invalid_values(library):
    retriever = LibraryRetriever(library)
    for bad in (
        LibraryQuery(page=0),
        LibraryQuery(page_size=0),
        LibraryQuery(page_size=101),
        LibraryQuery(min_sim=-0.1),
        LibraryQuery(min_sim=1.1),
        LibraryQuery(text=123),
        LibraryQuery(image="frame.png"),  # embedder_model mandatory for images
        LibraryQuery(image=[]),
    ):
        with pytest.raises(InvalidAssetError):
            retriever.search("video", bad)

    with pytest.raises(InvalidAssetError):
        retriever.search("video", LibraryQuery(), page=2)


def test_text_query_filters_and_paginates(library):
    _register_shots(library)
    result = query_library(library, "video", text="warm kitchen", page=2,
                           page_size=1)
    assert result["total"] == 2
    assert result["page"] == 2 and result["page_size"] == 1
    assert len(result["items"]) == 1
    assert result["mode"] == "text"
    assert result["degraded"] is False
    # Top hit is the tighter description; all rows carry a similarity score.
    assert result["items"][0]["score"] > 0

    filtered = query_library(library, "video", text="warm kitchen",
                             reason_code="FACE_MISMATCH")
    assert [item["asset_id"] for item in filtered["items"]] == ["sh_001"]


def test_metadata_query_carries_no_fake_similarity(library):
    _register_shots(library)
    result = query_library(library, "video", tags=["warm kitchen"])
    assert {item["asset_id"] for item in result["items"]} == {"sh_001", "sh_003"}
    assert all("score" not in item for item in result["items"])
    assert result["mode"] == "metadata"
    # min_sim must not silently pass rows that never had a similarity.
    assert query_library(library, "video", min_sim=0.1)["items"] == []


def test_image_query_uses_injected_embedder(library):
    library.register("image", "face_a", {}, embedding=[1.0, 0.0],
                     embedder_model="clip-v1")
    library.register("image", "face_b", {}, embedding=[0.0, 1.0],
                     embedder_model="clip-v1")

    retriever = LibraryRetriever(
        library, image_embedder=lambda paths: np.array([[1.0, 0.0]]))
    result = retriever.search(
        "image", LibraryQuery(image="warm_kitchen.png",
                              embedder_model="clip-v1", min_sim=0.9))
    assert result.mode == "image"
    assert result.degraded is False
    assert [item["asset_id"] for item in result.items] == ["face_a"]
    assert retriever.last_embed_ms is not None

    # Hybrid text + image fuses channels (best score per asset).
    library.register("video", "sh_warm", {"tags": ["warm kitchen"]})
    hybrid = retriever.search(
        "video", LibraryQuery(text="warm kitchen", image=[1.0, 0.0],
                              embedder_model="clip-v1"))
    assert hybrid.mode == "hybrid"


def test_image_query_degrades_to_tags_when_embedder_absent(library):
    _register_shots(library)
    retriever = LibraryRetriever(library, image_embedder=None)
    result = retriever.search(
        "video", LibraryQuery(image="warm_kitchen_night.png",
                              embedder_model="clip-v1"))
    assert result.degraded is True
    assert result.degraded_reason == DEGRADED_EMBEDDER_UNAVAILABLE
    assert result.mode == "tags"
    # Filename tokens fed the tag channel; the warm-kitchen shots are found.
    assert {item["asset_id"] for item in result.items} == {"sh_001", "sh_003"}


def test_image_query_degrades_when_embedder_exceeds_budget(library):
    library.register("image", "face_a", {}, embedding=[1.0, 0.0],
                     embedder_model="clip-v1")

    def slow_embedder(paths):
        time.sleep(0.01)
        return np.array([[1.0, 0.0]])

    retriever = LibraryRetriever(
        library, image_embedder=slow_embedder, max_embed_seconds=0.001)
    result = retriever.search(
        "image", LibraryQuery(image="face_a.png", embedder_model="clip-v1"))
    assert result.degraded is True
    assert result.degraded_reason == DEGRADED_EMBEDDER_SLOW
    assert result.mode == "tags"
    assert retriever.stats()["last_embed_ms"] is not None


def test_retrieval_performance_baseline_is_measured(library):
    _register_shots(library)
    retriever = LibraryRetriever(library)
    result = retriever.search("video", LibraryQuery(text="warm kitchen"))
    assert result.elapsed_ms >= 0.0
    stats = retriever.stats()
    assert stats["searches"] == 1
    assert stats["last_elapsed_ms"] == result.elapsed_ms
    assert stats["image_embedder"] == "absent"


# ---------------------------------------------------------------------------
# LIB-07 — cache vs library: independent keys, budgets and switches
# ---------------------------------------------------------------------------

def test_cache_is_off_by_default_and_independent_of_library(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENMONTAGE_ASSET_CACHE", raising=False)
    monkeypatch.delenv("OPENMONTAGE_ASSET_CACHE_DIR", raising=False)
    reset_default_asset_cache()
    assert asset_cache_enabled() is False
    assert default_asset_cache_dir() != tmp_path / "library"

    # The library is always on (semantic + tags), regardless of the cache flag.
    library = AssetLibrary(tmp_path / "library")
    library.register("image", "ch_ling", {"tags": ["lead"]})
    assert [a["asset_id"] for a in library.query("image", text="lead")] == ["ch_ling"]

    # Pointing the cache somewhere else never redirects library state.
    monkeypatch.setenv("OPENMONTAGE_ASSET_CACHE_DIR", str(tmp_path / "cache"))
    assert default_asset_cache_dir() == tmp_path / "cache"
    assert library.library_dir == tmp_path / "library"
    assert not (tmp_path / "cache" / "image").exists()


def test_cache_keys_are_content_addressed_not_path_addressed(tmp_path):
    cache = AssetCache(cache_dir=tmp_path / "cache", max_total_bytes=1024)
    first = tmp_path / "a" / "gen.bin"
    second = tmp_path / "b" / "copy.bin"
    first.parent.mkdir()
    second.parent.mkdir()
    payload = b"same generated bytes"
    first.write_bytes(payload)
    second.write_bytes(payload)

    key = hashlib.sha256(payload).hexdigest()
    assert cache.store(key, first, tool_name="mock", cost_usd=0.25) is True
    restored = tmp_path / "out" / "restored.bin"
    meta = cache.try_restore(key, restored)
    assert meta is not None
    assert restored.read_bytes() == payload
    assert meta["cost_usd"] == 0.25

    # Different content at the same directory is a different key.
    other = tmp_path / "a" / "other.bin"
    other.write_bytes(b"different bytes")
    other_key = hashlib.sha256(b"different bytes").hexdigest()
    assert other_key != key
    assert cache.try_restore(other_key, tmp_path / "out" / "other.bin") is None


def test_cache_lru_eviction_does_not_touch_library(tmp_path):
    library = AssetLibrary(tmp_path / "library")
    library.register("image", "ch_ling", {"tags": ["lead"]})
    library.promote("ch_ling")
    corpus_before = (library.corpus_dir / "index.jsonl").read_bytes()

    cache = AssetCache(cache_dir=tmp_path / "cache", max_total_bytes=10)
    old_file = tmp_path / "old.bin"
    old_file.write_bytes(b"0123456789")
    new_file = tmp_path / "new.bin"
    new_file.write_bytes(b"ABCDEFGHIJ")
    old_key = hashlib.sha256(old_file.read_bytes()).hexdigest()
    new_key = hashlib.sha256(new_file.read_bytes()).hexdigest()

    assert cache.store(old_key, old_file) is True
    assert cache.store(new_key, new_file) is True  # forces eviction of the LRU

    assert cache.try_restore(old_key, tmp_path / "out_old.bin") is None  # evicted
    assert cache.try_restore(new_key, tmp_path / "out_new.bin") is not None
    assert cache.stats()["evictions_this_session"] >= 1

    # Library state is a different store with its own (unbounded) budget.
    assert (library.corpus_dir / "index.jsonl").read_bytes() == corpus_before
    assert library.get("ch_ling")["status"] == "promoted"
    assert library.status_index()["ch_ling"]["status"] == "promoted"
    assert not (tmp_path / "cache" / "asset.json").exists()
    assert library.query("image", character_id="ch_ling") == []
    assert [a["asset_id"] for a in library.query("image", tags=["lead"])] == ["ch_ling"]


def test_library_writes_never_appear_in_cache_namespace(tmp_path):
    library = AssetLibrary(tmp_path / "library")
    library.register("video", "sh_001", {"tags": ["hook"]})
    cache = AssetCache(cache_dir=tmp_path / "cache", max_total_bytes=1024)

    # Library registration does not create cache entries...
    assert cache.stats()["entry_count"] == 0
    # ...and a cached artifact is not an asset the library can retrieve.
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"generated")
    key = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert cache.store(key, artifact) is True
    with pytest.raises(AssetNotFoundError):
        library.get(key)

    # The two stores have distinct namespaces on disk.
    assert (library.library_dir / "video" / "sh_001" / "asset.json").is_file()
    assert not (library.library_dir / "asset_manifest.jsonl").exists()
    assert not (tmp_path / "cache" / "video").exists()