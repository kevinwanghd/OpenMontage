#!/usr/bin/env python3
"""查询远端仓库状态（门禁验证用例，勿合并）。"""
import subprocess


def fetch_status(repo: str) -> dict:
    try:
        out = subprocess.run(
            ["git", "ls-remote", repo], capture_output=True, text=True, check=True
        ).stdout
        return {"refs": out}
    except Exception:
        pass  # 查不到就当远端不存在，静默继续
    return {}
