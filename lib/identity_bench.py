"""CHAR-10 / CHAR-07: offline identity-comparison bench scaffold.

This module is the M1 *measurement* seam that the M0 declaration
(:mod:`lib.identity_candidates`) deliberately left empty. It does three things
and nothing else:

1. Reads the candidate declaration, picks a license-clean CPU candidate and
   keeps the declared detection-only + human-identity fallback one call away.
2. Injects an ``embedder`` (a real model adapter or a deterministic fake) to
   turn labelled ``(sample_a, sample_b)`` pairs into similarity scores, then
   reports accuracy, a threshold sweep and the accuracy-maximising threshold
   (CHAR-07: the threshold belongs to the embedder/model, never a global
   constant).
3. Emits a structured report whose ``verification_status`` is honest about how
   it was produced: an unmeasured run is ``UNKNOWN``, a run against a
   deterministic fake is ``MOCKED``, and only a real embedder may be
   ``VERIFIED``. ``GREEN``/``PASS``/``OK`` are forbidden everywhere.

Zero external API calls, zero model downloads. The module never imports a
network client; the test suite's session-wide socket guard is the enforcement.

Fallback semantics (CHAR-06): when no candidate is usable and measured, the
report is ``UNKNOWN`` with ``detection-only + human identity`` -- never a
synthetic PASS score.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import yaml

from lib.identity_candidates import (
    DEFAULT_CANDIDATES_PATH,
    UNKNOWN,
    IdentityCandidatesError,
    load_candidates,
    usable_candidates,
    validate_candidates,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_PATH = _REPO_ROOT / "data" / "identity" / "char10_bench_fixture.yaml"

#: Similarity metrics. Both are normalised so that *higher means more same*.
METRICS = ("cosine", "euclidean")

#: How the embeddings were produced. This drives ``verification_status``.
EMBEDDER_KINDS = ("real", "mock")

#: A measured fake can never outrank MOCKED; an unmeasured run is UNKNOWN.
STATUS_BY_KIND = {"real": "VERIFIED", "mock": "MOCKED"}

#: Explicitly banned verdicts: GREEN is a doctor verdict, not a measurement.
FORBIDDEN_STATUSES = ("GREEN", "PASS", "OK")

#: Every report must carry these keys (the contract the task asks for).
REQUIRED_REPORT_FIELDS = (
    "candidate",
    "license",
    "device",
    "embedder_model",
    "accuracy",
    "threshold",
    "bias_slices",
    "verification_status",
    "generated_at",
    "notes",
)

Embedder = Callable[[str], Sequence[float]]


class IdentityBenchError(Exception):
    """Base error for the identity bench."""


class InvalidFixtureError(IdentityBenchError):
    """The labelled fixture violates the bench contract."""


class CalibrationError(IdentityBenchError):
    """The scored pairs cannot support a threshold calibration."""


# ---------------------------------------------------------------------------
# Fixture loading and validation
# ---------------------------------------------------------------------------


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidFixtureError(f"{where} must be an object")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidFixtureError(f"{where} must be a non-empty string")
    return value.strip()


def _vector(value: Any, where: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or not value:
        raise InvalidFixtureError(f"{where} must be a non-empty list of numbers")
    out: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise InvalidFixtureError(f"{where}[{index}] must be a number")
        if not math.isfinite(float(item)):
            raise InvalidFixtureError(f"{where}[{index}] must be finite")
        out.append(float(item))
    return out


@dataclass(frozen=True)
class LabelledPair:
    """One labelled ``(sample_a, sample_b)`` comparison."""

    pair_id: str
    sample_a: str
    sample_b: str
    same_person: bool
    slices: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PairScore:
    """A pair plus the similarity the injected embedder produced for it."""

    pair_id: str
    similarity: float
    same_person: bool
    slices: Mapping[str, str] = field(default_factory=dict)


def load_fixture(path: Optional[Path] = None) -> dict:
    """Load and validate a labelled fixture YAML."""
    resolved = Path(path) if path is not None else DEFAULT_FIXTURE_PATH
    if not resolved.is_file():
        raise IdentityBenchError(f"fixture not found: {resolved}")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InvalidFixtureError(f"{resolved} is not valid YAML: {exc}") from exc
    return validate_fixture(_mapping(data, "fixture"))


def validate_fixture(data: dict) -> dict:
    """Validate the fixture contract; return ``data`` unchanged.

    The fixture is a list of labelled pairs plus (optionally) a deterministic
    ``samples`` map of sample key -> embedding vector. It carries no images and
    no model: it is the offline seam the real embedder will later fill.
    """
    _text(data.get("version"), "version")
    pairs = data.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise InvalidFixtureError("pairs must be a non-empty list")
    seen: set[str] = set()
    for index, pair in enumerate(pairs):
        where = f"pairs[{index}]"
        _mapping(pair, where)
        pair_id = _text(pair.get("id"), f"{where}.id")
        if pair_id in seen:
            raise InvalidFixtureError(f"duplicate pair id {pair_id!r}")
        seen.add(pair_id)
        _text(pair.get("sample_a"), f"{where}.sample_a")
        _text(pair.get("sample_b"), f"{where}.sample_b")
        if not isinstance(pair.get("same_person"), bool):
            raise InvalidFixtureError(f"{where}.same_person must be a boolean")
        slices = pair.get("slices")
        if slices is not None:
            _mapping(slices, f"{where}.slices")
            for key, value in slices.items():
                _text(key, f"{where}.slices key")
                _text(value, f"{where}.slices[{key}]")

    samples = data.get("samples")
    if samples is not None:
        _mapping(samples, "samples")
        for key, value in samples.items():
            _text(key, "samples key")
            _vector(value, f"samples[{key}]")
    return data


def fixture_embedder(data: dict) -> Embedder:
    """Build an embedder backed by the fixture's inline ``samples`` map.

    This is a *mock* embedder: its vectors are hand-authored, so a run over
    them is ``MOCKED`` and can never be ``VERIFIED``.
    """
    samples = _mapping(data.get("samples") or {}, "samples")

    def embed(sample: str) -> Sequence[float]:
        if sample not in samples:
            raise IdentityBenchError(f"fixture has no vector for sample {sample!r}")
        return _vector(samples[sample], f"samples[{sample}]")

    return embed


def pairs_from_fixture(data: dict) -> list[LabelledPair]:
    validate_fixture(data)
    return [
        LabelledPair(
            pair_id=pair["id"],
            sample_a=pair["sample_a"],
            sample_b=pair["sample_b"],
            same_person=pair["same_person"],
            slices=dict(pair.get("slices") or {}),
        )
        for pair in data["pairs"]
    ]


# ---------------------------------------------------------------------------
# Similarity and decision rule
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in ``[-1, 1]``; higher means more alike."""
    if len(a) != len(b):
        raise IdentityBenchError(
            f"embedding dimensions differ: {len(a)} != {len(b)}")
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise IdentityBenchError("cannot compare a zero-magnitude embedding")
    return dot / (norm_a * norm_b)


