"""DATA-01 contract tests: legal trend/trope data-source interface (D11).

The brief must be able to ask for trends/tropes without knowing which source
supplied them. These tests pin the adapter protocol, the registry, the offline
manual source, the provenance fields a legal source must record, and the
UNKNOWN rule for unavailable data. They also pin that no network client exists
on this path — DATA-05 (a real legal source) is not connected yet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import data_source
from lib.data_source import (
    DATA_05_SOURCE_ID,
    MANUAL_SOURCE_ID,
    UNKNOWN,
    DataSourceAdapter,
    DataSourceError,
    DataSourceRegistry,
    DuplicateSourceError,
    InvalidDataEntryError,
    ManualSource,
    ManualTropeSource,
    SourceUnavailableError,
    UnconfiguredLegalSource,
    UnknownSourceError,
    default_registry,
)

FILE_PROVENANCE = {
    "source_name": "Curated Trope Notes",
    "source_url": "https://example.invalid/curated-tropes",
    "license": "CC-BY-4.0",
    "collected_at": "2026-11-01T09:30:00Z",
    "retrieval_method": "manual curation from licensed summaries",
    "definition": "trope = recurring narrative pattern; curated_count = people who listed it",
}

ENTRIES = [
    {
        "entry_id": "trope-001",
        "kind": "trope",
        "name": "Secret Heir",
        "description": "Hidden noble lineage revealed late.",
        "genre": "romance",
        "platform": "TikTok",
        "territory": "US",
        "period": "2026-Q3",
        "metrics": {"curated_count": 7},
        "definition": "Share of curators who independently listed this trope.",
        "tags": ["inheritance", "reveal"],
    },
    {
        "entry_id": "trope-002",
        "kind": "trope",
        "name": "Contract Marriage",
        "source": {"license": "CC0-1.0", "source_url": "https://example.invalid/cc0-list"},
    },
    {
        "entry_id": "trend-001",
        "kind": "trend",
        "name": "Kitchen Confession",
        "genre": "drama",
        "platform": "TikTok",
        "metrics": {"candidate_count": 3},
    },
    {
        # Deliberately missing: name, description, genre, platform, territory,
        # period, metrics, definition. Every one must surface as UNKNOWN / empty.
        "entry_id": "trope-003",
        "kind": "trope",
    },
]


def _write_manual(tmp_path: Path, document=None) -> Path:
    path = tmp_path / "tropes.json"
    if document is None:
        document = {"source": FILE_PROVENANCE, "entries": ENTRIES}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def source(tmp_path) -> ManualSource:
    return ManualSource(_write_manual(tmp_path))


@pytest.fixture
def registry(source) -> DataSourceRegistry:
    reg = DataSourceRegistry()
    reg.register(source)
    return reg


# --- adapter protocol / registry ---

def test_manual_source_satisfies_adapter_protocol(source):
    assert isinstance(source, DataSourceAdapter)
    assert ManualTropeSource is ManualSource  # DATA-01 design name
    assert source.source_id == MANUAL_SOURCE_ID


def test_unconfigured_legal_source_satisfies_adapter_protocol():
    assert isinstance(UnconfiguredLegalSource(), DataSourceAdapter)


def test_registry_registers_lists_and_describes(source):
    reg = DataSourceRegistry()
    assert reg.register(source) == MANUAL_SOURCE_ID
    assert reg.source_ids() == [MANUAL_SOURCE_ID]
    assert reg.get(MANUAL_SOURCE_ID) is source
    described = reg.describe()
    assert described[0]["source_id"] == MANUAL_SOURCE_ID
    assert described[0]["adapter"] == "manual"
    assert described[0]["offline"] is True
    assert described[0]["license"] == "CC-BY-4.0"


def test_registry_rejects_duplicate_and_unknown_sources(source):
    reg = DataSourceRegistry()
    reg.register(source)
    with pytest.raises(DuplicateSourceError):
        reg.register(ManualSource("other.json"))
    with pytest.raises(UnknownSourceError):
        reg.get("nope")
    with pytest.raises(UnknownSourceError):
        reg.fetch_trends("nope")


def test_registry_does_not_evaluate_adapter_members_while_registering(tmp_path):
    # Python 3.11 runs properties during Protocol isinstance checks; registering
    # an adapter must not read (or require) its file.
    reg = DataSourceRegistry()
    reg.register(ManualSource(tmp_path / "not-created-yet.json"))
    assert reg.source_ids() == [MANUAL_SOURCE_ID]


def test_registry_routes_fetch_calls_by_source_id(registry):
    trends = registry.fetch_trends(MANUAL_SOURCE_ID)
    tropes = registry.fetch_tropes(MANUAL_SOURCE_ID)
    assert [entry.kind for entry in trends] == ["trend"]
    assert [entry.entry_id for entry in tropes] == ["trope-001", "trope-002", "trope-003"]


def test_default_registry_is_manual_offline(tmp_path):
    empty = default_registry()
    assert empty.source_ids() == []
    reg = default_registry(manual_file=_write_manual(tmp_path))
    assert reg.source_ids() == [MANUAL_SOURCE_ID]
    assert reg.fetch_trends(MANUAL_SOURCE_ID)[0].entry_id == "trend-001"


# --- manual adapter reads local entries ---

def test_manual_source_reads_local_entries(source):
    tropes = source.fetch_tropes()
    assert isinstance(tropes, list) and len(tropes) == 3
    first = tropes[0]
    assert first.name == "Secret Heir"
    assert first.genre == "romance"
    assert first.period == "2026-Q3"
    assert first.metrics == {"curated_count": 7.0}
    assert first.metadata == {"tags": ["inheritance", "reveal"]}
    assert json.loads(json.dumps(first.to_dict()))["entry_id"] == "trope-001"


def test_manual_source_filters_and_limits(source):
    assert [e.entry_id for e in source.fetch_tropes(filters={"platform": "tiktok"})] == [
        "trope-001"]
    assert [e.entry_id for e in source.fetch_tropes(limit=2)] == [
        "trope-001", "trope-002"]
    assert source.fetch_tropes(limit=0) == []
    assert source.fetch_tropes(filters={"genre": "horror"}) == []


def test_manual_source_accepts_a_plain_entry_list(tmp_path):
    path = _write_manual(tmp_path, document=[ENTRIES[2]])
    trends = ManualSource(path).fetch_trends()
    assert [e.entry_id for e in trends] == ["trend-001"]


def test_manual_source_rereads_file_on_each_call(tmp_path):
    path = _write_manual(tmp_path)
    manual = ManualSource(path)
    assert len(manual.fetch_tropes()) == 3
    path.write_text(json.dumps({"source": FILE_PROVENANCE, "entries": ENTRIES[:1]}),
                    encoding="utf-8")
    assert len(manual.fetch_tropes()) == 1


# --- provenance: source / license / collected_at / definition ---

def test_entries_record_source_license_collected_at_and_definition(source):
    entry = source.fetch_tropes()[0]
    provenance = entry.provenance
    assert provenance.source_name == "Curated Trope Notes"
    assert provenance.source_url == "https://example.invalid/curated-tropes"
    assert provenance.license == "CC-BY-4.0"
    assert provenance.collected_at == "2026-11-01T09:30:00Z"
    assert provenance.retrieval_method.startswith("manual curation")
    assert entry.definition.startswith("Share of curators")


def test_entry_source_overrides_file_provenance_only_where_given(source):
    entry = source.fetch_tropes()[1]
    assert entry.provenance.license == "CC0-1.0"
    assert entry.provenance.source_url == "https://example.invalid/cc0-list"
    # Unspecified fields still come from the file-level source block.
    assert entry.provenance.source_name == "Curated Trope Notes"
    assert entry.provenance.collected_at == "2026-11-01T09:30:00Z"
    # No entry definition -> the source-level 口径 applies.
    assert entry.definition == FILE_PROVENANCE["definition"]


# --- UNKNOWN: never fabricate unavailable data ---

def test_missing_entry_fields_are_unknown_not_invented(source):
    entry = source.fetch_tropes()[2]
    assert entry.name == UNKNOWN
    assert entry.description == UNKNOWN
    assert entry.genre == UNKNOWN
    assert entry.platform == UNKNOWN
    assert entry.territory == UNKNOWN
    assert entry.period == UNKNOWN
    assert entry.definition == FILE_PROVENANCE["definition"]  # file-level 口径
    assert entry.metrics == {}  # not measured is not zero


def test_missing_file_provenance_is_unknown(tmp_path):
    path = _write_manual(tmp_path, document={"entries": ENTRIES[:1]})
    provenance = ManualSource(path).fetch_tropes()[0].provenance
    assert provenance == data_source.SourceProvenance()
    for value in provenance.to_dict().values():
        assert value == UNKNOWN


def test_unconfigured_legal_source_fails_instead_of_fabricating():
    blocker = UnconfiguredLegalSource()
    assert blocker.source_id == DATA_05_SOURCE_ID
    assert blocker.describe()["status"] == "BLOCKED"
    assert blocker.provenance == data_source.SourceProvenance()
    with pytest.raises(SourceUnavailableError):
        blocker.fetch_trends()
    with pytest.raises(SourceUnavailableError):
        blocker.fetch_tropes()


# --- fail loudly on malformed input ---

@pytest.mark.parametrize("collected_at", [
    "2026-11-01",              # date only, no timezone
    "2026-11-01T09:30:00",     # naive timestamp
    "yesterday",
])
def test_collected_at_must_be_timezone_aware_iso8601(tmp_path, collected_at):
    document = {"source": {"collected_at": collected_at}, "entries": []}
    with pytest.raises(InvalidDataEntryError):
        ManualSource(_write_manual(tmp_path, document)).provenance


@pytest.mark.parametrize("entry", [
    {"kind": "trope"},                                   # no entry_id
    {"entry_id": "x", "kind": "meme"},                   # unknown kind
    {"entry_id": "../escape", "kind": "trope"},          # unsafe id
    {"entry_id": "x", "kind": "trope", "metrics": {"v": "high"}},  # non-numeric metric
])
def test_malformed_entries_raise(tmp_path, entry):
    path = _write_manual(tmp_path, document={"entries": [entry]})
    with pytest.raises(InvalidDataEntryError):
        ManualSource(path).fetch_tropes()


def test_malformed_manual_file_raises(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(DataSourceError):
        ManualSource(bad_json).fetch_tropes()

    no_entries = _write_manual(tmp_path, document={"source": FILE_PROVENANCE})
    with pytest.raises(InvalidDataEntryError):
        ManualSource(no_entries).fetch_tropes()

    with pytest.raises(SourceUnavailableError):
        ManualSource(tmp_path / "missing.json").fetch_tropes()


def test_unknown_filter_and_bad_limit_raise(source):
    with pytest.raises(InvalidDataEntryError):
        source.fetch_tropes(filters={"vibes": "good"})
    with pytest.raises(InvalidDataEntryError):
        source.fetch_tropes(limit=-1)


# --- M0: no network path ---

def test_data_source_module_has_no_network_client():
    text = Path(data_source.__file__).read_text(encoding="utf-8")
    for banned in ("socket", "requests", "urllib", "httpx", "http.client",
                   "aiohttp", "urlopen"):
        assert banned not in text, f"DATA-01 must stay offline; found {banned!r}"