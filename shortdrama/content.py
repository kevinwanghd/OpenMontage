"""Content chain: canonical artifacts -> provider-neutral work items (M1).

The driver never invents shot content. It reads the artifacts the agent
produced through the stage director skills and turns them into per-item tool
inputs:

- ``character_cast`` -> one ``image_character_key`` item per character card
- ``keyframe``       -> one ``image_reference`` item per shot in ``scene_plan.shots``
- ``shot_gen``       -> one ``video_primary`` item per shot that has an approved
                        keyframe in ``asset_manifest``
- ``assembly``       -> one ``tts_voice`` item per script section

Slots whose content contract is not wired yet (music/subtitle/compose/upscale)
fall back to a single generic item so the binding still runs; their payload
lands with the assembly slice.

Every item is deterministic: the same artifacts produce the same item ids and
inputs, which is what lets the submission ledger's input hash identify a work
unit (ORC-03). Nothing here calls a provider or touches the network.
"""
from __future__ import annotations

# risk:untested reason:"content chain is covered by tests/contracts/test_shortdrama_content.py; thin static gate cannot link that test mapping" owner:@kevinwanghd reviewed:2026-11-12

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from lib.shot_prompt_builder import InvalidShotLanguageError, build_shot_prompt

#: Episode-scoped artifacts win over project-scoped ones so a series can keep
#: per-episode plans without rewriting the project-level artifact.
ARTIFACTS_DIRNAME = "artifacts"

#: Draft still resolution (9:16). Delivery upscales to 1080x1920 (DEL-01).
DRAFT_IMAGE_SIZE = "1152*2048"
DEFAULT_CANDIDATES = 2
MAX_CANDIDATES = 6


class ContentError(ValueError):
    """An artifact exists but cannot be turned into a work item."""


@dataclass(frozen=True)
class WorkItem:
    """One provider call's worth of content."""

    item_id: str
    inputs: dict[str, Any] = field(default_factory=dict)


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing; ordering never depends on dict insertion order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Artifact lookup
# ---------------------------------------------------------------------------


def episode_artifacts_dir(projects_dir: Path, project: str, episode: str) -> Path:
    return Path(projects_dir) / project / episode / ARTIFACTS_DIRNAME


def project_artifacts_dir(projects_dir: Path, project: str) -> Path:
    return Path(projects_dir) / project / ARTIFACTS_DIRNAME


def artifact_path(
    projects_dir: Path, project: str, episode: str, name: str
) -> Path:
    """Resolve an artifact, preferring the episode-scoped copy."""
    episode_scoped = episode_artifacts_dir(projects_dir, project, episode) / f"{name}.json"
    if episode_scoped.is_file():
        return episode_scoped
    return project_artifacts_dir(projects_dir, project) / f"{name}.json"