def pair_similarity(
    a: Sequence[float], b: Sequence[float], metric: str = "cosine"
) -> float:
    """Return a similarity where higher always means *more likely same person*.

    ``cosine`` maps Euclidean distance ``d`` to ``1 / (1 + d)`` so both metrics
    share one decision rule and one threshold sweep.
    """
    if metric == "cosine":
        return cosine_similarity(a, b)
    if metric == "euclidean":
        if len(a) != len(b):
            raise IdentityBenchError(
                f"embedding dimensions differ: {len(a)} != {len(b)}")
        distance = math.dist([float(x) for x in a], [float(y) for y in b])
        return 1.0 / (1.0 + distance)
    raise IdentityBenchError(f"unknown metric {metric!r}; expected one of {METRICS}")


def is_same(similarity: float, threshold: float) -> bool:
    """The single decision rule: same person iff similarity >= threshold."""
    return similarity >= threshold


def score_pairs(
    pairs: Iterable[LabelledPair], embedder: Embedder, metric: str = "cosine"
) -> list[PairScore]:
    """Embed every pair and score it, preserving the fixture's pair labels."""
    if metric not in METRICS:
        raise IdentityBenchError(f"unknown metric {metric!r}; expected one of {METRICS}")
    scored: list[PairScore] = []
    for pair in pairs:
        a = _vector(list(embedder(pair.sample_a)), f"embedder({pair.sample_a!r})")
        b = _vector(list(embedder(pair.sample_b)), f"embedder({pair.sample_b!r})")
        scored.append(PairScore(
            pair_id=pair.pair_id,
            similarity=pair_similarity(a, b, metric),
            same_person=pair.same_person,
            slices=dict(pair.slices),
        ))
    return scored


