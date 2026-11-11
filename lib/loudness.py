"""Deterministic loudness and mix validation for the short-drama pipeline.

Implements the machine-checkable half of two requirements:

- **AUD-07** — final-assembly loudness gate: integrated loudness −14 LUFS
  ±1 LU and true peak ≤ −1 dBTP. The caller supplies measured values (from
  any measurement pass); this module never invokes ffmpeg or a provider, so
  loudness QA is independent of the TTS provider.
- **AUD-08** — mix spec: dialogue must stay clear of BGM (minimum separation),
  BGM must duck under dialogue by at least the declared depth, and dialogue
  must not be buried below the minimum level. When there is no BGM the check
  reports ``N/A`` rather than a silent pass.

Both validators accept pre-measured data and return structured reports; a
malformed measurement is reported as a failure, never treated as passing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# --- AUD-07 thresholds ------------------------------------------------------
TARGET_LUFS = -14.0
LUFS_TOLERANCE_LU = 1.0
MIN_LUFS = TARGET_LUFS - LUFS_TOLERANCE_LU  # -15.0
MAX_LUFS = TARGET_LUFS + LUFS_TOLERANCE_LU  # -13.0
MAX_TRUE_PEAK_DBTP = -1.0
LOUDNESS_SPEC_VERSION = "AUD-07.v1"

# --- AUD-08 mix spec (declaration + thresholds) ----------------------------
MIX_SPEC_VERSION = "AUD-08.v1"
MIN_DIALOGUE_BGM_SEPARATION_DB = 6.0
MIN_DUCKING_DEPTH_DB = 3.0
MIN_DIALOGUE_RMS_DBFS = -30.0

INVALID_MEASUREMENT = "INVALID_MEASUREMENT"
LOUDNESS_OUT_OF_RANGE = "LOUDNESS_OUT_OF_RANGE"
TRUE_PEAK_TOO_HIGH = "TRUE_PEAK_TOO_HIGH"

BGM_MASKS_DIALOGUE = "BGM_MASKS_DIALOGUE"
INSUFFICIENT_DUCKING = "INSUFFICIENT_DUCKING"
DIALOGUE_TOO_QUIET = "DIALOGUE_TOO_QUIET"


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


@dataclass(frozen=True)
class LoudnessMeasurement:
    """Measured integrated loudness and true peak of the assembled mix."""

    integrated_lufs: float
    true_peak_dbtp: float


@dataclass(frozen=True)
class LoudnessViolation:
    code: str
    detail: str
    measured: float
    limit: str


@dataclass(frozen=True)
class LoudnessReport:
    measurement: LoudnessMeasurement
    violations: tuple[LoudnessViolation, ...]
    spec_version: str = LOUDNESS_SPEC_VERSION

    @property
    def passed(self) -> bool:
        return not self.violations


def evaluate_loudness(measurement: LoudnessMeasurement) -> LoudnessReport:
    """Judge a measurement against −14 LUFS ±1 LU and true peak ≤ −1 dBTP."""
    violations: list[LoudnessViolation] = []
    lufs = measurement.integrated_lufs
    peak = measurement.true_peak_dbtp
    if not _finite(lufs) or not _finite(peak):
        violations.append(LoudnessViolation(
            INVALID_MEASUREMENT,
            f"integrated_lufs={lufs!r} true_peak_dbtp={peak!r} must be finite",
            lufs if _finite(lufs) else float("nan"),
            f"{MIN_LUFS}..{MAX_LUFS} LUFS, peak <= {MAX_TRUE_PEAK_DBTP} dBTP",
        ))
        return LoudnessReport(measurement, tuple(violations))

    if lufs < MIN_LUFS or lufs > MAX_LUFS:
        violations.append(LoudnessViolation(
            LOUDNESS_OUT_OF_RANGE,
            f"integrated loudness {lufs} LUFS outside {MIN_LUFS}..{MAX_LUFS} LUFS",
            float(lufs),
            f"{MIN_LUFS}..{MAX_LUFS}",
        ))
    if peak > MAX_TRUE_PEAK_DBTP:
        violations.append(LoudnessViolation(
            TRUE_PEAK_TOO_HIGH,
            f"true peak {peak} dBTP exceeds {MAX_TRUE_PEAK_DBTP} dBTP",
            float(peak),
            f"<= {MAX_TRUE_PEAK_DBTP}",
        ))
    return LoudnessReport(measurement, tuple(violations))


# ---------------------------------------------------------------------------
# AUD-08 — mix spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MixMeasurement:
    """Measured dialogue/BGM levels; ducking depth is optional.

    ``bgm_rms_dbfs=None`` means the scene has no BGM and the mix check is
    reported as not applicable (never a silent pass).
    """

    dialogue_rms_dbfs: float
    bgm_rms_dbfs: Optional[float] = None
    ducking_depth_db: Optional[float] = None


@dataclass(frozen=True)
class MixViolation:
    code: str
    detail: str
    measured: Optional[float]
    limit: str


@dataclass(frozen=True)
class MixReport:
    status: str  # PASS | FAIL | N/A
    violations: tuple[MixViolation, ...]
    separation_db: Optional[float]
    spec_version: str = MIX_SPEC_VERSION

    @property
    def passed(self) -> bool:
        return self.status != "FAIL"

    @property
    def not_applicable(self) -> bool:
        return self.status == "N/A"


def mix_spec() -> dict:
    """Declare the mix spec so gates can record the threshold version used."""
    return {
        "spec_version": MIX_SPEC_VERSION,
        "min_dialogue_bgm_separation_db": MIN_DIALOGUE_BGM_SEPARATION_DB,
        "min_ducking_depth_db": MIN_DUCKING_DEPTH_DB,
        "min_dialogue_rms_dbfs": MIN_DIALOGUE_RMS_DBFS,
        "bgm_absent_status": "N/A",
    }


def evaluate_mix(measurement: MixMeasurement) -> MixReport:
    """Check dialogue clarity and BGM behaviour against the declared spec."""
    dialogue = measurement.dialogue_rms_dbfs
    if not _finite(dialogue):
        violation = MixViolation(
            INVALID_MEASUREMENT,
            f"dialogue_rms_dbfs={dialogue!r} must be finite",
            None,
            f">= {MIN_DIALOGUE_RMS_DBFS} dBFS",
        )
        return MixReport("FAIL", (violation,), None)

    bgm = measurement.bgm_rms_dbfs
    if bgm is None:
        return MixReport("N/A", (), None)

    if not _finite(bgm):
        violation = MixViolation(
            INVALID_MEASUREMENT,
            f"bgm_rms_dbfs={bgm!r} must be finite",
            None,
            "finite dBFS",
        )
        return MixReport("FAIL", (violation,), None)

    violations: list[MixViolation] = []
    separation = float(dialogue) - float(bgm)
    if separation < MIN_DIALOGUE_BGM_SEPARATION_DB:
        violations.append(MixViolation(
            BGM_MASKS_DIALOGUE,
            f"dialogue/BGM separation {separation:.1f} dB is below "
            f"{MIN_DIALOGUE_BGM_SEPARATION_DB:.1f} dB",
            separation,
            f">= {MIN_DIALOGUE_BGM_SEPARATION_DB}",
        ))
    if measurement.ducking_depth_db is not None:
        if not _finite(measurement.ducking_depth_db):
            violations.append(MixViolation(
                INVALID_MEASUREMENT,
                f"ducking_depth_db={measurement.ducking_depth_db!r} must be finite",
                None,
                f">= {MIN_DUCKING_DEPTH_DB}",
            ))
        elif measurement.ducking_depth_db < MIN_DUCKING_DEPTH_DB:
            violations.append(MixViolation(
                INSUFFICIENT_DUCKING,
                f"ducking depth {measurement.ducking_depth_db} dB is below "
                f"{MIN_DUCKING_DEPTH_DB} dB",
                float(measurement.ducking_depth_db),
                f">= {MIN_DUCKING_DEPTH_DB}",
            ))
    if dialogue < MIN_DIALOGUE_RMS_DBFS:
        violations.append(MixViolation(
            DIALOGUE_TOO_QUIET,
            f"dialogue level {dialogue} dBFS is below "
            f"{MIN_DIALOGUE_RMS_DBFS} dBFS",
            float(dialogue),
            f">= {MIN_DIALOGUE_RMS_DBFS}",
        ))
    return MixReport("FAIL" if violations else "PASS", tuple(violations), separation)