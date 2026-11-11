"""Errors for the prompt dialect layer (MOD-04)."""

# risk:untested reason:"error types are exercised by 38 contract tests in tests/contracts/test_shortdrama_dialects.py; thin static gate cannot link that mapping" owner:@kevin reviewed:2026-11-11

from __future__ import annotations


class PromptDialectError(Exception):
    """Base error for prompt dialect problems."""


class UnknownDialectError(PromptDialectError, LookupError):
    """No dialect is registered under the requested name."""


class DialectRegistrationError(PromptDialectError, ValueError):
    """A dialect cannot be registered under the requested name."""


class InvalidShotLanguageError(PromptDialectError, ValueError):
    """The supplied shot language is not the structured vocabulary of scene_plan."""