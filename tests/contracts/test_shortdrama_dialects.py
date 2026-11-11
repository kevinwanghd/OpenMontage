"""T6 contract tests for prompt dialects (MOD-04 / VIS-07).

Guards:
- One structured ``shot_language`` renders through every dialect with the same
  meaning and a different string: the dialect layer changes wording, never
  cinematography.
- Binding capabilities actually reach the prompt, and unverified capability
  values ("unknown") are ignored instead of guessed (MOD-02).
- ``register_dialect`` extends the pipeline by name alone; the existing
  dialects and the call site are untouched (MOD-04, X-3).
- An unknown dialect fails loudly with the registered names in the message.
- Invalid shot language never reaches a provider prompt.

M0: pure local rendering, zero external API calls.
"""

from __future__ import annotations

import copy

import pytest

import lib.prompt_dialects as pd
from lib.prompt_dialects import (
    DialectRegistrationError,
    InvalidShotLanguageError,
    UnknownDialectError,
    available_dialects,
    register_dialect,
    render,
    structured_layers,
)
from lib.prompt_dialects import dashscope, mock

SHOT = {
    "shot_size": "close_up",
    "camera_movement": "dolly_in",
    "lens_mm": 50,
    "lighting_key": "rim_lit",
    "depth_of_field": "shallow",
    "color_temperature": "warm",
}

CAPS = {
    "native_audio": False,
    "lipsync_from_dialogue": False,
    "first_frame_conditioning": True,
    "reference_images_max": 2,
    "aspect_ratios": ["9:16"],
    "output_resolutions": ["480p"],
}

#: field -> (canonical mock token, dashscope phrase table, value under test)
FIELD_REPRESENTATIONS = [
    ("shot_size", "close_up", dashscope.SHOT_SIZE_PHRASES),
    ("camera_movement", "dolly_in", dashscope.CAMERA_MOVEMENT_PHRASES),
    ("lens_mm", 50, dashscope.LENS_PHRASES),
    ("lighting_key", "rim_lit", dashscope.LIGHTING_PHRASES),
    ("depth_of_field", "shallow", dashscope.DEPTH_OF_FIELD_PHRASES),
    ("color_temperature", "warm", dashscope.COLOR_TEMPERATURE_PHRASES),
]


@pytest.fixture()
def isolated_registry(monkeypatch):
    """Snapshot the dialect registry so a test registration never leaks."""
    monkeypatch.setattr(pd, "_REGISTRY", dict(pd._REGISTRY))
    return pd._REGISTRY


# --- built-ins -------------------------------------------------------------


def test_builtin_dialects_are_registered():
    dialects = available_dialects()
    assert "dashscope" in dialects
    assert "mock" in dialects


def test_structured_layers_is_the_shared_five_layer_plan():
    layers = structured_layers(SHOT, CAPS)

    assert set(layers) == {"camera", "movement", "subject", "lighting", "style", "constraints"}
    assert layers["camera"] == ["lens_mm=50", "depth_of_field=shallow"]
    assert layers["movement"] == ["shot_size=close_up", "camera_movement=dolly_in"]
    assert layers["subject"] == []
    assert layers["lighting"] == ["lighting_key=rim_lit", "color_temperature=warm"]
    assert layers["style"] == []
    assert "native_audio=false" in layers["constraints"]
    assert "aspect_ratios=9:16" in layers["constraints"]


def test_subject_context_reaches_the_neutral_subject_layer():
    layers = structured_layers(
        {"shot_size": "medium", "subject": "雨夜巷口的人影", "texture_keywords": ["grain"]}
    )

    assert layers["subject"] == ["subject=雨夜巷口的人影", "texture=grain"]


# --- same shot, different dialect, same meaning ----------------------------