# ---------------------------------------------------------------------------
# Threshold calibration (CHAR-07: per embedder/model)
# ---------------------------------------------------------------------------


def _confusion(scores: Sequence[PairScore], threshold: float) -> dict:
    tp = fp = tn = fn = 0
    for score in scores:
        predicted = is_same(score.similarity, threshold)
        if score.same_person and predicted:
            tp += 1
        elif score.same_person and not predicted:
            fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    total = tp + fp + tn + fn
    accuracy = (tp + tn) / total if total else 0.0
    positives = tp + fn
    negatives = tn + fp
    balanced = (
        (tp / positives if positives else 0.0)
        + (tn / negatives if negatives else 0.0)
    ) / 2.0
    return {
        "threshold": threshold,
        "accuracy": accuracy,
        "balanced_accuracy": balanced,
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "count": total,
    }


def threshold_candidates(scores: Sequence[PairScore]) -> list[float]:
    """Every meaningful boundary between observed similarities (plus edges)."""
    values = sorted({score.similarity for score in scores})
    if not values:
        return []
    epsilon = 1e-9
    points = [values[0] - epsilon]
    for left, right in zip(values, values[1:]):
        points.append((left + right) / 2.0)
    points.append(values[-1] + epsilon)
    return points


def _margin(scores: Sequence[PairScore], threshold: float) -> float:
    return min(abs(score.similarity - threshold) for score in scores)


def calibrate_threshold(scores: Sequence[PairScore]) -> tuple[dict, list[dict]]:
    """Sweep thresholds and return ``(best, sweep)``.

    Calibration needs both classes: a fixture with only same (or only
    different) pairs cannot support a threshold, so it raises.
    """
    if not scores:
        raise CalibrationError("no pairs were scored")
    labels = {score.same_person for score in scores}
    if labels != {True, False}:
        raise CalibrationError(
            "threshold calibration needs both same-person and different-person "
            "pairs; fixture has " + ("only same-person pairs" if labels == {True}
                                     else "only different-person pairs"))

    sweep = [_confusion(scores, threshold) for threshold in threshold_candidates(scores)]
    # Highest accuracy wins. Ties prefer the largest margin (mid-gap), then
    # balanced accuracy -- deterministic and robust to rounding.
    best = max(
        sweep,
        key=lambda point: (
            point["accuracy"],
            _margin(scores, point["threshold"]),
            point["balanced_accuracy"],
            -point["threshold"],
        ),
    )
    best = dict(best)
    best["margin"] = _margin(scores, best["threshold"])
    return best, sweep


def slice_accuracies(
    scores: Sequence[PairScore], threshold: float
) -> dict[str, dict[str, dict]]:
    """Accuracy per declared slice value at one threshold.

    Slices are optional fixture metadata (e.g. ``pose: profile``). They are
    reported, never used to pick the threshold: CHAR-07 calibrates per model.
    """
    keys = sorted({key for score in scores for key in score.slices})
    result: dict[str, dict[str, dict]] = {}
    for key in keys:
        values = sorted({score.slices[key] for score in scores if key in score.slices})
        per_value: dict[str, dict] = {}
        for value in values:
            subset = [score for score in scores if score.slices.get(key) == value]
            if not subset:
                continue
            stats = _confusion(subset, threshold)
            per_value[value] = {
                "count": stats["count"],
                "accuracy": round(stats["accuracy"], 6),
            }
        if per_value:
            result[key] = per_value
    return result