def load_artifact(
    projects_dir: Path, project: str, episode: str, name: str
) -> Optional[dict[str, Any]]:
    path = artifact_path(projects_dir, project, episode, name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"artifact {name!r} is unreadable at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContentError(f"artifact {name!r} must be a JSON object, got {type(data).__name__}")
    return data


def load_artifacts(
    projects_dir: Path, project: str, episode: str, names: Iterable[str]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        data = load_artifact(projects_dir, project, episode, name)
        if data is not None:
            out[name] = data
    return out


def missing_artifacts(
    projects_dir: Path, project: str, episode: str, names: Iterable[str]
) -> list[str]:
    return [
        name
        for name in names
        if not artifact_path(projects_dir, project, episode, name).is_file()
    ]


# ---------------------------------------------------------------------------
# Artifact readers
# ---------------------------------------------------------------------------


def _shots(scene_plan: dict[str, Any]) -> list[dict[str, Any]]:
    shots = scene_plan.get("shots")
    if not isinstance(shots, list):
        raise ContentError(
            "scene_plan has no shots[] layer; the short-drama pipeline requires "
            "the drama shot layer (VIS-01)"
        )
    return [shot for shot in shots if isinstance(shot, dict)]


def _shot_id(shot: dict[str, Any]) -> str:
    shot_id = shot.get("shot_id")
    if not isinstance(shot_id, str) or not shot_id:
        raise ContentError(f"scene_plan shot is missing shot_id: {shot!r}")
    return shot_id


def _character_cards(
    projects_dir: Path, project: str, episode: str, artifacts: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """character_id -> card. Accepts one card, a list, or artifacts/characters/*.json."""
    raw = artifacts.get("drama_character")
    cards: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        cards.extend(raw.get("characters") or [])
        if "character_id" in raw:
            cards.append(raw)
    # Project-scoped cards are the series roster; episode-scoped cards win when
    # a specific episode changes wardrobe or adds a character.
    characters_dirs = (
        project_artifacts_dir(projects_dir, project) / "characters",
        episode_artifacts_dir(projects_dir, project, episode) / "characters",
    )
    for characters_dir in characters_dirs:
        if not characters_dir.is_dir():
            continue
        for path in sorted(characters_dir.glob("*.json")):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ContentError(f"character card {path} is unreadable: {exc}") from exc
            if isinstance(card, dict):
                cards.append(card)
    out: dict[str, dict[str, Any]] = {}
    for card in cards:
        character_id = card.get("character_id")
        if isinstance(character_id, str) and character_id:
            out[character_id] = card
    return out


def _shot_prompt(
    shot: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    *,
    props: Optional[dict[str, dict[str, Any]]] = None,
    scenes: Optional[dict[str, dict[str, Any]]] = None,
    director_brief: Optional[dict[str, Any]] = None,
) -> str:
    parts: list[str] = []
    for key in ("single_action", "visual_evidence"):
        value = shot.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip().rstrip("."))
    for character_id in shot.get("reference_character_ids") or []:
        card = characters.get(str(character_id))
        if card:
            parts.append(
                f"{card.get('name') or character_id} "
                f"({card.get('canonical_description') or ''})".strip()
            )
    # Location and props are part of continuity: without them the video model
    # re-invents the room and swaps the bag between shots.
    scene = (scenes or {}).get(str(shot.get("scene_id") or ""))
    if scene and scene.get("description"):
        parts.append(f"Location: {str(scene['description']).strip().rstrip('.')}")
    prop_descriptions = [
        str((props or {}).get(str(prop_id), {}).get("description") or "").strip()
        for prop_id in shot.get("prop_ids") or []
    ]
    prop_descriptions = [d for d in prop_descriptions if d]
    if prop_descriptions:
        parts.append("On-screen props: " + "; ".join(prop_descriptions))
    # DIR-06: the director's visual tone and audience feeling travel with every
    # downstream prompt, so the shots stay one film instead of a stack of clips.
    tone = str((director_brief or {}).get("visual_tone") or "").strip().rstrip(".")
    feeling = str((director_brief or {}).get("audience_emotional_trigger") or "").strip().rstrip(".")
    if tone:
        parts.append(f"Director's tone: {tone}")
    if feeling:
        parts.append(f"Audience feeling: {feeling}")
    description = ". ".join(parts)
    scene = {
        "description": f"{description}." if description else "",
        "shot_language": shot.get("shot_language") or {},
    }
    try:
        return build_shot_prompt(scene)
    except InvalidShotLanguageError as exc:
        raise ContentError(
            f"shot {shot.get('shot_id')!r} has invalid shot_language: {exc}"
        ) from exc


def _prop_index(scene_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(prop.get("id")): prop
        for prop in scene_plan.get("props") or []
        if isinstance(prop, dict) and prop.get("id")
    }


def _scene_index(scene_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(scene.get("id")): scene
        for scene in scene_plan.get("scenes") or []
        if isinstance(scene, dict) and scene.get("id")
    }


def _approved_keyframes(asset_manifest: Optional[dict[str, Any]]) -> dict[str, str]:
    """shot_id -> keyframe path, last asset wins (manifest order is append-only)."""
    out: dict[str, str] = {}
    if not asset_manifest:
        return out
    for asset in asset_manifest.get("assets") or []:
        if not isinstance(asset, dict) or asset.get("type") != "image":
            continue
        scene_id = asset.get("scene_id")
        path = asset.get("path")
        if isinstance(scene_id, str) and isinstance(path, str) and path:
            out[scene_id] = path
    return out


# ---------------------------------------------------------------------------
# Work-item builders
# ---------------------------------------------------------------------------


def _character_items(
    projects_dir: Path,
    project: str,
    episode: str,
    artifacts: dict[str, Any],
) -> list[WorkItem]:
    items: list[WorkItem] = []
    for character_id, card in _character_cards(
        projects_dir, project, episode, artifacts
    ).items():
        wardrobe = [
            str(entry.get("description"))
            for entry in card.get("wardrobe") or []
            if isinstance(entry, dict) and entry.get("description")
        ]
        prompt = ", ".join(
            part
            for part in [
                str(card.get("canonical_description") or ""),
                " / ".join(wardrobe),
                "single character, full-length portrait, neutral background, "
                "consistent face, live-action short drama still",
            ]
            if part
        )
        items.append(
            WorkItem(character_id, {"prompt": prompt, "size": DRAFT_IMAGE_SIZE, "n": 1})
        )
    return items


def _keyframe_items(
    projects_dir: Path,
    project: str,
    episode: str,
    scene_plan: dict[str, Any],
    artifacts: dict[str, Any],
) -> list[WorkItem]:
    characters = _character_cards(projects_dir, project, episode, artifacts)
    prop_index, scene_index = _prop_index(scene_plan), _scene_index(scene_plan)
    items: list[WorkItem] = []
    for shot in _shots(scene_plan):
        shot_id = _shot_id(shot)
        candidates = int(shot.get("candidates") or DEFAULT_CANDIDATES)
        candidates = max(1, min(MAX_CANDIDATES, candidates))
        items.append(
            WorkItem(
                shot_id,
                {
                    "prompt": _shot_prompt(
                        shot, characters, props=prop_index, scenes=scene_index
                    ),
                    "size": DRAFT_IMAGE_SIZE,
                    "n": candidates,
                },
            )
        )
    return items


def _character_voice(card: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Provider voice fields declared by a character card.

    ``voice_id`` on the card is a logical id (``voice_margaret``), which a
    speech provider cannot use. The concrete provider voice lives under the
    ``tts`` object so casting stays in the artifact, not in code.
    """
    if not card:
        return {}
    tts = card.get("tts") if isinstance(card.get("tts"), dict) else {}
    voice_id = tts.get("voice_id") or card.get("tts_voice")
    resource_id = tts.get("resource_id") or card.get("tts_resource_id")
    out: dict[str, Any] = {}
    if isinstance(voice_id, str) and voice_id:
        out["voice_id"] = voice_id
    if isinstance(resource_id, str) and resource_id:
        out["resource_id"] = resource_id
    return out


def _speaker_card(
    speaker: Any, characters: dict[str, dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Resolve a character id or display name to its card."""
    if not speaker:
        return None
    key = str(speaker).strip()
    if key in characters:
        return characters[key]
    lowered = key.lower()
    for card in characters.values():
        if str(card.get("name") or "").strip().lower() == lowered:
            return card
    return None


def _shot_tts_items(
    scene_plan: dict[str, Any],
    characters: Optional[dict[str, dict[str, Any]]] = None,
) -> list[WorkItem]:
    """One dialogue TTS item per shot that speaks on screen (shot_gen stage).

    Producing the track here -- before video_primary in the same stage -- is
    what makes audio-driven lipsync possible; assembly no longer has to guess.
    """
    items: list[WorkItem] = []
    for shot in _shots(scene_plan):
        dialogue = shot.get("dialogue")
        if not isinstance(dialogue, dict):
            continue
        text = (dialogue.get("text") or "").strip()
        if not text:
            continue
        inputs: dict[str, Any] = {"text": text, "speaker": dialogue.get("speaker")}
        inputs.update(_character_voice(_speaker_card(dialogue.get("speaker"), characters or {})))
        items.append(WorkItem(_shot_id(shot), inputs))
    return items


def _shot_items(
    scene_plan: dict[str, Any],
    asset_manifest: Optional[dict[str, Any]],
    *,
    keyframes_required: bool = True,
    characters: Optional[dict[str, dict[str, Any]]] = None,
    audio_path_for=None,
    director_brief: Optional[dict[str, Any]] = None,
) -> list[WorkItem]:
    prop_index, scene_index = _prop_index(scene_plan), _scene_index(scene_plan)
    keyframes = _approved_keyframes(asset_manifest) if keyframes_required else {}
    items: list[WorkItem] = []
    missing: list[str] = []
    shot_ids = [_shot_id(shot) for shot in _shots(scene_plan)]
    for shot in _shots(scene_plan):
        shot_id = _shot_id(shot)
        keyframe = keyframes.get(shot_id)
        if not keyframes_required:
            duration = max(1.0, min(10.0, float(shot.get("duration_s") or 5.0)))
            items.append(
                WorkItem(
                    shot_id,
                    {
                        # Declared fallback: no approved keyframe (image slot
                        # blocked), so the shot is animated straight from text.
                        "prompt": _shot_prompt(
                            shot, characters or {},
                            props=prop_index, scenes=scene_index,
                            director_brief=director_brief,
                        ),
                        "operation": "text_to_video",
                        "duration_seconds": duration,
                        "ratio": "9:16",
                    },
                )
            )
            continue
        if not keyframe:
            missing.append(shot_id)
            continue
        duration = float(shot.get("duration_s") or 5.0)
        duration = max(1.0, min(10.0, duration))
        inputs: dict[str, Any] = {
            # wan3.0 with audio uses reference_image (first_frame and
            # reference_* are mutually exclusive), which is weaker than a true
            # first frame; the text must reinforce identity, location and props.
            "prompt": _shot_prompt(
                shot, characters or {}, props=prop_index, scenes=scene_index,
                director_brief=director_brief,
            ),
            "operation": "first_frame",
            "reference_image_path": keyframe,
            "duration_seconds": duration,
            # Short drama is always vertical; the provider needs an
            # explicit ratio (the deprecated size field is ignored).
            "ratio": "9:16",
        }
        if audio_path_for is not None:
            audio = audio_path_for(shot_id)
            if audio:
                # Dialogue track produced by the shot_gen tts_voice slot; the
                # video model drives mouth shapes from it. The line must be in
                # the prompt too: a reference-audio model otherwise invents
                # its own speech (wrong words and wrong voice).
                dialogue = shot.get("dialogue")
                if isinstance(dialogue, dict) and (dialogue.get("text") or "").strip():
                    line = str(dialogue["text"]).strip()
                    inputs["prompt"] = (
                        f'{inputs["prompt"]} The character speaks exactly: '
                        f'"{line}". Mouth shapes and timing follow the supplied '
                        "audio, and the voice keeps that audio's timbre."
                    )
                inputs["audio_path"] = audio
        items.append(WorkItem(shot_id, inputs))
    if missing:
        raise ContentError(
            "shot_gen has no approved keyframe for shot(s) "
            f"{', '.join(missing)}; review the keyframe stage first (VIS-04)"
        )
    if not items:
        raise ContentError(f"shot_gen produced no work items for {len(shot_ids)} shots")
    return items


def _section_speaker(section: dict[str, Any]) -> Optional[str]:
    """Structured speaker if present, else the name prefix of the direction."""
    speaker = section.get("speaker")
    if isinstance(speaker, str) and speaker.strip():
        return speaker.strip()
    directions = section.get("speaker_directions")
    if isinstance(directions, str) and directions.strip():
        head = directions.split("-", 1)[0].strip()
        if head and 0 < len(head) <= 40 and head.upper() != head:
            return head
        if head and head.upper() == head and head.isalpha():
            return head
    return None


def _tts_items(
    script: dict[str, Any],
    characters: Optional[dict[str, dict[str, Any]]] = None,
) -> list[WorkItem]:
    sections = script.get("sections")
    if not isinstance(sections, list):
        raise ContentError("script has no sections[]")
    items: list[WorkItem] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = section.get("id")
        text = section.get("text")
        if not isinstance(section_id, str) or not isinstance(text, str) or not text.strip():
            continue
        inputs: dict[str, Any] = {"text": text}
        if isinstance(section.get("speaker_directions"), str):
            inputs["speaker_directions"] = section["speaker_directions"]
        inputs.update(
            _character_voice(_speaker_card(_section_speaker(section), characters or {}))
        )
        items.append(WorkItem(section_id, inputs))
    return items


#: Slots whose per-item content contract is deliberate; everything else keeps
#: the legacy single-call behaviour until its slice lands.
_SINGLE_ITEM_SLOTS = frozenset({"music", "subtitle", "compose", "upscale"})


def build_work_items(
    stage: str,
    slot: str,
    artifacts: dict[str, Any],
    *,
    projects_dir: Optional[Path] = None,
    project: str = "",
    episode: str = "",
    keyframes_required: bool = True,
) -> list[WorkItem]:
    """Build the ordered work items for one stage/slot pair.

    Returns an empty list when the artifacts carry no items for this slot and
    the slot has no generic fallback (the caller decides whether that is a
    pause or a skip).
    """
    if slot == "image_character_key":
        return _character_items(projects_dir or Path("."), project, episode, artifacts)
    if slot == "image_reference":
        scene_plan = artifacts.get("scene_plan")
        if not scene_plan:
            raise ContentError("keyframe requires the scene_plan artifact")
        return _keyframe_items(
            projects_dir or Path("."), project, episode, scene_plan, artifacts
        )
    if slot == "video_primary":
        scene_plan = artifacts.get("scene_plan")
        if not scene_plan:
            raise ContentError("shot_gen requires the scene_plan artifact")
        assets = _assets_dir(projects_dir, project, episode)

        def audio_path_for(shot_id: str) -> Optional[str]:
            path = assets / f"shot_gen_tts_voice_{shot_id}.mp3"
            return str(path) if path.is_file() else None

        return _shot_items(
            scene_plan,
            artifacts.get("asset_manifest"),
            keyframes_required=keyframes_required,
            characters=_character_cards(projects_dir or Path("."), project, episode, artifacts),
            audio_path_for=audio_path_for,
            director_brief=artifacts.get("director_brief"),
        )
    if slot == "tts_voice":
        if stage == "shot_gen":
            scene_plan = artifacts.get("scene_plan")
            if not scene_plan:
                raise ContentError("shot_gen tts requires the scene_plan artifact")
            return _shot_tts_items(
            scene_plan,
            _character_cards(projects_dir or Path("."), project, episode, artifacts),
        )
        script = artifacts.get("script")
        if not script:
            raise ContentError("assembly requires the script artifact")
        return _tts_items(
            script,
            _character_cards(projects_dir or Path("."), project, episode, artifacts),
        )
    if slot == "subtitle":
        return _subtitle_items(artifacts, projects_dir, project, episode)
    if slot == "compose":
        return _compose_items(artifacts, projects_dir, project, episode)
    if slot in _SINGLE_ITEM_SLOTS:
        return [WorkItem(f"{slot}_default", {})]
    return []


def _assets_dir(
    projects_dir: Optional[Path], project: str, episode: str
) -> Path:
    return (projects_dir or Path(".")) / project / episode / "assets"


def _audio_duration(path: Path) -> Optional[float]:
    """Measured seconds of a local audio track, or None when absent."""
    if not path.is_file():
        return None
    import subprocess

    # risk:swallowed-exception reason:"ffprobe 不可用时返回 None，字幕按未知时长降级，不阻断生成" owner:@kevinwanghd reviewed:2026-11-14
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _subtitle_items(
    artifacts: dict[str, Any],
    projects_dir: Optional[Path],
    project: str,
    episode: str,
) -> list[WorkItem]:
    """Subtitle cues timed to real TTS audio, not to script page timings."""
    from shortdrama import continuity

    script = artifacts.get("script")
    if not script:
        raise ContentError("subtitle requires the script artifact")
    assets = _assets_dir(projects_dir, project, episode)

    def duration(section_id) -> Optional[float]:
        return _audio_duration(assets / f"assembly_tts_voice_{section_id}.mp3")

    segments = continuity.subtitle_segments(script, duration)
    return [WorkItem("subtitle_default", {"segments": segments, "format": "srt"})]


def _compose_items(
    artifacts: dict[str, Any],
    projects_dir: Optional[Path],
    project: str,
    episode: str,
) -> list[WorkItem]:
    """Compose the approved clips into one vertical cut.

    ``video_compose`` speaks ``edit_decisions`` (cuts with source/in/out), not
    a bare clip list. When the assembly director already produced that
    artifact it wins; otherwise the cuts are derived deterministically from
    scene_plan order and the approved clips in asset_manifest.
    """
    manifest = artifacts.get("asset_manifest")
    if not manifest:
        raise ContentError("compose requires the asset_manifest artifact")
    scene_plan = artifacts.get("scene_plan") or {}
    assets = _assets_dir(projects_dir, project, episode)

    edit_decisions = artifacts.get("edit_decisions")
    if not isinstance(edit_decisions, dict):
        approved = {
            a["scene_id"]
            for a in manifest.get("assets") or []
            if isinstance(a, dict) and a.get("type") == "video"
        }
        cuts: list[dict[str, Any]] = []
        missing: list[str] = []
        for shot in _shots(scene_plan):
            shot_id = _shot_id(shot)
            if shot_id not in approved:
                missing.append(shot_id)
                continue
            clip = assets / f"shot_gen_video_primary_{shot_id}.mp4"
            if not clip.is_file():
                missing.append(shot_id)
                continue
            cuts.append(
                {
                    "source": str(clip),
                    "in_seconds": 0.0,
                    "out_seconds": float(shot.get("duration_s") or 5.0),
                    "shot_id": shot_id,
                }
            )
        if missing:
            raise ContentError(
                "compose has no approved clip for shot(s) "
                f"{', '.join(missing)}; generate shot_gen first"
            )
        if not cuts:
            raise ContentError("compose produced no cuts from the asset_manifest")
        edit_decisions = {
            "cuts": cuts,
            # 9:16 draft canvas; cover crops rather than letterboxing a
            # vertical source into a landscape default.
            "metadata": {
                "compose_target": {"width": 720, "height": 1280, "fit": "cover"}
            },
        }

    subtitle = assets / "assembly_subtitle_subtitle_default.srt"
    edit_decisions = dict(edit_decisions)
    if subtitle.is_file() and not edit_decisions.get("subtitles"):
        edit_decisions["subtitles"] = {"source": str(subtitle)}

    inputs: dict[str, Any] = {
        "operation": "compose",
        "edit_decisions": edit_decisions,
    }
    if subtitle.is_file():
        inputs["subtitle_path"] = str(subtitle)
    return [WorkItem("compose_default", inputs)]