def test_same_shot_language_renders_differently_per_dialect():
    dashscope_prompt = render(SHOT, CAPS, dialect="dashscope")
    mock_prompt = render(SHOT, CAPS, dialect="mock")

    assert dashscope_prompt != mock_prompt
    # Distinct vocabularies: no canonical tokens leak into prose, no Chinese
    # prose leaks into the token dialect.
    assert "shot_size=" not in dashscope_prompt
    assert "特写" not in mock_prompt
    assert "shot_size=close_up" in mock_prompt
    assert "特写" in dashscope_prompt


@pytest.mark.parametrize("field,value,phrases", FIELD_REPRESENTATIONS)
def test_every_field_renders_through_both_dialects(field, value, phrases):
    dashscope_prompt = render(SHOT, CAPS, dialect="dashscope")
    mock_prompt = render(SHOT, CAPS, dialect="mock")

    assert phrases[value] in dashscope_prompt
    assert f"{field}={value}" in mock_prompt


def test_static_camera_is_a_semantic_value_not_an_omission():
    shot = {"shot_size": "medium", "camera_movement": "static"}

    assert "固定机位" in render(shot, {}, dialect="dashscope")
    assert "camera_movement=static" in render(shot, {}, dialect="mock")


# --- binding capabilities --------------------------------------------------


def test_native_audio_capability_changes_both_dialects():
    without_audio = dict(CAPS, native_audio=False)
    with_audio = dict(CAPS, native_audio=True)

    no_audio_prompt = render(SHOT, without_audio, dialect="dashscope")
    audio_prompt = render(SHOT, with_audio, dialect="dashscope")
    assert "无原生音频，口型需与对白匹配" in no_audio_prompt
    assert "带原生音频与对白" in audio_prompt
    assert no_audio_prompt != audio_prompt

    assert "constraint=native_audio=false" in render(SHOT, without_audio, dialect="mock")
    assert "constraint=native_audio=true" in render(SHOT, with_audio, dialect="mock")


def test_reference_first_frame_and_resolution_capabilities_reach_the_prompt():
    prompt = render(SHOT, CAPS, dialect="dashscope")
    mock_prompt = render(SHOT, CAPS, dialect="mock")

    assert "参考图不超过 2 张" in prompt
    assert "以首帧图作为起始画面" in prompt
    assert "输出分辨率 480p" in prompt
    assert "竖屏 9:16" in prompt
    assert "constraint=reference_images_max=2" in mock_prompt
    assert "constraint=output_resolutions=480p" in mock_prompt


def test_zero_reference_images_says_so_instead_of_dropping_the_fact():
    prompt = render(SHOT, {"reference_images_max": 0}, dialect="dashscope")

    assert "不使用参考图" in prompt


def test_unverified_capabilities_are_ignored_not_assumed():
    unknown_caps = {
        "native_audio": "unknown",
        "first_frame_conditioning": None,
        "reference_images_max": "unknown",
        "aspect_ratios": [],
        "seed_control": True,
    }

    assert render(SHOT, unknown_caps, dialect="mock") == render(SHOT, {}, dialect="mock")
    assert render(SHOT, unknown_caps, dialect="dashscope") == render(
        SHOT, {}, dialect="dashscope"
    )


def test_non_vertical_aspect_ratio_is_not_rendered_as_vertical():
    prompt = render(SHOT, {"aspect_ratios": ["16:9"]}, dialect="dashscope")

    assert "画幅 16:9" in prompt
    assert "竖屏" not in prompt


# --- extension by registration ---------------------------------------------


def test_register_dialect_extends_dispatch_without_pipeline_change(isolated_registry):
    before = {
        name: render(SHOT, CAPS, dialect=name) for name in available_dialects()
    }
    seen: list[tuple[dict, dict]] = []

    def canary(shot_language: dict, binding_caps: dict) -> str:
        seen.append((shot_language, binding_caps))
        layers = structured_layers(shot_language, binding_caps)
        return "canary|" + ",".join(layers["movement"])

    register_dialect("canary", canary)

    assert "canary" in available_dialects()
    # The existing call site routes to the new dialect unchanged.
    assert render(SHOT, CAPS, dialect="canary") == (
        "canary|shot_size=close_up,camera_movement=dolly_in"
    )
    assert seen == [(SHOT, CAPS)]
    # Registering a dialect does not disturb the built-ins.
    for name, prompt in before.items():
        assert render(SHOT, CAPS, dialect=name) == prompt


