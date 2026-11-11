"""Provider prompt dialects for structured shot language (MOD-04 / VIS-07).

A dialect is a pure function ``renderer(shot_language, binding_caps) -> str``
registered by name. The pipeline only ever calls :func:`render`; adding a
provider dialect means calling :func:`register_dialect`, never editing the
pipeline or the other dialects (MOD-04, X-3).

What makes two dialects "the same shot, different wording" is that both start
from :func:`structured_layers` -- the neutral five-layer plan from
:mod:`lib.prompt_dialects.neutral`. Built-ins:

- ``dashscope`` -- wan3.0-video style Chinese cinematography prose.
- ``mock`` -- deterministic canonical tokens, the contract-test oracle.

Unknown is never support: unverified ``binding_caps`` values produce no
constraint instead of a default. M0: pure local rendering, zero external API.
"""

# risk:untested reason:"dialect package is covered by 38 contract tests in tests/contracts/test_shortdrama_dialects.py; thin static gate cannot link that mapping" owner:@kevin reviewed:2026-11-11

from __future__ import annotations

import re
from typing import Any, Callable

from . import dashscope as _dashscope
from . import mock as _mock
from .errors import (
    DialectRegistrationError,
    InvalidShotLanguageError,
    PromptDialectError,
    UnknownDialectError,
)
from .neutral import LAYER_NAMES, structured_layers

__all__ = [
    "DialectRegistrationError",
    "DialectRenderer",
    "InvalidShotLanguageError",
    "LAYER_NAMES",
    "PromptDialectError",
    "UnknownDialectError",
    "available_dialects",
    "get_dialect",
    "register_dialect",
    "render",
    "structured_layers",
]

#: ``renderer(shot_language, binding_caps) -> prompt``.
DialectRenderer = Callable[[dict[str, Any], dict[str, Any]], str]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_REGISTRY: dict[str, DialectRenderer] = {}


def register_dialect(
    name: str, renderer: DialectRenderer, *, replace: bool = False
) -> None:
    """Register ``renderer`` under ``name``; adding a dialect needs no pipeline change.

    Raises :class:`DialectRegistrationError` for a malformed or already-used
    name, or for a non-callable renderer. ``replace=True`` is the explicit
    opt-in for overriding an existing dialect.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DialectRegistrationError(
            f"invalid dialect name {name!r}; use lowercase letters, digits and underscores"
        )
    if not callable(renderer):
        raise DialectRegistrationError(
            f"dialect {name!r} renderer must be callable, got {type(renderer).__name__}"
        )
    if name in _REGISTRY and not replace:
        raise DialectRegistrationError(
            f"dialect {name!r} is already registered; pass replace=True to override"
        )
    _REGISTRY[name] = renderer


def available_dialects() -> list[str]:
    """Return the registered dialect names, sorted."""
    return sorted(_REGISTRY)


def get_dialect(name: str) -> DialectRenderer:
    """Return the renderer registered under ``name`` or raise :class:`UnknownDialectError`."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(available_dialects()) or "none"
        raise UnknownDialectError(
            f"unknown prompt dialect {name!r}; registered: {known}"
        ) from None


def render(
    shot_language: dict[str, Any], binding_caps: dict[str, Any], *, dialect: str
) -> str:
    """Render structured shot language into ``dialect``'s prompt.

    Args:
        shot_language: Structured cinematography from ``scene_plan`` (see
            :func:`structured_layers` for the accepted vocabulary).
        binding_caps: Capabilities of the binding that will generate the shot
            (``native_audio``, ``first_frame_conditioning``,
            ``reference_images_max``, ``aspect_ratios``, ...). Unverified
            values are ignored rather than assumed.
        dialect: Registered dialect name.

    Raises:
        UnknownDialectError: no dialect is registered under ``dialect``.
        InvalidShotLanguageError: the shot language is outside the schema enums.
        PromptDialectError: the dialect returned an empty prompt.
    """
    renderer = get_dialect(dialect)
    prompt = renderer(dict(shot_language or {}), dict(binding_caps or {}))
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptDialectError(f"dialect {dialect!r} returned an empty prompt")
    return prompt


register_dialect("dashscope", _dashscope.render)
register_dialect("mock", _mock.render)