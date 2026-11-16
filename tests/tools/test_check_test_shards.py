"""CI 分片覆盖校验脚本的测试。

这个脚本存在的意义是防止 CI 静默少跑用例，所以它自己必须被验证：
分片映射正确、未归片的目录会被判失败。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_test_shards.py"

spec = importlib.util.spec_from_file_location("check_test_shards", SCRIPT)
assert spec and spec.loader
check_test_shards = importlib.util.module_from_spec(spec)
sys.modules["check_test_shards"] = check_test_shards
spec.loader.exec_module(check_test_shards)


class TestShardMapping:

    def test_known_directories_map_to_expected_shards(self):
        assert check_test_shards.shard_of(Path("tools/test_x.py")) == "tools"
        assert check_test_shards.shard_of(Path("contracts/test_x.py")) == "contracts"
        assert check_test_shards.shard_of(Path("lib/test_x.py")) == "contracts"
        assert check_test_shards.shard_of(Path("qa/test_x.py")) == "pipeline"
        assert check_test_shards.shard_of(Path("backlot/test_x.py")) == "pipeline"

    def test_root_level_file_goes_to_pipeline(self):
        assert check_test_shards.shard_of(Path("test_network_guard.py")) == "pipeline"

    def test_unknown_directory_is_unassigned(self):
        # 新增测试目录却没归片时必须暴露出来，而不是被默认塞进某个分片
        assert check_test_shards.shard_of(Path("brand_new_suite/test_x.py")) is None

    def test_repository_tests_are_fully_covered(self):
        assert check_test_shards.main() == 0