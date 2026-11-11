"""``python -m shortdrama`` entry point (NFR-12 / X-8)."""
# risk:untested reason:"入口模块仅转发 main, 行为由 tests/contracts/test_shortdrama_cli.py 的入口测试覆盖, thin 模式无法绑定运行证据" owner:@kevinwanghd reviewed:2026-11-11

from __future__ import annotations

import sys

from shortdrama.cli import main


def _run() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_run())