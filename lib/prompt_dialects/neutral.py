"""Neutral shot-language layers shared by every prompt dialect (VIS-07).

The pipeline speaks one language -- the structured ``shot_language`` of
``scene_plan`` -- and a dialect turns it into one provider's prompt. To keep
dialects semantically aligned they all start from :func:`structured_layers`:
the same five layers (camera / movement / subject / lighting / style) expressed
as canonical ``field=value`` tokens, plus a ``constraints`` list derived from
the binding's capability facts.

Canonical tokens are the neutral vocabulary; a dialect maps each token to its
own phrasing. That is what makes "same shot, different dialect" machine
checkable: the tokens are identical, only the rendering differs.

Unknown is never support (MOD-02): capability values the binding has not
verified (``"unknown"``/absent) produce no constraint rather than a default.
M0: local validation only, zero external API calls.
"""

# risk:untested reason:"neutral layer plan is covered by 38 contract tests in tests/contracts/test_shortdrama_dialects.py; thin static gate cannot link that mapping" owner:@kevin reviewed:2026-11-11

from __future__ import annotations

from typing import Any

from .errors import InvalidShotLanguageError

SHOT_SIZES = frozenset(
    {
        "extreme_wide",
        "wide",
        "medium_wide",
        "medium",
        "medium_close",
        "close_up",
        "extreme_close_up",
        "over_shoulder",
        "insert",
        "establishing",
    }
)

CAMERA_MOVEMENTS = frozenset(
    {
        "static",
        "pan_left",
        "pan_right",
        "tilt_up",
        "tilt_down",
        "dolly_in",
        "dolly_out",
        "tracking_left",
        "tracking_right",
        "crane_up",
        "crane_down",
        "handheld",
        "steadicam",
        "whip_pan",
        "orbital",
        "zoom_in",
        "zoom_out",
        "rack_focus",
    }
)

LENS_MM = (14, 24, 35, 50, 85, 135, 200)

LIGHTING_KEYS = frozenset(
    {
        "high_key",
        "low_key",
        "natural",
        "golden_hour",
        "blue_hour",
        "tungsten_warm",
        "neon",
        "silhouette",
        "rim_lit",
        "volumetric",
        "overcast_soft",
    }
)

DEPTHS_OF_FIELD = frozenset({"shallow", "medium", "deep"})

COLOR_TEMPERATURES = frozenset({"cool", "neutral", "warm", "mixed"})

#: Five-layer order (VIS-07). ``style`` stays empty in M0: the frozen
#: signature carries no style context, and inventing one would let a dialect
#: override the playbook.
LAYER_NAMES = ("camera", "movement", "subject", "lighting", "style")

#: Schema fields plus optional caller context. ``subject`` and
#: ``texture_keywords`` live at scene level in scene_plan; the stage may pass
#: them through so the subject layer is renderable without changing the
#: dialect signature.
_ALLOWED_KEYS = frozenset(
    {
        "shot_size",
        "camera_movement",
        "lens_mm",
        "lighting_key",
        "depth_of_field",
        "color_temperature",
        "subject",
        "texture_keywords",
    }
)

#: Capability facts that belong in a per-shot prompt. Anything else the
#: binding reports is out of scope for a single shot.
_CAPABILITY_BOOLEANS = ("native_audio", "lipsync_from_dialogue", "first_frame_conditioning")
_CAPABILITY_LISTS = ("aspect_ratios", "output_resolutions")


def _enum(
    shot_language: dict[str, Any], field: str, allowed: frozenset[str]
) -> str | None:
    value = shot_language.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise InvalidShotLanguageError(
            f"unknown {field} {value!r}; expected one of: {', '.join(sorted(allowed))}"
        )
    return value


def _lens(shot_language: dict[str, Any]) -> int | None:
    value = shot_language.get("lens_mm")
    if value is None:
        return None
    if isinstance(value, bool) or value not in LENS_MM:
        expected = ", ".join(str(mm) for mm in LENS_MM)
        raise InvalidShotLanguageError(
            f"unknown lens_mm {value!r}; expected one of: {expected}"
        )
    return int(value)


