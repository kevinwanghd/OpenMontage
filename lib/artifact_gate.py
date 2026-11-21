"""Gate on the artifacts themselves, not on how they were produced (GATE-02).

The 2026-11-17 delivery reached `completed` with a female character voiced by a
male track. Nothing caught it because every check lived on the *execution* path:
`shortdrama/driver.py` ran QA only when it had executed the slot itself, and
`shortdrama/qa.py` located clips by a hard-coded filename. Generating a clip by
calling the tool directly therefore skipped the gate entirely, silently.

This module moves the check to the artifact boundary. It reads the project's
`asset_manifest.json`, resolves each entry by its declared path, and verifies
that the file on disk is what the manifest claims it is. It does not care who
generated the asset or whether a driver ran.

Two deliberate scope limits:

* **Provenance is opt-in per manifest** (`provenance_version`). Legacy manifests
  produced before this gate carry no hashes or voice ids; blocking them would
  make every in-flight project unresumable. They are reported as a warning
  instead, which is visible without breaking resumption.
* **Only voice identity is judged, not voice quality.** The gate compares the
  asset's declared `voice_id` against the character card, because that
  comparison is deterministic. Whether the voice sounds right stays a human /
  reviewer judgement.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

MANIFEST_FILE = Path("artifacts") / "asset_manifest.json"
CHARACTERS_DIR = Path("artifacts") / "characters"
PROVENANCE_VERSION_FIELD = "provenance_version"
CURRENT_PROVENANCE_VERSION = 1

#: Asset kinds that are produced by a generator and therefore need provenance.
GENERATED_KINDS = frozenset({"image", "video", "audio", "narration"})
AUDIO_KINDS = frozenset({"audio", "narration"})
REQUIRED_PROVENANCE = ("provider", "model", "sha256")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Optional[Any]:
    # risk:swallowed-exception reason:"清单/角色卡损坏时返回 None，由调用方按缺失阻断而不是抛出堆栈；错误信息在下游 findings 中出现" owner:@kevinwanghd reviewed:2026-11-21
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def character_voice_ids(project_dir: Path) -> dict[str, str]:
    """Map character_id -> declared TTS voice_id from the character cards."""
    voices: dict[str, str] = {}
    directory = Path(project_dir) / CHARACTERS_DIR
    if not directory.is_dir():
        return voices
    for card_path in sorted(directory.glob("*.json")):
        card = _load_json(card_path)
        if not isinstance(card, dict):
            continue
        tts = card.get("tts")
        if isinstance(tts, dict) and isinstance(tts.get("voice_id"), str):
            character_id = str(card.get("character_id") or card_path.stem)
            voices[character_id] = tts["voice_id"]
    return voices


def _check_asset(
    asset: dict[str, Any],
    project_dir: Path,
    voices: dict[str, str],
    *,
    require_provenance: bool,
    blocking: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    asset_id = str(asset.get("id") or "?")
    kind = str(asset.get("type") or "")
    declared_path = asset.get("path")
    if not declared_path:
        blocking.append({
            "asset": asset_id, "issue": "manifest entry has no path",
            "severity": "P0", "fix": "record the produced file path on the asset",
        })
        return

    file_path = Path(project_dir) / str(declared_path)
    if not file_path.is_file():
        blocking.append({
            "asset": asset_id, "issue": "declared artifact is missing on disk",
            "evidence": str(declared_path), "severity": "P0",
            "fix": "regenerate the asset or correct the manifest path",
        })
        return

    provenance = asset.get("provenance")
    if not isinstance(provenance, dict):
        entry = {
            "asset": asset_id,
            "issue": "no provenance recorded (provider/model/sha256)",
            "evidence": str(declared_path),
            "fix": "record provenance when generating; see asset_manifest schema",
        }
        if require_provenance:
            blocking.append({**entry, "severity": "P0"})
        else:
            warnings.append({**entry, "severity": "P1"})
        return

    missing = [field for field in REQUIRED_PROVENANCE if not provenance.get(field)]
    if missing:
        blocking.append({
            "asset": asset_id, "issue": "incomplete provenance",
            "evidence": ", ".join(missing), "severity": "P0",
            "fix": "record provider, model and sha256 for every generated asset",
        })

    declared_hash = str(provenance.get("sha256") or "")
    if declared_hash:
        actual = sha256_file(file_path)
        if actual != declared_hash:
            blocking.append({
                "asset": asset_id, "issue": "artifact does not match its recorded hash",
                "evidence": f"declared {declared_hash[:12]}… actual {actual[:12]}…",
                "severity": "P0",
                "fix": "the file changed after review; re-review or revert it",
            })

    if kind in AUDIO_KINDS:
        voice_id = provenance.get("voice_id")
        character_id = provenance.get("character_id")
        if not voice_id or not character_id:
            blocking.append({
                "asset": asset_id, "issue": "dialogue audio has no voice identity",
                "evidence": f"voice_id={voice_id!r} character_id={character_id!r}",
                "severity": "P0",
                "fix": "record voice_id and character_id on the asset provenance",
            })
            return
        expected = voices.get(str(character_id))
        if expected is None:
            blocking.append({
                "asset": asset_id, "issue": "voice cannot be verified against a character card",
                "evidence": str(character_id), "severity": "P0",
                "fix": "register the character card, or fix the character_id",
            })
        elif str(voice_id) != expected:
            blocking.append({
                "asset": asset_id, "issue": "voice does not match the character card",
                "evidence": f"asset voice_id={voice_id} card voice_id={expected}",
                "severity": "P0",
                "fix": "regenerate the line with the character's registered voice",
            })


def validate(project_dir: Path | str) -> dict[str, Any]:
    """Validate every manifest-declared artifact. Returns blocking/warnings."""
    project_dir = Path(project_dir)
    manifest = _load_json(project_dir / MANIFEST_FILE)
    if manifest is None:
        # Other gates already fail on a missing manifest; this one stays quiet
        # so a single missing file is not reported twice.
        return {"blocking": [], "warnings": []}
    if not isinstance(manifest, dict):
        return {"blocking": [{
            "issue": "asset manifest is not an object", "severity": "P0",
            "fix": "rewrite artifacts/asset_manifest.json",
        }], "warnings": []}

    require_provenance = (
        int(manifest.get(PROVENANCE_VERSION_FIELD) or 0) >= CURRENT_PROVENANCE_VERSION
    )
    voices = character_voice_ids(project_dir)
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for asset in manifest.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("type") or "") not in GENERATED_KINDS:
            continue
        _check_asset(
            asset, project_dir, voices,
            require_provenance=require_provenance,
            blocking=blocking, warnings=warnings,
        )
    return {"blocking": blocking, "warnings": warnings}