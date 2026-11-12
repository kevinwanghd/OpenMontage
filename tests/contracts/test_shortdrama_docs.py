"""NFR-11 / X-17 contract: the four short-drama docs match the real CLI.

Why this test exists
--------------------
NFR-11 requires the documentation to be *machine-consistent* with the frozen
CLI/error contracts, not merely present. A doc that drifts from
``shortdrama.cli.build_parser()`` or ``shortdrama.errors.ErrorCode`` silently
misleads agents and operators, so this contract walks the live parser and the
live error catalogue and asserts every name is documented.

The test is read-only: it reads the four Markdown files and imports the CLI
parser. No command is executed, no file is written, no network is touched
(M0 local-only guarantee).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from shortdrama.cli import build_parser
from shortdrama.errors import ApiError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

QUICKSTART = DOCS / "shortdrama-quickstart.md"
CLI_REFERENCE = DOCS / "shortdrama-cli-reference.md"
ERROR_CODES = DOCS / "shortdrama-error-codes.md"
RUNBOOK = DOCS / "shortdrama-review-runbook.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required doc: {path}"
    return path.read_text(encoding="utf-8")


def _command_paths(parser: argparse.ArgumentParser, prefix: str = "om") -> list[str]:
    """Every command path reachable from ``parser``, e.g. ``om review list``."""
    paths = [prefix]
    for action in parser._actions:  # noqa: SLF001 - argparse has no public walk API
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        for name, subparser in action.choices.items():
            paths.extend(_command_paths(subparser, f"{prefix} {name}"))
    return paths


@pytest.fixture(scope="module")
def cli_doc() -> str:
    return _read(CLI_REFERENCE)


@pytest.fixture(scope="module")
def error_doc() -> str:
    return _read(ERROR_CODES)


@pytest.fixture(scope="module")
def runbook() -> str:
    return _read(RUNBOOK)


def test_all_four_docs_exist() -> None:
    for path in (QUICKSTART, CLI_REFERENCE, ERROR_CODES, RUNBOOK):
        assert path.is_file(), f"missing NFR-11 deliverable: {path}"
        assert path.stat().st_size > 0, f"empty doc: {path}"


def test_cli_reference_covers_every_live_command(cli_doc: str) -> None:
    paths = _command_paths(build_parser())
    assert "om review" in paths  # sanity: the walk really found subparsers
    assert "om review decide" in paths
    missing = [path for path in paths if path not in cli_doc]
    assert not missing, f"CLI reference is missing live command(s): {missing}"


def test_cli_reference_covers_every_exit_code(cli_doc: str) -> None:
    """Exit codes documented must include 0 and every ApiError mapping."""
    exit_codes = {0} | {ApiError(code, "x").exit_code for code in ErrorCode}
    assert exit_codes == {0, 1, 2, 3, 4, 5}
    missing = [code for code in sorted(exit_codes) if f"| `{code}` |" not in cli_doc]
    assert not missing, f"CLI reference exit-code table missing: {missing}"
    assert "退出码" in cli_doc


def test_error_catalog_covers_every_error_code(error_doc: str) -> None:
    missing = [code.value for code in ErrorCode if f"`{code.value}`" not in error_doc]
    assert not missing, f"error-codes doc missing: {missing}"


def test_error_doc_documents_the_envelope_contract(error_doc: str) -> None:
    for field in (
        "code",
        "what",
        "fix",
        "recovery",
        "side_effects",
        "context",
        "trace",
    ):
        assert f"`{field}`" in error_doc, f"envelope field not documented: {field}"
    # recovery / side_effects enums are part of the §10.3 contract.
    for value in ("retry", "fix-then-retry", "do-X-first", "ask-human"):
        assert value in error_doc
    for value in ("none", "partial", "complete", "unknown"):
        assert value in error_doc


def test_runbook_covers_review_operating_surfaces(runbook: str) -> None:
    lowered = runbook.lower()
    for keyword in ("batch", "undo", "stale", "watch", "policy"):
        assert keyword in lowered, f"runbook missing required topic: {keyword}"
    for layer in ("L1", "L2", "L3"):
        assert layer in runbook, f"runbook missing review layer: {layer}"
    for mode in ("sampled", "all"):
        assert mode.lower() in lowered, f"runbook missing sampling mode: {mode}"


def test_quickstart_is_zero_key_hello_world() -> None:
    quickstart = _read(QUICKSTART)
    for token in (
        "om doctor",
        "om demo --mock",
        "om review list",
        "om review decide",
        "om review stats",
    ):
        assert token in quickstart, f"quickstart missing step: {token}"
    # Windows path/commands must be present for the primary supported platform.
    assert "PowerShell" in quickstart
    assert "Scripts" in quickstart
    assert "external_api_calls" in quickstart