"""Shot prompt builder — structured five-layer shot language (VIS-07 / F14).

The builder is the bridge between a ``scene_plan`` scene and a provider
dialect. It normalises the scene's cinematography into the neutral five-layer
plan that every dialect renders from:

  Layer 1 (camera):    lens, depth of field
  Layer 2 (movement):  shot size, camera movement
  Layer 3 (subject):   scene description + texture keywords
  Layer 4 (lighting):  lighting key, colour temperature
  Layer 5 (style):     playbook-derived aesthetic / mood

Each layer is returned as an ordered list of canonical ``field=value`` tokens
(:func:`build_shot_prompt_layers`). Providers never receive this structure
directly: ``lib/prompt_dialects`` renders it into its own wording, so the same
shot keeps the same meaning across dialects (MOD-04). ``build_shot_prompt``
still returns one flat English sentence for callers that only need a string,
but it is now a rendering of the structured layers rather than the source of
truth.

Validation is delegated to :func:`lib.prompt_dialects.neutral.structured_layers`
so an out-of-schema value can never reach a paid provider.

M0: pure local transformation, zero external API calls.
"""

from __future__ import annotations

from typing import Any

from lib.prompt_dialects.errors import InvalidShotLanguageError
from lib.prompt_dialects.neutral import structured_layers

#: VIS-07 layer order. The returned mapping always has exactly these keys.
LAYER_NAMES = ("camera", "movement", "subject", "lighting", "style")

# --- neutral token -> flat English phrase ----------------------------------

_SHOT_SIZE_PHRASES = {
    "extreme_wide": "extreme wide shot showing vast environment",
    "wide": "wide shot capturing full scene",
    "medium_wide": "medium-wide shot framing subject with surroundings",
    "medium": "medium shot from waist up",
    "medium_close": "medium close-up from chest up",
    "close_up": "close-up focusing on face or detail",
    "extreme_close_up": "extreme close-up on fine detail",
    "over_shoulder": "over-the-shoulder perspective",
    "insert": "insert shot of specific detail",
    "establishing": "establishing shot setting the location",
}

_MOVEMENT_PHRASES = {
    "static": "locked-off static camera",
    "pan_left": "smooth pan to the left",
    "pan_right": "smooth pan to the right",
    "tilt_up": "gentle tilt upward",
    "tilt_down": "gentle tilt downward",
    "dolly_in": "slow dolly in toward subject",
    "dolly_out": "slow dolly out from subject",
    "tracking_left": "tracking shot moving left alongside subject",
    "tracking_right": "tracking shot moving right alongside subject",
    "crane_up": "crane shot rising upward",
    "crane_down": "crane shot descending",
    "handheld": "handheld camera with natural movement",
    "steadicam": "smooth steadicam following movement",
    "whip_pan": "fast whip pan",
    "orbital": "orbital camera circling subject",
    "zoom_in": "slow zoom in",
    "zoom_out": "slow zoom out",
    "rack_focus": "rack focus shift between foreground and background",
}

_LIGHTING_PHRASES = {
    "high_key": "bright high-key lighting, minimal shadows",
    "low_key": "dramatic low-key lighting with deep shadows",
    "natural": "natural ambient lighting",
    "golden_hour": "warm golden hour sunlight",
    "blue_hour": "cool blue hour twilight",
    "tungsten_warm": "warm tungsten interior lighting",
    "neon": "neon-lit with vibrant color spill",
    "silhouette": "backlit silhouette",
    "rim_lit": "rim lighting highlighting edges",
    "volumetric": "volumetric light with visible rays",
    "overcast_soft": "soft overcast diffused light",
}

_DOF_PHRASES = {
    "shallow": "shallow depth of field with bokeh",
    "medium": "medium depth of field",
    "deep": "deep focus with everything sharp",
}

_COLOR_TEMP_PHRASES = {
    "cool": "cool blue-toned color palette",
    "neutral": "neutral balanced colors",
    "warm": "warm amber-toned color palette",
    "mixed": "mixed color temperatures for contrast",
}


# --- structured builder ----------------------------------------------------


def _style_tokens(style_context: dict[str, Any] | None) -> list[str]:
    """Return playbook-derived style tokens for the fifth layer.

    The playbook is advisory context, not a shot-language enum: only the
    aesthetic and mood fields are consumed, and absent values add no token.
    """
    if not style_context:
        return []
    if not isinstance(style_context, dict):
        raise TypeError(
            f"style_context must be a dict, got {type(style_context).__name__}"
        )

    tokens: list[str] = []
    visual_language = style_context.get("visual_language")
    aesthetic = ""
    if isinstance(visual_language, dict):
        raw = visual_language.get("aesthetic")
        if isinstance(raw, str):
            aesthetic = raw.strip()
    if aesthetic:
        tokens.append(f"style={aesthetic}")

    mood = style_context.get("mood")
    if isinstance(mood, str) and mood.strip():
        tokens.append(f"mood={mood.strip()}")
    return tokens


