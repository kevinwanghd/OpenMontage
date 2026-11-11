"""Legal trend/trope data-source adapter interface for the short-drama pipeline.

DATA-01 (P0) is a **source-agnostic interface only** (D11): the brief pipeline
asks for trends/tropes through this seam and never imports a concrete source.
The default adapter is :class:`ManualSource`, which reads a human-curated local
JSON file — an offline, auditable way to supply a small legal set of tropes.

Every entry carries its provenance: where it came from, under which license it
was collected, when it was collected, and the measurement definition (口径) of
its metrics. Fields that are not available are returned as :data:`UNKNOWN`
rather than guessed, because a fabricated trend metric is worse than a gap
(NFR-09 / R21).

DATA-05 (real legal source) is **not implemented here**. When a legal source is
selected, implement :class:`DataSourceAdapter`, verify its license/ToS evidence,
and register it next to the manual adapter. :class:`UnconfiguredLegalSource`
marks that seam and fails loudly instead of inventing data.

M0: local files only, zero external API calls, no network imports.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

UNKNOWN = "UNKNOWN"
ENTRY_KINDS = ("trend", "trope")
MANUAL_SOURCE_ID = "manual"
DATA_05_SOURCE_ID = "DATA-05"
DEFAULT_VERSION = "1.0"

_ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FILTER_FIELDS = ("entry_id", "kind", "name", "genre", "platform", "territory", "period")
_KNOWN_ENTRY_KEYS = frozenset({
    "entry_id", "id", "kind", "name", "description", "genre", "platform",
    "territory", "period", "metrics", "definition", "source",
})


class DataSourceError(Exception):
    """Base data-source error."""


class InvalidDataEntryError(DataSourceError):
    """An entry or adapter record is malformed. Never silently repaired."""


class DuplicateSourceError(DataSourceError):
    """source_id is already registered; adapters are not silently replaced."""


class UnknownSourceError(DataSourceError, KeyError):
    """source_id is not registered."""


class SourceUnavailableError(DataSourceError):
    """The source exists conceptually but cannot be used (e.g. DATA-05)."""


def _text(value: Any, field_name: str) -> str:
    """Normalise an optional scalar to text; missing values become UNKNOWN."""
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        raise InvalidDataEntryError(f"{field_name} must be text, got {value!r}")
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text if text else UNKNOWN
    raise InvalidDataEntryError(f"{field_name} must be text, got {type(value).__name__}")


def _collected_at(value: Any) -> str:
    """Record an ISO-8601 timestamp; require an explicit timezone, else UNKNOWN."""
    text = _text(value, "collected_at")
    if text == UNKNOWN:
        return UNKNOWN
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InvalidDataEntryError(
            f"collected_at must be ISO-8601, got {text!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidDataEntryError(
            f"collected_at must include a timezone, got {text!r}")
    return text


@dataclass(frozen=True)
class SourceProvenance:
    """Where a record came from and under what terms it may be used."""

    source_name: str = UNKNOWN
    source_url: str = UNKNOWN
    license: str = UNKNOWN
    collected_at: str = UNKNOWN
    retrieval_method: str = UNKNOWN

    @classmethod
    def from_record(cls, record: Any = None) -> "SourceProvenance":
        if record is None:
            return cls()
        if not isinstance(record, Mapping):
            raise InvalidDataEntryError("source must be an object")
        return cls(
            source_name=_text(record.get("source_name", record.get("name")), "source_name"),
            source_url=_text(record.get("source_url", record.get("url")), "source_url"),
            license=_text(record.get("license"), "license"),
            collected_at=_collected_at(record.get("collected_at")),
            retrieval_method=_text(record.get("retrieval_method"), "retrieval_method"),
        )

    def merged_with(self, override: Any) -> "SourceProvenance":
        """Apply an entry-level source block on top of the file-level one."""
        if override is None:
            return self
        if not isinstance(override, Mapping):
            raise InvalidDataEntryError("entry source must be an object")
        values = self.to_dict()
        for key, value in override.items():
            values[key] = value
        return SourceProvenance.from_record(values)

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "source_url": self.source_url,
            "license": self.license,
            "collected_at": self.collected_at,
            "retrieval_method": self.retrieval_method,
        }


def _metrics(record: Any) -> dict[str, float]:
    """Keep measured numbers only; an unmeasured metric is absent, not zero."""
    if record is None:
        return {}
    if not isinstance(record, Mapping):
        raise InvalidDataEntryError("metrics must be an object")
    out: dict[str, float] = {}
    for key, value in record.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidDataEntryError(
                f"metric {key!r} must be a number, got {value!r}")
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidDataEntryError(f"metric {key!r} must be finite")
        out[str(key)] = float(value)
    return out


@dataclass(frozen=True)
class DataEntry:
    """One trend or trope, with the definition needed to read its metrics."""

    entry_id: str
    kind: str
    name: str = UNKNOWN
    description: str = UNKNOWN
    genre: str = UNKNOWN
    platform: str = UNKNOWN
    territory: str = UNKNOWN
    period: str = UNKNOWN
    metrics: Mapping[str, float] = field(default_factory=dict)
    definition: str = UNKNOWN
    provenance: SourceProvenance = field(default_factory=SourceProvenance)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(
        cls,
        record: Any,
        *,
        default_provenance: Optional[SourceProvenance] = None,
        default_definition: str = UNKNOWN,
    ) -> "DataEntry":
        if not isinstance(record, Mapping):
            raise InvalidDataEntryError("entry must be an object")
        raw_id = record.get("entry_id", record.get("id"))
        entry_id = _text(raw_id, "entry_id")
        if entry_id == UNKNOWN or not _ENTRY_ID.fullmatch(entry_id):
            raise InvalidDataEntryError(
                f"entry_id must match {_ENTRY_ID.pattern}, got {raw_id!r}")
        kind = _text(record.get("kind"), "kind")
        if kind not in ENTRY_KINDS:
            raise InvalidDataEntryError(
                f"kind must be one of {ENTRY_KINDS}, got {kind!r}")
        provenance = (default_provenance or SourceProvenance()).merged_with(
            record.get("source"))
        metadata = {
            key: value for key, value in record.items()
            if key not in _KNOWN_ENTRY_KEYS
        }
        try:
            json.dumps(metadata)
        except (TypeError, ValueError) as exc:
            raise InvalidDataEntryError(f"extra entry fields must be JSON: {exc}") from exc
        return cls(
            entry_id=entry_id,
            kind=kind,
            name=_text(record.get("name"), "name"),
            description=_text(record.get("description"), "description"),
            genre=_text(record.get("genre"), "genre"),
            platform=_text(record.get("platform"), "platform"),
            territory=_text(record.get("territory"), "territory"),
            period=_text(record.get("period"), "period"),
            metrics=_metrics(record.get("metrics")),
            definition=_text(record.get("definition", default_definition), "definition"),
            provenance=provenance,
            metadata=metadata,
        )

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "genre": self.genre,
            "platform": self.platform,
            "territory": self.territory,
            "period": self.period,
            "metrics": dict(self.metrics),
            "definition": self.definition,
            "provenance": self.provenance.to_dict(),
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class DataSourceAdapter(Protocol):
    """Source-agnostic contract every trend/trope source implements.

    Callers depend on this protocol, never on a concrete adapter, so swapping
    the manual file for a verified DATA-05 source changes no pipeline code
    (DATA-01b).
    """

    source_id: str

    @property
    def provenance(self) -> SourceProvenance:
        ...

    def describe(self) -> dict:
        ...

    def fetch_trends(
        self,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> list[DataEntry]:
        ...

    def fetch_tropes(
        self,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> list[DataEntry]:
        ...


_ADAPTER_METHODS = ("describe", "fetch_trends", "fetch_tropes")


def _implements_adapter(adapter: Any) -> bool:
    """Structural check that never *calls* an adapter's members.

    ``isinstance(x, DataSourceAdapter)`` on Python 3.11 evaluates properties
    such as ``provenance``, which reads a file and can therefore raise.
    Registration must stay side-effect free, so inspect the class instead.
    """
    if not isinstance(getattr(adapter, "source_id", None), str):
        return False
    if not hasattr(type(adapter), "provenance"):
        return False
    return all(callable(getattr(adapter, name, None)) for name in _ADAPTER_METHODS)


def _limit(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidDataEntryError(f"limit must be a non-negative int, got {value!r}")
    return value


def _matches(entry: DataEntry, filters: Optional[Mapping[str, Any]]) -> bool:
    if not filters:
        return True
    if not isinstance(filters, Mapping):
        raise InvalidDataEntryError("filters must be an object")
    for key, expected in filters.items():
        if key not in _FILTER_FIELDS:
            raise InvalidDataEntryError(
                f"unknown filter {key!r}; allowed: {_FILTER_FIELDS}")
        actual = getattr(entry, key)
        if str(actual).strip().lower() != str(expected).strip().lower():
            return False
    return True


class ManualSource:
    """Offline adapter for a human-curated local JSON file.

    File format::

        {
          "source": {"source_name": "...", "source_url": "...",
                     "license": "...", "collected_at": "2026-11-01T00:00:00Z",
                     "retrieval_method": "manual", "definition": "..."},
          "entries": [
            {"entry_id": "trope-001", "kind": "trope", "name": "...",
             "genre": "...", "definition": "...", "metrics": {"..." : 1.0},
             "source": {"license": "..."}}
          ]
        }

    A top-level list of entries is also accepted (provenance is then UNKNOWN).
    The file is re-read on every call so a human edit takes effect without a
    restart. ``ManualTropeSource`` is the DATA-01 name for this adapter.
    """

    def __init__(self, path, *, source_id: str = MANUAL_SOURCE_ID):
        if not isinstance(source_id, str) or not source_id.strip():
            raise InvalidDataEntryError("source_id must be a non-empty string")
        self.source_id = source_id
        self.path = Path(path)

    def _load(self) -> tuple[SourceProvenance, str, list]:
        if not self.path.is_file():
            raise SourceUnavailableError(f"manual source file not found: {self.path}")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataSourceError(f"cannot read manual source {self.path}: {exc}") from exc

        if isinstance(document, list):
            return SourceProvenance(), UNKNOWN, document
        if not isinstance(document, Mapping):
            raise InvalidDataEntryError(
                "manual source must be an object or a list of entries")
        entries = document.get("entries")
        if not isinstance(entries, list):
            raise InvalidDataEntryError("manual source must contain an 'entries' list")
        raw_source = document.get("source")
        provenance = SourceProvenance.from_record(raw_source)
        definition = (_text(raw_source.get("definition"), "definition")
                      if isinstance(raw_source, Mapping) else UNKNOWN)
        return provenance, definition, entries

    @property
    def provenance(self) -> SourceProvenance:
        return self._load()[0]

    def describe(self) -> dict:
        provenance, definition, entries = self._load()
        out = {
            "source_id": self.source_id,
            "adapter": "manual",
            "offline": True,
            "path": str(self.path),
            "entry_count": len(entries),
            "definition": definition,
        }
        out.update(provenance.to_dict())
        return out

    def _fetch(
        self,
        kind: str,
        filters: Optional[Mapping[str, Any]],
        limit: Optional[int],
    ) -> list[DataEntry]:
        provenance, definition, records = self._load()
        max_items = _limit(limit)
        entries: list[DataEntry] = []
        for record in records:
            entry = DataEntry.from_record(
                record, default_provenance=provenance, default_definition=definition)
            if entry.kind == kind:
                entries.append(entry)
        selected = [entry for entry in entries if _matches(entry, filters)]
        return selected if max_items is None else selected[:max_items]

    def fetch_trends(self, *, filters=None, limit=None) -> list[DataEntry]:
        return self._fetch("trend", filters, limit)

    def fetch_tropes(self, *, filters=None, limit=None) -> list[DataEntry]:
        return self._fetch("trope", filters, limit)


ManualTropeSource = ManualSource


class UnconfiguredLegalSource:
    """DATA-05 seam: a real legal source is not selected or verified yet.

    It is deliberately unusable. Registering a verified DATA-05 adapter under
    :data:`DATA_05_SOURCE_ID` replaces this object; until then, callers get an
    explicit ``SourceUnavailableError`` instead of fabricated trends.
    """

    source_id = DATA_05_SOURCE_ID
    status = "BLOCKED"

    @property
    def provenance(self) -> SourceProvenance:
        return SourceProvenance()

    def describe(self) -> dict:
        return {
            "source_id": self.source_id,
            "adapter": "unconfigured-legal-source",
            "status": self.status,
            "reason": "DATA-05 legal source not selected or licensed yet",
        }

    def _unavailable(self) -> None:
        raise SourceUnavailableError(
            "DATA-05 legal source is not configured; select a licensed source "
            "and register its adapter before requesting live trends")

    def fetch_trends(self, *, filters=None, limit=None) -> list[DataEntry]:
        self._unavailable()

    def fetch_tropes(self, *, filters=None, limit=None) -> list[DataEntry]:
        self._unavailable()


class DataSourceRegistry:
    """Explicit adapter registry; the brief never hardcodes a source id."""

    def __init__(self) -> None:
        self._adapters: dict[str, DataSourceAdapter] = {}

    def register(self, adapter: DataSourceAdapter) -> str:
        if not _implements_adapter(adapter):
            raise InvalidDataEntryError(
                "adapter must implement source_id, provenance, describe, "
                "fetch_trends and fetch_tropes")
        source_id = getattr(adapter, "source_id", None)
        if not isinstance(source_id, str) or not source_id.strip():
            raise InvalidDataEntryError("adapter.source_id must be a non-empty string")
        if source_id in self._adapters:
            raise DuplicateSourceError(f"source {source_id!r} is already registered")
        self._adapters[source_id] = adapter
        return source_id

    def get(self, source_id: str) -> DataSourceAdapter:
        try:
            return self._adapters[source_id]
        except KeyError:
            raise UnknownSourceError(
                f"source {source_id!r} is not registered; "
                f"known: {sorted(self._adapters)}") from None

    def source_ids(self) -> list[str]:
        return sorted(self._adapters)

    def describe(self) -> list[dict]:
        return [self._adapters[source_id].describe()
                for source_id in self.source_ids()]

    def fetch_trends(
        self,
        source_id: str,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> list[DataEntry]:
        return self.get(source_id).fetch_trends(filters=filters, limit=limit)

    def fetch_tropes(
        self,
        source_id: str,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> list[DataEntry]:
        return self.get(source_id).fetch_tropes(filters=filters, limit=limit)


def default_registry(*, manual_file=None) -> DataSourceRegistry:
    """Registry with the manual adapter registered when a file is supplied.

    The default is manual/offline (D11). Callers may register additional
    adapters; nothing else in the pipeline changes.
    """
    registry = DataSourceRegistry()
    if manual_file is not None:
        registry.register(ManualSource(manual_file))
    return registry