def _subject(shot_language: dict[str, Any]) -> str | None:
    value = shot_language.get("subject")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidShotLanguageError(
            f"subject must be a string, got {type(value).__name__}"
        )
    return value.strip() or None


def _texture_keywords(shot_language: dict[str, Any]) -> list[str]:
    value = shot_language.get("texture_keywords")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidShotLanguageError("texture_keywords must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _capability_facts(binding_caps: dict[str, Any] | None) -> list[str]:
    """Return canonical ``key=value`` facts for verified capabilities only."""
    if binding_caps is None:
        return []
    if not isinstance(binding_caps, dict):
        raise TypeError(
            f"binding_caps must be a dict, got {type(binding_caps).__name__}"
        )
    facts: list[str] = []
    for key in _CAPABILITY_BOOLEANS:
        value = binding_caps.get(key)
        if isinstance(value, bool):
            facts.append(f"{key}={str(value).lower()}")
    value = binding_caps.get("reference_images_max")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        facts.append(f"reference_images_max={value}")
    for key in _CAPABILITY_LISTS:
        values = binding_caps.get(key)
        if isinstance(values, list):
            cleaned = [item for item in values if isinstance(item, str) and item]
            if cleaned:
                facts.append(f"{key}={','.join(cleaned)}")
    return facts


def structured_layers(
    shot_language: dict[str, Any] | None = None,
    binding_caps: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Return the neutral layer plan every dialect renders from.

    The result has one entry per :data:`LAYER_NAMES` plus ``constraints``.
    Every entry is an ordered list of canonical tokens::

        {"camera": ["lens_mm=50", "depth_of_field=shallow"],
         "movement": ["shot_size=close_up", "camera_movement=dolly_in"],
         "subject": ["subject=...", "texture=grain"],
         "lighting": ["lighting_key=rim_lit", "color_temperature=warm"],
         "style": [],
         "constraints": ["native_audio=false", "aspect_ratios=9:16"]}

    Raises :class:`InvalidShotLanguageError` for values outside the schema
    enums and for unknown keys -- an unvalidated shot must not reach a paid
    provider.
    """
    shot_language = {} if shot_language is None else shot_language
    if not isinstance(shot_language, dict):
        raise InvalidShotLanguageError(
            f"shot_language must be a dict, got {type(shot_language).__name__}"
        )
    unknown = sorted(set(shot_language) - _ALLOWED_KEYS)
    if unknown:
        raise InvalidShotLanguageError(
            f"unknown shot_language key(s): {', '.join(unknown)}"
        )

    layers: dict[str, list[str]] = {name: [] for name in LAYER_NAMES}

    lens = _lens(shot_language)
    if lens is not None:
        layers["camera"].append(f"lens_mm={lens}")
    depth = _enum(shot_language, "depth_of_field", DEPTHS_OF_FIELD)
    if depth is not None:
        layers["camera"].append(f"depth_of_field={depth}")

    shot_size = _enum(shot_language, "shot_size", SHOT_SIZES)
    if shot_size is not None:
        layers["movement"].append(f"shot_size={shot_size}")
    movement = _enum(shot_language, "camera_movement", CAMERA_MOVEMENTS)
    if movement is not None:
        layers["movement"].append(f"camera_movement={movement}")

    subject = _subject(shot_language)
    if subject is not None:
        layers["subject"].append(f"subject={subject}")
    layers["subject"].extend(f"texture={item}" for item in _texture_keywords(shot_language))

    lighting = _enum(shot_language, "lighting_key", LIGHTING_KEYS)
    if lighting is not None:
        layers["lighting"].append(f"lighting_key={lighting}")
    color = _enum(shot_language, "color_temperature", COLOR_TEMPERATURES)
    if color is not None:
        layers["lighting"].append(f"color_temperature={color}")

    layers["constraints"] = _capability_facts(binding_caps)
    return layers