# ---------------------------------------------------------------------------
# Report construction
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_status(status: str) -> str:
    if status in FORBIDDEN_STATUSES:
        raise IdentityBenchError(f"forbidden verification status {status!r}")
    allowed = set(STATUS_BY_KIND.values()) | {UNKNOWN}
    if status not in allowed:
        raise IdentityBenchError(
            f"verification_status must be one of {sorted(allowed)}, got {status!r}")
    return status


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _license_block(candidate: Mapping[str, Any]) -> dict:
    model = candidate.get("model") or {}
    library = candidate.get("library") or {}
    return {
        "model": model.get("license"),
        "library": library.get("license"),
        "clean_for_commercial": candidate.get("license_clean_for_commercial"),
    }


def fallback_report(
    *,
    candidate_id: Optional[str],
    reason: str,
    fallback: Mapping[str, Any],
    metric: str = "cosine",
    generated_at: Optional[str] = None,
) -> dict:
    """The honest no-tool report: detection-only plus human identity, UNKNOWN."""
    report = {
        "candidate": candidate_id,
        "license": None,
        "device": "CPU",
        "embedder_model": None,
        "accuracy": None,
        "threshold": None,
        "bias_slices": {},
        "verification_status": _check_status(UNKNOWN),
        "generated_at": generated_at or _now(),
        "notes": (
            f"{reason} No usable, license-clean, measured CPU matcher: identity "
            f"stays {UNKNOWN}. Use {fallback.get('detection', {}).get('tool')} for "
            "detection only and route identity to human review "
            "(detection-only + human identity). Never synthesise a PASS score."
        ),
        "metric": metric,
        "pair_count": 0,
        "fallback": {
            "id": fallback.get("id"),
            "identity_decision": fallback.get("identity_decision"),
            "identity_status": fallback.get("identity_status", UNKNOWN),
            "review_route": fallback.get("review_route"),
        },
        "sweep": [],
    }
    return report


