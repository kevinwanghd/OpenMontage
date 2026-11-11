"""``python -m shortdrama`` entry point (NFR-12 / X-8)."""

from __future__ import annotations

import sys

from shortdrama.cli import main


def _run() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_run())