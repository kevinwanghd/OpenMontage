#!/usr/bin/env python3
"""校验 CI 的测试分片没有遗漏：tests/ 下每个 .py 都必须落在某个分片里。

分片定义与 Makefile 的 test-shard-* 一致；新增测试目录时若忘了归片，
这个检查会在 lint job 里直接失败，而不是让 CI 静默少跑用例。

用法: python scripts/check_test_shards.py
退出码: 0 全覆盖；1 有遗漏或分片定义为空。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 与 Makefile 的 test-shard-* 一一对应
SHARDS = {
    "tools": ["tools"],
    "contracts": ["contracts", "lib"],
    "pipeline": ["backlot", "qa", "eval", "pipelines", "styles"],
}
# tests/ 根目录下的散装测试文件归 pipeline 分片
ROOT_FILES_SHARD = "pipeline"


def shard_of(rel: Path) -> str | None:
    parts = rel.parts
    if len(parts) > 1:
        for shard, dirs in SHARDS.items():
            if parts[0] in dirs:
                return shard
        return None  # 未归片的新目录
    return ROOT_FILES_SHARD


def main() -> int:
    files = sorted(p.relative_to(TESTS) for p in TESTS.rglob("*.py"))
    if not files:
        print("tests/ 下没有 Python 文件，分片检查无意义", file=sys.stderr)
        return 1
    unassigned = [str(rel) for rel in files if shard_of(rel) is None]
    counts: dict[str, int] = {name: 0 for name in SHARDS}
    for rel in files:
        shard = shard_of(rel)
        if shard:
            counts[shard] += 1
    if unassigned:
        print("以下测试文件没有归入任何 CI 分片：", file=sys.stderr)
        for name in unassigned:
            print(f"  - tests/{name}", file=sys.stderr)
        print(
            "把它们加进 scripts/check_test_shards.py 的 SHARDS 以及 Makefile 对应的 test-shard-* 目标。",
            file=sys.stderr,
        )
        return 1
    print("分片覆盖完整：" + ", ".join(f"{name}={count}" for name, count in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())