"""Deterministic audio analysis for the short-drama QA gate.

Called by the reviewer agent via Bash; outputs JSON to stdout. No network,
no paid API. Uses ffmpeg/ffprobe (must be on PATH) and numpy.

Usage:
    python -m shortdrama.audio_analysis <media_path> [--ref <reference_audio>]

Checks:
- codec / sample rate / channels / duration
- mean & peak volume (clipping detection)
- speech span (first/last voiced frame)
- silence ratio
- median F0 (fundamental frequency → voice gender/age proxy)
- if --ref given: waveform correlation with the reference track
"""
from __future__ import annotations

# risk:untested reason:"audio_analysis 由 reviewer agent 在真实项目上实测验证；纯本地 ffmpeg+numpy 计算，无网络调用" owner:@kevinwanghd reviewed:2026-11-15

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional


def _run(cmd: list[str], timeout: int = 60) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout


def _ffprobe(path: Path) -> dict[str, Any]:
    out = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,sample_rate,channels,width,height"
        ":format=duration",
        "-of", "json", str(path),
    ])
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return {}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = data.get("format") or {}
    return {
        "has_audio": bool(audio),
        "audio_codec": audio.get("codec_name"),
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
        "has_video": bool(video),
        "width": video.get("width"),
        "height": video.get("height"),
        "duration_s": float(fmt.get("duration") or 0),
    }


def _decode_pcm(path: Path, sr: int = 16000) -> Optional[Any]:
    import numpy as np
    tmp = Path(tempfile.mktemp(suffix=".raw"))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-ac", "1", "-ar", str(sr),
             "-f", "s16le", str(tmp)],
            capture_output=True, timeout=60,
        )
        if not tmp.is_file() or tmp.stat().st_size == 0:
            return None
        return np.frombuffer(tmp.read_bytes(), dtype="<i2").astype(np.float32)
    finally:
        tmp.unlink(missing_ok=True)


def _volume(path: Path) -> dict[str, float]:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    ).stderr
    mean = max_ = None
    for line in out.splitlines():
        if "mean_volume:" in line:
            mean = float(line.split("mean_volume:")[1].strip().split()[0])
        if "max_volume:" in line:
            max_ = float(line.split("max_volume:")[1].strip().split()[0])
    return {"mean_db": mean, "max_db": max_}


def _f0_and_speech(x: Any, sr: int = 16000) -> dict[str, Any]:
    import numpy as np
    frame = 1024
    hop = frame // 2
    f0s: list[float] = []
    voiced: list[int] = []
    total_frames = 0
    for i in range(0, len(x) - frame, hop):
        f = x[i:i + frame]
        total_frames += 1
        rms = float(np.sqrt(np.mean(f * f)))
        if rms < 300:
            continue
        voiced.append(i)
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[frame - 1:]
        ac /= (ac[0] + 1e-9)
        lo_idx, hi_idx = int(sr / 350), int(sr / 70)
        k = int(np.argmax(ac[lo_idx:hi_idx])) + lo_idx
        if ac[k] > 0.3:
            f0s.append(sr / k)
    speech_start = voiced[0] / sr if voiced else None
    speech_end = (voiced[-1] + frame) / sr if voiced else None
    silence_ratio = 1.0 - len(voiced) / max(total_frames, 1)
    return {
        "median_f0_hz": round(float(np.median(f0s)), 1) if f0s else None,
        "f0_samples": len(f0s),
        "speech_start_s": round(speech_start, 3) if speech_start is not None else None,
        "speech_end_s": round(speech_end, 3) if speech_end is not None else None,
        "silence_ratio": round(silence_ratio, 3),
    }


def _correlation(a: Any, b: Any) -> Optional[float]:
    import numpy as np
    n = min(len(a), len(b))
    if n < 1600:
        return None
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    den = float(np.linalg.norm(a) * np.linalg.norm(b))
    return round(float(np.dot(a, b) / den), 4) if den else None


def analyze(path: Path, ref: Optional[Path] = None) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {"error": f"file not found: {path}"}
    result: dict[str, Any] = {"file": str(path), **_ffprobe(path)}
    if not result.get("has_audio"):
        result["audio_checks"] = "no audio stream"
        return result

    result["volume"] = _volume(path)
    x = _decode_pcm(path)
    if x is not None:
        result["voice"] = _f0_and_speech(x)
        if ref and Path(ref).is_file():
            rx = _decode_pcm(Path(ref))
            if rx is not None:
                result["ref_correlation"] = _correlation(x, rx)
                result["ref_voice"] = _f0_and_speech(rx)
    return result


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Deterministic audio analysis")
    parser.add_argument("media", type=Path)
    parser.add_argument("--ref", type=Path, default=None,
                        help="reference audio for correlation / F0 comparison")
    args = parser.parse_args()
    print(json.dumps(analyze(args.media, args.ref), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()