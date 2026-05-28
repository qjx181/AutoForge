"""self_evolve_round.py — 项目三自进化后勤脚本

职责（每 30 分钟由 cronjob 触发）：
  1. PID 文件锁 + 冲突自愈
  2. 磁盘空间检查 + 日志轮转
  3. 成本熔断检查
  4. 项目一同步（git pull + commit）
  5. 项目三同步（git pull + commit）
  6. 🚀 持续优化引擎（九维全覆盖，任意目标项目）：
       扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快
  7. 分层委托诊断 + 强制委托检查
  8. ⬆️ 并行任务规划（微委托集成）
  9. 更新 state.json

注意：
  实际的任务执行（write_file / delegate_task）由 Hermes Agent cronjob 的 prompt 驱动。
  本脚本只做"后勤 + 规划"——打扫战场、生成执行计划。
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

from src.core.evolve.cost import GIT_TIMEOUT
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




def check_and_heal_conflicts():
    """检查并自动恢复冲突状态。"""
    state = load_state()
    if state.get("paused_due_to_conflict"):
        conflict_files = state.get("pending_review", [])
        relog("🩹", "冲突状态中，待检查: %s", conflict_files)
        return False
    return True


def mark_conflict(conflict_files: list[str]):
    """标记冲突状态。"""
    state = load_state()
    state["paused_due_to_conflict"] = True
    state["pending_review"] = conflict_files
    save_state(state)