def test_duplicate_registration_requires_explicit_replace(isolated_registry):
    def first(shot_language: dict, binding_caps: dict) -> str:
        return "first"

    def second(shot_language: dict, binding_caps: dict) -> str:
        return "second"

    register_dialect("probe", first)
    with pytest.raises(DialectRegistrationError, match="already registered"):
        register_dialect("probe", second)

    register_dialect("probe", second, replace=True)
    assert render(SHOT, {}, dialect="probe") == "second"


@pytest.mark.parametrize("name", ["", "DashScope", "has-dash", "1leading", None])
def test_malformed_dialect_names_are_rejected(name):
    with pytest.raises(DialectRegistrationError, match="invalid dialect name"):
        register_dialect(name, lambda shot_language, binding_caps: "x")


def test_non_callable_renderer_is_rejected():
    with pytest.raises(DialectRegistrationError, match="must be callable"):
        register_dialect("not_a_function", "nope")


def test_registered_dialect_returning_empty_prompt_fails_loudly(isolated_registry):
    register_dialect("empty", lambda shot_language, binding_caps: "  ")

    with pytest.raises(pd.PromptDialectError, match="empty prompt"):
        render(SHOT, CAPS, dialect="empty")


# --- unknown dialect -------------------------------------------------------


def test_unknown_dialect_fails_with_the_registered_names():
    with pytest.raises(UnknownDialectError) as excinfo:
        render(SHOT, CAPS, dialect="veo")

    message = str(excinfo.value)
    assert "veo" in message
    assert "dashscope" in message
    assert "mock" in message


# --- invalid shot language -------------------------------------------------


@pytest.mark.parametrize(
    "shot_language,match",
    [
        ({"shot_size": "bogus"}, "unknown shot_size"),
        ({"camera_movement": "spin"}, "unknown camera_movement"),
        ({"lighting_key": "candle"}, "unknown lighting_key"),
        ({"depth_of_field": "infinite"}, "unknown depth_of_field"),
        ({"color_temperature": "green"}, "unknown color_temperature"),
        ({"lens_mm": 60}, "unknown lens_mm"),
        ({"lens_mm": True}, "unknown lens_mm"),
        ({"subject": 42}, "subject must be a string"),
        ({"texture_keywords": "grain"}, "texture_keywords must be a list"),
        ({"mood": "tense"}, "unknown shot_language key"),
    ],
)
def test_invalid_shot_language_is_rejected_before_rendering(shot_language, match):
    torch = dict(shot_language)

    with pytest.raises(InvalidShotLanguageError, match=match):
        render(torch, CAPS, dialect="dashscope")
    with pytest.raises(InvalidShotLanguageError, match=match):
        render(torch, CAPS, dialect="mock")


# --- determinism and purity ------------------------------------------------


def test_rendering_is_deterministic_and_does_not_mutate_inputs():
    shot = copy.deepcopy(SHOT)
    caps = copy.deepcopy(CAPS)

    for dialect in ("dashscope", "mock"):
        first = render(shot, caps, dialect=dialect)
        second = render(shot, caps, dialect=dialect)
        assert first == second

    assert shot == SHOT
    assert caps == CAPS


def test_empty_shot_language_still_yields_a_prompt():
    assert render({}, {}, dialect="mock") == (
        "camera=-\nmovement=-\nsubject=-\nlighting=-\nstyle=-"
    )
    assert render({}, {}, dialect="dashscope") == "电影感短剧镜头。"