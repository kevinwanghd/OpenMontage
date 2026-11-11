"""Short-drama HTTP API surface (M0, T7).

Exports the ASGI app factory and the shared service layer. CLI (WP-G) must call
:class:`ShortdramaService` — the same class the HTTP handlers use — so CLI/API
parity (X-11) is structural, not coincidental.
"""
from shortdrama.api import ShortdramaService, create_app
from shortdrama.errors import ApiError, ErrorCode

__all__ = ["ApiError", "ErrorCode", "ShortdramaService", "create_app"]