def build_report(
    *,
    candidate_id: str,
    fixture: Mapping[str, Any],
    embedder: Optional[Embedder] = None,
    embedder_kind: str = "mock",
    metric: str = "cosine",
    candidates_path: Optional[Path] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """Score a fixture for one candidate and return a structured report.

    ``embedder_kind="mock"`` (the default) yields ``MOCKED``; ``"real"`` yields
    ``VERIFIED``. A missing embedder, unknown candidate, or a candidate that is
    not license-clean/CPU/unblocked yields the ``UNKNOWN`` fallback report.
    """
    if embedder_kind not in EMBEDDER_KINDS:
        raise IdentityBenchError(
            f"embedder_kind must be one of {EMBEDDER_KINDS}, got {embedder_kind!r}")
    if metric not in METRICS:
        raise IdentityBenchError(f"unknown metric {metric!r}; expected one of {METRICS}")

    document = load_candidates(candidates_path)
    validate_candidates(document)
    declaration = document["fallback"]
    rows = {row["id"]: row for row in document["candidates"]}

    candidate = rows.get(candidate_id)
    if candidate is None:
        return fallback_report(
            candidate_id=candidate_id,
            reason=f"Candidate {candidate_id!r} is not declared.",
            fallback=declaration, metric=metric, generated_at=generated_at)

    usable_ids = {row["id"] for row in usable_candidates(document)}
    if candidate_id not in usable_ids:
        return fallback_report(
            candidate_id=candidate_id,
            reason=(
                f"Candidate {candidate_id!r} is not usable "
                f"(license_clean_for_commercial="
                f"{candidate.get('license_clean_for_commercial')!r}, "
                f"verification_status={candidate.get('verification_status')!r})."
            ),
            fallback=declaration, metric=metric, generated_at=generated_at)

    if embedder is None:
        return fallback_report(
            candidate_id=candidate_id,
            reason=f"No embedder was injected for {candidate_id!r}.",
            fallback=declaration, metric=metric, generated_at=generated_at)

    pairs = pairs_from_fixture(fixture)
    scores = score_pairs(pairs, embedder, metric)
    best, sweep = calibrate_threshold(scores)
    threshold = best["threshold"]

    status = _check_status(STATUS_BY_KIND[embedder_kind])
    notes = (
        f"Fixture {fixture.get('version')!r} scored with a "
        f"{embedder_kind} embedder ({metric}); "
        f"{len(scores)} labelled pairs. Threshold calibrated for this "
        f"candidate/embedder (CHAR-07), not a global constant. "
        f"Status {status} is a measurement honesty label, never GREEN."
    )
    if fixture.get("notes"):
        notes = f"{notes} {str(fixture['notes']).strip()}"

    report = {
        "candidate": candidate_id,
        "license": _license_block(candidate),
        "device": "CPU",
        "embedder_model": (candidate.get("model") or {}).get("name"),
        "accuracy": _round(best["accuracy"]),
        "threshold": _round(threshold),
        "bias_slices": slice_accuracies(scores, threshold),
        "verification_status": status,
        "generated_at": generated_at or _now(),
        "notes": notes,
        "metric": metric,
        "pair_count": len(scores),
        "calibration": {
            "balanced_accuracy": _round(best["balanced_accuracy"]),
            "margin": _round(best["margin"]),
            "confusion": {
                "true_positive": best["true_positive"],
                "false_positive": best["false_positive"],
                "true_negative": best["true_negative"],
                "false_negative": best["false_negative"],
            },
        },
        "sweep": [
            {
                "threshold": _round(point["threshold"]),
                "accuracy": _round(point["accuracy"]),
                "balanced_accuracy": _round(point["balanced_accuracy"]),
            }
            for point in sweep
        ],
        "fallback": None,
    }
    validate_report(report)
    return report


def validate_report(report: Mapping[str, Any]) -> Mapping[str, Any]:
    """Enforce the report contract; return ``report`` unchanged."""
    missing = [key for key in REQUIRED_REPORT_FIELDS if key not in report]
    if missing:
        raise IdentityBenchError(f"report is missing required fields: {missing}")
    _check_status(str(report["verification_status"]))
    if report["verification_status"] == UNKNOWN:
        if report["accuracy"] is not None or report["threshold"] is not None:
            raise IdentityBenchError(
                "an UNKNOWN report must not carry a fabricated accuracy/threshold")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline identity-comparison bench (CHAR-10/CHAR-07). Scores a "
            "labelled fixture with an injected embedder and reports a "
            "per-model threshold. No network, no model download."))
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES_PATH),
                        help="candidate declaration YAML")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE_PATH),
                        help="labelled pair fixture YAML")
    parser.add_argument("--candidate", default=None,
                        help="candidate id (default: first usable CPU candidate)")
    parser.add_argument("--metric", choices=METRICS, default="cosine")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        document = load_candidates(Path(args.candidates))
        fixture = load_fixture(Path(args.fixture))
        candidate_id = args.candidate
        if candidate_id is None:
            usable = usable_candidates(document)
            if not usable:
                candidate_id = ""
            else:
                candidate_id = usable[0]["id"]
        embedder = fixture_embedder(fixture) if fixture.get("samples") else None
        report = build_report(
            candidate_id=candidate_id,
            fixture=fixture,
            embedder=embedder,
            embedder_kind="mock",
            metric=args.metric,
            candidates_path=Path(args.candidates),
        )
    except (IdentityCandidatesError, IdentityBenchError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"candidate: {report['candidate']}  device: {report['device']}")
        print(f"status: {report['verification_status']}  "
              f"accuracy: {report['accuracy']}  threshold: {report['threshold']}")
        print(f"notes: {report['notes']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via CLI only
    raise SystemExit(main())