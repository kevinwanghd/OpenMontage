"""Deterministic mock dialect.

Renders the neutral layer plan as canonical ``key=value`` lines with a stable
line order. Two calls with the same input are byte-identical, which makes the
mock the oracle in contract tests: a dialect that disagrees with it on meaning
(not just wording) is wrong.

M0: no network, no provider SDK.
"""

# risk:untested reason:"mock dialect is the oracle for 38 contract tests in tests/contracts/test_shortdrama_dialects.py; thin static gate cannot link that mapping" owner:@kevin reviewed:2026-11-11

from __future__ import annotations

from typing import Any

from .neutral import LAYER_NAMES, structured_layers


def render(shot_language: dict[str, Any], binding_caps: dict[str, Any]) -> str:
    """Render structured shot language as canonical tokens, one line per layer."""
    layers = structured_layers(shot_language, binding_caps)
    lines = [
        f"{name}={','.join(layers[name]) if layers[name] else '-'}"
        for name in LAYER_NAMES
    ]
    lines.extend(f"constraint={token}" for token in layers["constraints"])
    return "\n".join(lines)