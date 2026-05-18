#!/usr/bin/env python3
"""自进化循环 - Git后勤脚本。

作用：Hermes cronjob 完成 A→B→Git 主循环后的后勤保障。
在 Hermes cronjob 每30分钟触发完整 A→B→Git 的同时，
本脚本作为 backup 确保：
1. 项目一未提交的变更被 Git 提交
2. 项目三的 CHANGELOG 和 TODO 同步状态
3. 状态日志输出，供排查问题

为什么需要这个：
Hermes cronjob 可能因网络波动、模型错误等原因中途中断，
导致 A 队代码已写入但 Git 未提交。本脚本兜底。

逻辑：
1. 检查项目一 Git 状态 → 如有未提交变更则 commit
2. 检查项目三 Git 状态 → 如有 TODO/CHANGELOG 变更则 commit
3. 输出状态报告（当前待办、Git 状态、时间戳）
"""

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("self_evolve_round")

# 核心路径
PROJECT_ONE = Path(
    "/mnt/c/Users/qjx/Desktop/agent-自进化版/项目一cursor版本/在线部分"
)
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
TODO_PATH = SWARM_DIR / "TODO.md"
CHANGELOG_PATH = SWARM_DIR / "CHANGELOG.md"


def run_git_commit(repo_dir: Path, message: str) -> bool:
    """作用：在指定仓库执行 git add + commit（不 push）
    为什么：Hermes cronjob 可能中断导致代码未提交，本函数兜底
    逻辑：add -A → 检查是否有变更 → 有则 commit，无则跳过"""
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(repo_dir),
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("  → 无变更，跳过 commit")
            return True

        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if commit.returncode == 0:
            logger.info("  ✅ commit 成功: %s", commit.stdout.strip())
            return True
        else:
            logger.warning("  ⚠️ commit 失败: %s", commit.stderr.strip())
            return False
    except subprocess.CalledProcessError as e:
        logger.error("  ❌ Git 操作失败: %s", e)
        return False
    except FileNotFoundError:
        logger.error("  ❌ 目录不存在: %s", repo_dir)
        return False


def read_todo_first_task() -> str:
    """读取 TODO.md 的第一条未完成任务"""
    try:
        if not TODO_PATH.exists():
            return "TODO.md 不存在"
        content = TODO_PATH.read_text(encoding="utf-8")
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- [ ] "):
                return stripped.replace("- [ ] ", "").strip()
        return "所有待办已完成"
    except Exception as e:
        return f"读取失败: {e}"


def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=" * 55)
    logger.info("后勤脚本启动 — %s", timestamp)
    logger.info("=" * 55)

    # 1. 读取当前待办
    current_task = read_todo_first_task()
    logger.info("当前待办: %s", current_task)

    # 2. 检查项目一 Git 状态
    logger.info("项目一 Git 状态:")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(PROJECT_ONE),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.stdout.strip():
            logger.info("  ⚠️ 有 %d 个未提交文件", len(status.stdout.strip().split("\n")))
            for line in status.stdout.strip().split("\n"):
                logger.info("    %s", line)
            # 兜底提交
            run_git_commit(
                PROJECT_ONE,
                f"swarm-evolve: 后勤自动提交 — {timestamp[:10]}",
            )
        else:
            logger.info("  ✅ 工作区干净")
    except Exception as e:
        logger.warning("  ⚠️ 检查失败: %s", e)

    # 3. 检查项目三 Git 状态
    logger.info("项目三（Swarm）Git 状态:")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(SWARM_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.stdout.strip():
            logger.info("  ⚠️ 有 %d 个未提交文件", len(status.stdout.strip().split("\n")))
            for line in status.stdout.strip().split("\n"):
                logger.info("    %s", line)
            run_git_commit(
                SWARM_DIR,
                f"swarm-evolve: 后勤同步 — {timestamp[:10]}",
            )
        else:
            logger.info("  ✅ 工作区干净")
    except Exception as e:
        logger.warning("  ⚠️ 检查失败: %s", e)

    # 4. 小提示
    logger.info("")
    logger.info("提示：主要 A→B→Git 由 Hermes cronjob 每30分钟自动执行")
    logger.info("      swd_evolve_round.py 仅做 Git 后勤兜底")
    logger.info("=" * 55)


if __name__ == "__main__":
    main()
