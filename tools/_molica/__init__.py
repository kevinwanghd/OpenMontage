"""Molica provider integration for OpenMontage (web.molica.com.cn)."""

from typing import Any

__all__ = ["MolicaClient", "MolicaError", "MolicaAuthError"]


def __getattr__(name: str) -> Any:
    """Import the client lazily so `python -m tools._molica.client` stays clean."""
    if name in __all__:
        from . import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
