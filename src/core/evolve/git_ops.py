"""evolve/git_ops — Git 操作封装（pull/commit/push/冲突处理）

职责：
  - git_pull_rebase: 拉取并处理冲突
  - run_git_commit / run_git_commit_with_retry: 带重试的提交
  - check_and_heal_conflicts / mark_conflict: 冲突状态管理
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import os
from typing import Any
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from src.core.evolve.cost import GIT_TIMEOUT
from src.core.evolve.logging import relog
from src.core.evolve.state import load_state, save_state
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


def _run_git(cmd: list[str], repo_dir: Path, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """执行 git 命令的辅助函数。"""
    return subprocess.run(
        cmd,
        cwd=str(repo_dir),
        capture_output=True, text=True, timeout=timeout,
    )


def git_pull_rebase(repo_dir: Path) -> tuple[bool, list[str]]:
    """git pull --rebase。返回 (是否成功, 冲突文件列表)。"""
    try:
        result = _run_git(["git", "pull", "--rebase"], repo_dir)
        if result.returncode != 0:
            conflicts = []
            for line in result.stderr.splitlines():
                if "CONFLICT" in line and "content" in line:
                    parts = line.split("in ")
                    if len(parts) >= 2:
                        conflicts.append(parts[-1].strip())
                if "both modified:" in line:
                    parts = line.split("both modified:")
                    if len(parts) >= 2:
                        conflicts.append(parts[-1].strip())
            return False, conflicts
        return True, []
    except subprocess.TimeoutExpired:
        return False, []


def run_git_commit(repo_dir: Path, message: str, skip_pull: bool = False) -> bool:
    """git add -A + commit。"""
    try:
        status = _run_git(["git", "status", "--porcelain"], repo_dir, timeout=10)
        if not status.stdout.strip():
            return True  # 干净，无需提交

        _run_git(["git", "add", "-A"], repo_dir, timeout=30)
        cmt = _run_git(["git", "commit", "-m", message], repo_dir, timeout=30)
        relog("✅", "提交成功: %s  (%s)", message[:50], (cmt.stdout or "")[:30])
        return True
    except subprocess.TimeoutExpired:
        relog("❌", "git commit 超时")
        return False


def run_git_commit_with_retry(repo_dir: Path, message: str, repo_name: str = "unknown", max_retries: int = 3) -> bool:
    """带重试的 git commit。"""
    for attempt in range(max_retries):
        try:
            if run_git_commit(repo_dir, message):
                return True
            time.sleep(2 ** attempt)
        except Exception as e:
            relog("⚠️", "%s 第 %d 次重试: %s", repo_name, attempt + 1, e)
    relog("❌", "%s 最终失败", repo_name)
    return False




def check_and_heal_conflicts() -> Any:
    """检查并自动恢复冲突状态。"""
    state = load_state()
    if state.get("paused_due_to_conflict"):
        conflict_files = state.get("pending_review", [])
        relog("🩹", "冲突状态中，待检查: %s", conflict_files)
        return False
    return True


def mark_conflict(conflict_files: list[str]) -> None:
    """标记冲突状态。"""
    state = load_state()
    state["paused_due_to_conflict"] = True
    state["pending_review"] = conflict_files
    save_state(state)