def build_shot_prompt_layers(
    scene: dict[str, Any],
    style_context: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Return the five neutral layers for ``scene`` as structured tokens.

    Args:
        scene: Scene or shot dict from ``scene_plan``. ``shot_language`` is
            validated against the schema vocabulary; ``description`` becomes
            the subject layer and ``texture_keywords`` become texture tokens.
        style_context: Optional playbook-derived context with
            ``visual_language.aesthetic`` and/or ``mood``.

    Returns:
        ``{"camera": [...], "movement": [...], "subject": [...],
        "lighting": [...], "style": [...]}`` — ordered canonical tokens, ready
        for a dialect renderer. Constraints are deliberately not included;
        they belong to the binding, not to a single shot.

    Raises:
        InvalidShotLanguageError: a shot_language value is outside the schema.
        TypeError: ``scene`` or ``style_context`` has the wrong type.
    """
    if not isinstance(scene, dict):
        raise TypeError(f"scene must be a dict, got {type(scene).__name__}")

    shot_language = scene.get("shot_language", {})
    if shot_language is None:
        shot_language = {}
    if not isinstance(shot_language, dict):
        raise InvalidShotLanguageError(
            f"shot_language must be a dict, got {type(shot_language).__name__}"
        )

    # Fold scene-level subject context into the shot language so the neutral
    # validator owns both the vocabulary and the canonical token formatting.
    augmented = dict(shot_language)
    description = scene.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise TypeError(
                f"scene description must be a string, got {type(description).__name__}"
            )
        if description.strip():
            augmented["subject"] = description
    texture = scene.get("texture_keywords")
    if texture:
        augmented["texture_keywords"] = texture

    neutral = structured_layers(augmented)
    layers = {name: list(neutral[name]) for name in LAYER_NAMES}
    layers["style"] = _style_tokens(style_context)
    return layers


# --- flat rendering (backwards-compatible string prompt) -------------------


def _split_token(token: str) -> tuple[str, str]:
    """Split a canonical ``field=value`` token, tolerating ``=`` in values."""
    return token.partition("=")[::2]


def _flat_layer(name: str, tokens: list[str]) -> str:
    parts: list[str] = []
    if name == "camera":
        for token in tokens:
            field, value = _split_token(token)
            if field == "lens_mm":
                parts.append(f"{value}mm lens")
            elif field == "depth_of_field":
                parts.append(_DOF_PHRASES.get(value, value))
    elif name == "movement":
        for token in tokens:
            field, value = _split_token(token)
            if field == "shot_size":
                parts.append(_SHOT_SIZE_PHRASES.get(value, value))
            elif field == "camera_movement" and value != "static":
                # Static is the effective default; the neutral layer keeps it
                # explicit but the flat prompt does not need to repeat it.
                parts.append(_MOVEMENT_PHRASES.get(value, value))
    elif name == "subject":
        for token in tokens:
            _field, value = _split_token(token)
            parts.append(value)
    elif name == "lighting":
        for token in tokens:
            field, value = _split_token(token)
            if field == "lighting_key":
                parts.append(_LIGHTING_PHRASES.get(value, value))
            elif field == "color_temperature":
                parts.append(_COLOR_TEMP_PHRASES.get(value, value))
    elif name == "style":
        values = [_split_token(token)[1] for token in tokens]
        if values:
            return "Style: " + ", ".join(values)
    return ", ".join(part for part in parts if part)


def render_shot_prompt_layers(layers: dict[str, list[str]]) -> str:
    """Render the structured layers into the legacy flat English prompt.

    The layers stay the source of truth; this is only a convenience for
    callers that do not route through a provider dialect.
    """
    missing = [name for name in LAYER_NAMES if name not in layers]
    if missing:
        raise ValueError(f"layers missing required entries: {', '.join(missing)}")
    return ". ".join(
        text for text in (_flat_layer(name, layers[name]) for name in LAYER_NAMES) if text
    )


def build_shot_prompt(
    scene: dict[str, Any],
    style_context: dict[str, Any] | None = None,
) -> str:
    """Convert a scene into one flat natural-language generation prompt.

    Kept for callers that only need a string. Provider dialects should consume
    :func:`build_shot_prompt_layers` instead so wording stays dialect-owned
    (VIS-07 / MOD-04).
    """
    return render_shot_prompt_layers(build_shot_prompt_layers(scene, style_context))


def build_batch_prompts(
    scenes: list[dict[str, Any]],
    style_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build flat prompts for all visual scenes in a scene plan.

    Returns list of ``{scene_id, prompt, hero_moment}`` dicts.
    """
    results = []
    for scene in scenes:
        scene_type = scene.get("type", "")
        if scene_type == "transition":
            continue
        results.append({
            "scene_id": scene.get("id", "unknown"),
            "prompt": build_shot_prompt(scene, style_context),
            "hero_moment": scene.get("hero_moment", False),
        })
    return results


def build_batch_prompt_layers(
    scenes: list[dict[str, Any]],
    style_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build structured layer sets for all visual scenes in a scene plan.

    The structured counterpart of :func:`build_batch_prompts` for dialect
    rendering (VIS-07).
    """
    results = []
    for scene in scenes:
        if scene.get("type", "") == "transition":
            continue
        results.append({
            "scene_id": scene.get("id", "unknown"),
            "layers": build_shot_prompt_layers(scene, style_context),
            "hero_moment": scene.get("hero_moment", False),
        })
    return results