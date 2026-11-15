"""Audio analysis contract tests: deterministic voice/volume/speech checks.

These pin the measurements the reviewer agent relies on to catch voice
mismatches, clipping, and silence problems before they reach the audience.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from shortdrama.audio_analysis import analyze, _f0_and_speech, _correlation


def _make_wav(path: Path, freq: float = 200.0, duration: float = 1.0,
              sr: int = 16000, amplitude: float = 8000.0) -> Path:
    """Generate a pure-tone WAV for deterministic F0 testing."""
    import numpy as np
    t = np.arange(int(sr * duration)) / sr
    x = (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    import wave
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    return path


def test_f0_estimation_female_range(tmp_path: Path):
    import numpy as np
    sr = 16000
    t = np.arange(sr) / sr
    x = (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    result = _f0_and_speech(x, sr)
    assert result["median_f0_hz"] is not None
    assert 200 < result["median_f0_hz"] < 240


def test_f0_estimation_male_range(tmp_path: Path):
    import numpy as np
    sr = 16000
    t = np.arange(sr) / sr
    x = (8000 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
    result = _f0_and_speech(x, sr)
    assert result["median_f0_hz"] is not None
    assert 90 < result["median_f0_hz"] < 130


def test_silence_detection(tmp_path: Path):
    import numpy as np
    sr = 16000
    x = np.zeros(sr * 2, dtype=np.float32)
    result = _f0_and_speech(x, sr)
    assert result["median_f0_hz"] is None
    assert result["silence_ratio"] > 0.95
    assert result["speech_start_s"] is None


def test_correlation_identical():
    import numpy as np
    x = np.random.default_rng(42).normal(0, 1000, 16000).astype(np.float32)
    assert _correlation(x, x) == pytest.approx(1.0, abs=0.01)


def test_correlation_different():
    import numpy as np
    rng = np.random.default_rng(42)
    a = rng.normal(0, 1000, 16000).astype(np.float32)
    b = rng.normal(0, 1000, 16000).astype(np.float32)
    c = _correlation(a, b)
    assert c is not None and abs(c) < 0.1


def test_analyze_missing_file(tmp_path: Path):
    result = analyze(tmp_path / "nope.mp4")
    assert "error" in result


def test_analyze_wav(tmp_path: Path):
    wav = _make_wav(tmp_path / "tone.wav", freq=220, duration=1.0)
    result = analyze(wav)
    assert result["has_audio"] is True
    assert result["voice"]["median_f0_hz"] is not None
    assert 200 < result["voice"]["median_f0_hz"] < 240
    assert result["volume"]["mean_db"] is not None
