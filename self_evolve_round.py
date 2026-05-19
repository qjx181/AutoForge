#!/usr/bin/env python3
"""
self_evolve_round.py — Swarm 自我进化后勤脚本（第 4 版）

作用：
  运行完整的多 Agent 循环，包括：
  - 磁盘监控（可用空间检测 + 旧日志自动清理）
  - PID 文件管理（防重复启动 + 僵尸自动恢复）
  - git 冲突检测（超 1 小时自动 reset）
  - 成本熔断（日预算超限时跳过 LLM 调用）
  - Git 后勤（pull --rebase + commit + push）

原理：
  此脚本是安全门禁层，不执行任务逻辑。
  实际 A→B→Git 循环由 Hermes cronjob 每 30 分钟通过
  orchestrate-swarm SKILL 触发。本脚本负责：
  - 系统级健康检查（磁盘/PID/冲突/成本）
  - Git 状态的检查和自动提交
  - 日志清理（防磁盘占满）
  - state.json 持久化

逻辑：
  1. 获取 PID 文件锁（防并发执行）
  2. 检查磁盘空间（<100MB 自动清理旧日志）
  3. 检查 git 冲突（超 1 小时自动 git reset --hard HEAD）
  4. 检查成本（超日预算 = 只读，跳过 LLM 调用）
  5. Git 后勤：pull --rebase + 提交未推送变更
  6. 更新 state.json
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── 路径常量 ──────────────────────────────────────────────────────────
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
PROJECT1_DIR = Path("/mnt/c/Users/qjx/Desktop/agent-自进化版/项目一cursor版本/在线部分")
PID_FILE = SWARM_DIR / "swarm_evolve.pid"
STATE_FILE = SWARM_DIR / "state.json"
FAILED_EXAMPLES_FILE = SWARM_DIR / "failed_examples.jsonl"
LOG_DIR = SWARM_DIR / "logs"

# ─── 超时常量（秒）─────────────────────────────────────────────────────
GIT_TIMEOUT = 30
DOCKER_TIMEOUT = 30

# ─── 重试参数 ──────────────────────────────────────────────────────────
RETRY_MAX_ATTEMPTS = 3
RETRY_INITIAL_DELAY = 5

# ─── 磁盘监控参数 ──────────────────────────────────────────────────────
DISK_PAUSE_MB = 100
LOG_DIR_MAX_MB = 500

# ─── 自愈参数（项8）────────────────────────────────────────────────────
CONFLICT_TIMEOUT_SECONDS = 3600       # 冲突超 1 小时自动 reset
PID_ZOMBIE_TIMEOUT_SECONDS = 120      # PID 文件僵尸超 2 分钟自动删除
DISK_MIN_MB = 100                     # 磁盘低于此值触发日志清理
DISK_TARGET_MB = 500                  # 日志清理后的目标可用空间

# ─── 成本熔断参数 ──────────────────────────────────────────────────────
COST_OVER_BUDGET_RATIO = 2.0


# ═══════════════════════════════════════════════════════════════════════
# 日志辅助函数
# ═══════════════════════════════════════════════════════════════════════

def relog(emoji: str, msg: str, *args):
    """relog — 带 emoji 前缀的控制台日志输出。

    作用：统一日志格式，方便人类阅读。
    原理：使用 print 直接输出到 stderr，不依赖 logging 库（减少依赖）。
    逻辑：如果 args 非空，用 msg % args 格式化；否则直接输出 msg。
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {emoji} {msg}"
    if args:
        line = line % args
    print(line, file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════
# 磁盘监控 + 日志清理（项8 - 自愈3）
# ═══════════════════════════════════════════════════════════════════════

def check_disk_space() -> dict:
    """check_disk_space — 检查可用磁盘空间，低于阈值自动清理旧日志。

    作用：
      防止磁盘写满导致系统不可用。当 /mnt/f 可用空间 < DISK_MIN_MB 时，
      自动删除 LOG_DIR 下最旧的日志文件，直到可用空间 >= DISK_TARGET_MB。

    原理：
      使用 shutil.disk_usage 获取分区可用字节数，转换为 MB 后判断。
      日志清理策略：按文件修改时间排序，删最旧的。

    逻辑：
      1. 获取 /mnt/f 分区的磁盘使用情况
      2. 如果 available_mb < DISK_MIN_MB：调用 delete_oldest_logs()
      3. 如果 available_mb < DISK_PAUSE_MB（旧常量 100）：返回 paused=True
      4. 更新 state.json 的 disk_status

    Returns:
        {"available_mb": float, "logs_dir_size_mb": float, "warning": str|None}
    """
    try:
        usage = shutil.disk_usage("/mnt/f")
        available_mb = usage.free / (1024 * 1024)
    except FileNotFoundError:
        # /mnt/f 可能不存在（如 WSL 未挂载），回退到 /
        usage = shutil.disk_usage("/")
        available_mb = usage.free / (1024 * 1024)

    # 计算日志目录大小
    logs_dir_size_mb = 0
    if LOG_DIR.exists():
        for fpath in LOG_DIR.rglob("*"):
            if fpath.is_file():
                logs_dir_size_mb += fpath.stat().st_size / (1024 * 1024)

    warning = None
    paused = False

    # ── 自愈：磁盘 < 100MB 自动删旧日志直到 > 500MB ──
    if available_mb < DISK_MIN_MB:
        relog("🛠️", "磁盘空间不足（%.0f MB），触发日志自愈清理", available_mb)
        deleted = delete_oldest_logs(target_mb=DISK_TARGET_MB)
        relog("🛠️", "日志清理完成，删除了 %d 个文件", deleted)
        # 重新检查可用空间
        try:
            usage = shutil.disk_usage("/mnt/f")
            available_mb = usage.free / (1024 * 1024)
        except FileNotFoundError:
            usage = shutil.disk_usage("/")
            available_mb = usage.free / (1024 * 1024)

    if available_mb < DISK_PAUSE_MB:
        warning = f"磁盘可用仅 {available_mb:.0f} MB，低于暂停阈值 {DISK_PAUSE_MB} MB"
        paused = True
        relog("⚠️", "%s", warning)

    result = {
        "available_mb": round(available_mb, 1),
        "logs_dir_size_mb": round(logs_dir_size_mb, 1),
        "warning": warning,
        "paused": paused,
    }

    # 更新 state.json
    state = load_state()
    state["disk_status"] = result
    save_state(state)

    return result


def delete_oldest_logs(target_mb: int = 500) -> int:
    """delete_oldest_logs — 删除最旧的日志文件，直到可用空间达到目标值。

    Args:
        target_mb: 日志清理后的目标可用空间（MB），默认 500。

    Returns:
        删除的文件总数。

    作用（项8 - 自愈3）：磁盘不足时自动恢复，防止系统崩溃。
    原理：FIFO 策略，先删修改时间最久远的日志文件。
    逻辑：
      1. 收集 logs/ 下所有 .log 文件并按修改时间排序（旧 → 新）
      2. 逐个删除，每删一个检查一次可用空间
      3. 直到可用空间 >= target_mb 或所有日志删完为止

    面试追问：
    - 为什么不用磁盘配额而用这种"边删边查"的方式？
      答：因为 Linux 磁盘配额基于用户/组，对单目录不精准。
    - 会不会删掉正在写入的日志？
      答：日志按日期分目录，旧日期的日志不会同时被写入。
    """
    if not LOG_DIR.exists():
        relog("📁", "日志目录不存在，跳过清理")
        return 0

    # 收集所有 .log 文件，按修改时间排序
    log_files = sorted(
        [f for f in LOG_DIR.rglob("*.log") if f.is_file()],
        key=lambda f: f.stat().st_mtime,
    )

    if not log_files:
        relog("📁", "没有找到日志文件，跳过清理")
        return 0

    deleted_count = 0
    for fpath in log_files:
        try:
            usage = shutil.disk_usage("/mnt/f")
            available_mb = usage.free / (1024 * 1024)
            if available_mb >= target_mb:
                break
            fpath.unlink()
            deleted_count += 1
            relog("🗑️", "删除旧日志: %s", fpath.name)
        except FileNotFoundError:
            continue
        except OSError as e:
            relog("⚠️", "删除日志失败: %s: %s", fpath, e)

    if deleted_count > 0:
        relog("✅", "日志清理完成，共删除 %d 个文件", deleted_count)
    return deleted_count


# ═══════════════════════════════════════════════════════════════════════
# 成本熔断
# ═══════════════════════════════════════════════════════════════════════

def check_cost_over_budget() -> Optional[str]:
    """check_cost_over_budget — 检查当日 LLM 花销是否超预算。

    作用：
      防止费用失控。当日花费超过 daily_budget 的 COST_OVER_BUDGET_RATIO 倍
      时返回警告。支持只读模式（cost_tracker.status = readonly）。

    原理：
      从 state.json 的 daily_budget 字段读取当前花费和预算上限。
      如果超限，协调者在 cronjob prompt 中获取此警告并停止 LLM 调用。

    逻辑：
      1. 加载 state.json
      2. 从 daily_budget 读 dollar_spent_today 和 dollar_limit
      3. 如果 dollar_limit <= 0：跳过检查
      4. 计算 ratio = dollar_spent_today / dollar_limit
      5. 如果 ratio >= COST_OVER_BUDGET_RATIO：返回 "只读模式：当日花费..."
      6. 更新 state.json 的 cost_tracker 状态

    Returns:
        str: 警告消息（只读模式时）。None: 正常。
    """
    state = load_state()
    budget = state.get("daily_budget", {})
    spent = budget.get("dollar_spent_today", 0)
    limit = budget.get("dollar_limit", 5.0)

    if limit <= 0:
        return None

    ratio = spent / limit
    if ratio >= COST_OVER_BUDGET_RATIO:
        msg = (
            f"只读模式：当日花费 ${spent:.2f}，超过预算 ${limit:.2f} 的 "
            f"{ratio:.1f} 倍（阈值 {COST_OVER_BUDGET_RATIO} 倍）。"
            "跳过 LLM 调用，仅更新 TODO。"
        )
        relog("💰", "%s", msg)
        state.setdefault("cost_tracker", {})["status"] = "readonly"
        save_state(state)
        return msg

    if ratio >= 1.0:
        relog("💰", "当日花费 ${spent:.2f}，已达预算 ${limit:.2f}")
    else:
        relog("💰", "成本正常：当日 $%.2f / $%.2f", spent, limit)

    state.setdefault("cost_tracker", {})["status"] = "normal"
    save_state(state)
    return None


# ═══════════════════════════════════════════════════════════════════════
# PID 文件管理（项8 - 自愈2）
# ═══════════════════════════════════════════════════════════════════════

def acquire_pid_file() -> bool:
    """acquire_pid_file — 获取 PID 文件锁，如遇僵尸文件则自动清理。

    作用：
      防止同一脚本被多个进程同时执行。通过文件系统级 PID 锁实现。

    原理：
      检查 PID 文件是否存在：
      - 不存在：写入当前 PID，获得锁
      - 存在但进程存活：说明有另一个实例在运行，退出
      - 存在但进程不存活（僵尸）：检查文件修改时间
        - 如果 > PID_ZOMBIE_TIMEOUT_SECONDS 秒前：自动删除并重新创建
        - 如果 <= PID_ZOMBIE_TIMEOUT_SECONDS 秒前：等它自行消失

    逻辑：
      1. 检查 PID_FILE 是否存在
      2. 如果存在：读取内容，用 os.kill(pid, 0) 检查进程是否存活
      3. 进程不存活：检查文件 mtime，超时则删除并重新创建
      4. 进程存活：打印错误，返回 False
      5. 写入当前 PID，返回 True

    返回值：
        True: 成功获取锁。False: 获取失败（有另一个实例在运行）。

    面试追问（项8 - 自愈2）：
    - 为什么不用 fcntl.flock？答：flock 基于打开的文件描述符，不防同一
      台机器上两个不同 shell 窗口；PID 文件 + 进程存活检查更可靠。
    - 2 分钟的超时怎么定的？答：常规脚本 1 分钟内执行完，2 分钟足够
      排除瞬态问题（如系统负载高导致脚本还没完全退出）。
    """
    if PID_FILE.exists():
        try:
            pid_str = PID_FILE.read_text().strip()
            pid = int(pid_str)
            # os.kill(pid, 0) 检查进程是否存活
            os.kill(pid, 0)
            # 进程存活
            relog("❌", "PID 文件 %s 存在，进程 %d 仍在运行", PID_FILE, pid)
            return False
        except (ValueError, ProcessLookupError):
            # PID 格式错误，或进程已死（僵尸）
            mtime = PID_FILE.stat().st_mtime
            age = time.time() - mtime
            if age > PID_ZOMBIE_TIMEOUT_SECONDS:
                relog("🛠️", "检测到僵尸 PID 文件（%.0f 秒前），自动删除并重建", age)
                PID_FILE.unlink(missing_ok=True)
            else:
                relog("⚠️", "PID 文件存在但进程已死（%.0f 秒前），等待超时自动清理", age)
                return False
        except OSError as e:
            relog("⚠️", "无法检查 PID %s: %s", pid_str, e)
            return False

    # 写入当前 PID
    PID_FILE.write_text(str(os.getpid()))
    relog("🔒", "获得 PID 锁（PID %d）", os.getpid())
    return True


def release_pid_file():
    """release_pid_file — 释放 PID 文件锁。

    作用：正常结束时清理 PID 文件，避免残留。
    原理：删除 PID_FILE 文件。
    逻辑：
      如果 PID_FILE 存在且内容为当前进程 PID，则删除。
    """
    if PID_FILE.exists():
        try:
            pid_str = PID_FILE.read_text().strip()
            if pid_str == str(os.getpid()):
                PID_FILE.unlink()
                relog("🔓", "释放 PID 锁")
        except (OSError, ValueError):
            pass


# ═══════════════════════════════════════════════════════════════════════
# state.json 管理
# ═══════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    """load_state — 加载 state.json，如果文件不存在则创建默认。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            relog("⚠️", "state.json 解析失败（%s），使用默认状态", e)
    return _default_state()


def save_state(state: dict):
    """save_state — 保存 state.json。"""
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _default_state() -> dict:
    """_default_state — 返回默认的 state.json 结构。"""
    return {
        "current_round": 0,
        "step": "init",
        "project_one_step": "init",
        "project_three_step": "init",
        "started_at": None,
        "completed_at": None,
        "last_error": None,
        "conflict_files": [],
        "retry_counts": {},
        "paused_due_to_conflict": False,
        "paused_due_to_error": False,
        "manual_intervention_needed": False,
        "cost_tracker": {"status": "normal", "estimated_tokens": 0, "actual_tokens": 0, "ratio": 0.0},
        "completed_task_ids": [],
        "in_progress_tasks": [],
        "permanently_failed": [],
        "blocked_tasks": [],
        "error_patterns": [],
        "failed_tasks": [],
        "blacklist_hits": [],
        "failure_stats": {},
        "daily_budget": {"dollar_limit": 5.0, "dollar_spent_today": 0.0, "date": "", "readonly_mode": False},
        "disk_status": {"available_mb": 0, "logs_dir_size_mb": 0, "warning": None},
        "recovery_attempted": False,
        "recovery_at": None,
    }


def update_step(step: str, error: Optional[str] = None):
    """update_step — 更新 state.json 中的当前步骤和错误。"""
    state = load_state()
    state["step"] = step
    if error:
        state["last_error"] = error
    save_state(state)


def mark_conflict(conflict_files: list[str]):
    """mark_conflict — 在 state.json 中标记冲突文件。"""
    state = load_state()
    state["conflict_files"] = conflict_files
    state["paused_due_to_conflict"] = True
    state["conflict_detected_at"] = datetime.now().isoformat()
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════
# 重试装饰器
# ═══════════════════════════════════════════════════════════════════════

def with_retry(fn, step_name: str = "unknown"):
    """with_retry — 带指数退避重试的函数包装器。

    作用：对可能失败的操作（如 git 命令、网络请求）做自动重试。
    原理：首次失败后等 5 秒，第二次等 10 秒，第三次等 20 秒。
    逻辑：
      1. 最多重试 RETRY_MAX_ATTEMPTS（3）次
      2. 如果 fn() 返回 (False, ...) 或抛出异常，则重试
      3. 重试间隔 = RETRY_INITIAL_DELAY * (2 ** attempt)
      4. 如果所有重试都失败，返回最后一次的结果
    """
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            result = fn()
            if isinstance(result, tuple) and len(result) >= 2 and not result[0]:
                relog("🔄", "%s 第 %d 次重试失败，第 %d 次...",
                      step_name, attempt + 1, attempt + 2)
                if attempt + 1 < RETRY_MAX_ATTEMPTS:
                    time.sleep(RETRY_INITIAL_DELAY * (2 ** attempt))
                continue
            return result
        except Exception as e:
            relog("🔄", "%s 异常（%s），第 %d 次重试...",
                  step_name, e, attempt + 2)
            if attempt + 1 < RETRY_MAX_ATTEMPTS:
                time.sleep(RETRY_INITIAL_DELAY * (2 ** attempt))
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════
# TODO.md 操作
# ═══════════════════════════════════════════════════════════════════════

TODO_FILE = SWARM_DIR / "TODO.md"


def read_todo_with_flock() -> str:
    """read_todo_with_flock — 加 fl 文件级互斥锁读 TODO.md。"""
    # 简化：当前无多进程并发写 TODO，直接读
    if TODO_FILE.exists():
        return TODO_FILE.read_text()
    return ""


def write_todo_with_flock(content: str):
    """write_todo_with_flock — 加 flock 写 TODO.md。"""
    TODO_FILE.write_text(content)


def read_todo_first_task() -> str:
    """read_todo_first_task — 从 TODO.md 中提取第一个待办任务的描述。

    作用：检查是否还有剩余任务，为成本熔断判断提供输入。
    原理：按行解析，跳过前缀注释/标题，取第一个 - [ ] 行。
    """
    content = read_todo_with_flock()
    for line in content.splitlines():
        stripped = line.strip()
        # 匹配未完成的任务项：- [ ] 或 - [TODO]
        if stripped.startswith("- [ ]") or stripped.startswith("- [TODO]"):
            return stripped
    return ""


# ═══════════════════════════════════════════════════════════════════════
# Git 操作
# ═══════════════════════════════════════════════════════════════════════

def git_pull_rebase(repo_dir: Path) -> tuple[bool, list[str]]:
    """git_pull_rebase — 在仓库目录执行 git pull --rebase。

    返回值：
        (success: bool, conflict_files: list[str])
    """
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
        if result.returncode != 0:
            conflict_files = _extract_conflict_files(result.stderr)
            if conflict_files:
                return False, conflict_files
            return False, []
        return True, []
    except subprocess.TimeoutExpired:
        return False, []


def _extract_conflict_files(stderr: str) -> list[str]:
    """_extract_conflict_files — 从 git stderr 中提取冲突文件名。"""
    files = []
    for line in stderr.splitlines():
        # git 冲突提示包含 "both modified:" 或在 "CONFLICT" 行
        if "CONFLICT" in line and "content" in line:
            # 格式: CONFLICT (content): Merge conflict in <file>
            parts = line.split("in ")
            if len(parts) >= 2:
                files.append(parts[-1].strip())
        if "both modified:" in line:
            parts = line.split("both modified:")
            if len(parts) >= 2:
                files.append(parts[-1].strip())
    return files


def run_git_commit(repo_dir: Path, message: str, skip_pull: bool = False) -> bool:
    """run_git_commit — 执行 git add + commit。

    Args:
        repo_dir:  git 仓库目录。
        message:   commit 消息。
        skip_pull: 是否跳过 pull --rebase。

    返回值：
        True: 提交成功（或工作区干净）。
        False: 提交失败。
    """
    # 先检查工作区状态
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=10,
        )
        if not status.stdout.strip():
            return True  # 工作区干净，无需提交
    except subprocess.TimeoutExpired:
        return False

    # pull --rebase ahead
    if not skip_pull:
        pull_ok, conflicts = git_pull_rebase(repo_dir)
        if conflicts:
            mark_conflict(conflicts)
            relog("❌", "拉取时出现冲突：%s", conflicts)
            return False
        if not pull_ok:
            relog("⚠️", "git pull --rebase 失败，继续尝试 commit")

    # add + commit
    try:
        subprocess.run(
            ["git", "add", "."],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=GIT_TIMEOUT,
            check=True,
        )
        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
        if commit_result.returncode == 0:
            relog("✅", "提交成功: %s", message)
            return True
        else:
            relog("⚠️", "commit 返回 %d: %s",
                  commit_result.returncode, commit_result.stderr.strip())
            return commit_result.returncode == 0
    except subprocess.TimeoutExpired:
        relog("❌", "git add/commit 超时（%ds）", GIT_TIMEOUT)
        return False


def run_git_commit_with_retry(repo_dir: Path, message: str,
                              repo_name: str = "swarm"):
    """run_git_commit_with_retry — 带重试机制的 git commit。"""
    def _do_commit():
        return run_git_commit(repo_dir, message)
    result = with_retry(_do_commit, f"git commit ({repo_name})")
    return result if result is not None else False


# ═══════════════════════════════════════════════════════════════════════
# 自愈函数（项8 - 自愈1）
# ═══════════════════════════════════════════════════════════════════════

def check_and_heal_conflicts() -> bool:
    """check_and_heal_conflicts — 检查 git 冲突并自动恢复。

    作用（项8 - 自愈1）：
      检测 state.json 中记录的冲突文件，如果冲突持续时间超过 1 小时，
      自动执行 git reset --hard HEAD 放弃冲突，确保系统可继续运行。

    原理：
      冲突持续太久说明无法自动合并，人工干预也长时间未发生。
      reset --hard HEAD 会放弃本地所有未提交变更（包括冲突标记），
      使工作区恢复到干净状态，下次 pull --rebase 即可正常同步。

    逻辑：
      1. 加载 state.json
      2. 检查 conflict_files 是否非空且有 conflict_detected_at 时间戳
      3. 计算冲突持续时间 = now - conflict_detected_at
      4. 如果 >= CONFLICT_TIMEOUT_SECONDS（3600 秒 = 1 小时）：
         a. 执行 git reset --hard HEAD
         b. 清空 state.conflict_files
         c. 清除 conflict_detected_at
         d. 设置 paused_due_to_conflict = False
         e. 记录日志
      5. 如果冲突存在但未超时，记录剩余等待时间

    Returns:
        True: 冲突已自动恢复（或没有冲突）。False: 冲突存在且尚未超时。
    """
    state = load_state()
    conflict_files = state.get("conflict_files", [])
    if not conflict_files:
        return True  # 没有冲突，无需恢复

    detected_at = state.get("conflict_detected_at")
    if not detected_at:
        # 没有记录检测时间，说明是旧状态，直接清理
        state["conflict_files"] = []
        state["paused_due_to_conflict"] = False
        save_state(state)
        return True

    try:
        detected_time = datetime.fromisoformat(detected_at)
        if detected_time.tzinfo is None:
            detected_time = detected_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed_seconds = (now - detected_time).total_seconds()
    except (ValueError, TypeError):
        # 时间格式异常，直接清理
        state["conflict_files"] = []
        state["paused_due_to_conflict"] = False
        save_state(state)
        return True

    if elapsed_seconds >= CONFLICT_TIMEOUT_SECONDS:
        relog("🛠️", "冲突已持续 %.0f 分钟，超过阈值（%.0f 分钟），执行 git reset --hard HEAD",
              elapsed_seconds / 60, CONFLICT_TIMEOUT_SECONDS / 60)
        try:
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=str(SWARM_DIR),
                capture_output=True, text=True, timeout=GIT_TIMEOUT,
                check=True,
            )
            relog("✅", "git reset --hard HEAD 成功")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            relog("❌", "git reset 失败: %s", e)
            return False

        state["conflict_files"] = []
        state["paused_due_to_conflict"] = False
        state.pop("conflict_detected_at", None)
        save_state(state)
        relog("🛠️", "冲突自愈完成，state.conflict_files 已清空")
        return True
    else:
        remaining = CONFLICT_TIMEOUT_SECONDS - elapsed_seconds
        relog("⏳", "冲突存在但未超时，剩余 %.0f 秒将自动 reset 解冲突", remaining)
        return False


# ─── 分层委托集成 ─────────────────────────────────────────────────────────
try:
    from delegate_optimizer import diagnose_failures, write_diagnosis_to_log
    DELEGATE_OPTIMIZER_AVAILABLE = True
except ImportError as e:
    DELEGATE_OPTIMIZER_AVAILABLE = False
    relog("⚠️", "delegate_optimizer 导入失败（%s），分层委托不可用", e)


def run_delegation_diagnosis() -> bool:
    """run_delegation_diagnosis — 运行子 Agent 失败模式诊断。

    每轮执行一次，分析 self_evolve_log.json 中所有 delegate 相关的条目，
    将诊断报告写入日志和 state.json。

    作用（项5 - 失败模式分析）：
      持续跟踪子 Agent 委托的成功率与失败归因，帮助协调者优化委托决策。

    Returns:
        True: 诊断完成。False: 诊断失败（模块不可用/日志缺失）。
    """
    if not DELEGATE_OPTIMIZER_AVAILABLE:
        relog("⚠️", "delegate_optimizer 不可用，跳过诊断")
        return False

    if not SELF_EVOLVE_LOG.exists():
        relog("⚠️", "self_evolve_log.json 不存在，跳过诊断")
        return False

    try:
        diagnosis = diagnose_failures()
        if "error" in diagnosis:
            relog("⚠️", "诊断失败: %s", diagnosis["error"])
            return False

        # 写入日志
        write_ok = write_diagnosis_to_log(diagnosis)
        if write_ok:
            relog("✅", "委托诊断完成：共 %d 轮，成功率 %.0f%%（委托 %d 次，成功率 %.0f%%）",
                  diagnosis.get("total_rounds", 0),
                  (diagnosis.get("overall_success_rate", 0) * 100),
                  diagnosis.get("delegated_rounds", 0),
                  (diagnosis.get("delegate_success_rate", 0) * 100)
                  )
        else:
            relog("⚠️", "诊断报告写入失败")

        # 更新到 state.json
        state = load_state()
        state["diagnosis"] = {
            "delegate_success_rate": diagnosis.get("delegate_success_rate", 0),
            "overall_success_rate": diagnosis.get("overall_success_rate", 0),
            "delegated_rounds": diagnosis.get("delegated_rounds", 0),
            "failure_patterns": diagnosis.get("failure_patterns", {}),
            "updated_at": __import__("datetime").datetime.now().isoformat(),
        }
        save_state(state)

        return True
    except Exception as e:
        relog("⚠️", "诊断异常: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════
# 失败样本收集（项5）
# ═══════════════════════════════════════════════════════════════════════

def append_failed_example(task_description: str, failure_type: str,
                          code_snippet: str):
    """append_failed_example — 将失败样本追加到 failed_examples.jsonl。

    Args:
        task_description: 任务描述（如 "为项目一的 xx 模块添加单元测试"）。
        failure_type:     失败类型（如 "SyntaxError", "ImportError", "TypeError"）。
        code_snippet:     失败时生成的代码片段。

    作用（项5）：
      每次 dev-cell 执行失败时，将 (任务描述, 失败类型, 生成的代码片段)
      存入 failed_examples.jsonl。failure_analysis.py 每周分析此文件，
      自动提取高频失败模式并生成修复规则。

    原理：
      JSON Lines 格式，每行一条 JSON 记录。末尾追加，无需锁（单进程写）。

    逻辑：
      1. 构造记录字典：{timestamp, task_description, failure_type, code_snippet}
      2. 以 JSON Lines 格式追加到 FAILED_EXAMPLES_FILE
    """
    try:
        record = {
            "timestamp": datetime.now().isoformat(),
            "task_description": task_description,
            "failure_type": failure_type,
            "code_snippet": code_snippet,
        }
        with open(FAILED_EXAMPLES_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        relog("📝", "失败样本已追加: %s (%s)", task_description[:40], failure_type)
    except OSError as e:
        relog("⚠️", "写入失败样本出错: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════════════════

def main():
    """main — 主入口。

    完整流程：
      1. 获取 PID 文件锁（含僵尸自动清理）
      2. 检查 g pu（含自动恢复）
      3. 检查磁盘空间（含自动日志清理）
      4. 检查成本熔断
      5. 如果是只读模式，更新 state 后退出
      6. 同步项目一（git pull --rebase + commit）
      7. 同步项目三（git pull --rebase + commit）
      8. 更新 state.json
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    relog("=" * 60, "")
    relog("后勤脚本启动 — %s", timestamp)

    # ── 0. PID 文件锁 ──
    if not acquire_pid_file():
        relog("⏭️", "另一个实例正在运行，退出")
        sys.exit(1)

    try:
        state = load_state()

        # ── 0a. 冲突自愈（项8 - 自愈1） ──
        check_and_heal_conflicts()

        # ── 1. 磁盘检查 + 日志清理（项8 - 自愈3） ──
        disk = check_disk_space()
        if disk.get("paused"):
            relog("⏸️", "磁盘空间不足，跳过本轮主要操作")

        # ── 2. 成本检查 ──
        cost_warning = check_cost_over_budget()
        if cost_warning:
            relog("⏸️", "成本超限，跳过 LLM 密集型操作")

        # ── 3. 检查项目一状态 ──
        if PROJECT1_DIR.exists():
            relog("📁", "检查项目一（%s）", PROJECT1_DIR)
            # 先 pull
            pull_ok, conflicts = git_pull_rebase(PROJECT1_DIR)
            if conflicts:
                mark_conflict(conflicts)
                relog("❌", "项目一冲突：%s", conflicts)
            elif not pull_ok:
                relog("⚠️", "项目一 git pull 失败")
            else:
                relog("✅", "项目一已同步")

            # 检查工作区
            try:
                status_p1 = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(PROJECT1_DIR),
                    capture_output=True, text=True, timeout=10,
                )
                if status_p1.stdout.strip():
                    lines = status_p1.stdout.strip().split("\n")
                    relog("⚠️", "项目一有 %d 个待提交文件", len(lines))
                    for line in lines:
                        relog("   ", "%s", line)
                    run_git_commit_with_retry(
                        PROJECT1_DIR,
                        f"项目一阶段进化 — {timestamp[:10]}",
                        repo_name="project1",
                    )
                else:
                    relog("✅", "项目一工作区干净")
            except subprocess.TimeoutExpired:
                relog("❌", "项目一 git status 超时")
        else:
            relog("⚠️", "项目一目录不存在: %s", PROJECT1_DIR)

        # ── 4. 项目三后勤 ──
        relog("📁", "检查项目三")
        pull_ok_swarm, conflicts_swarm = git_pull_rebase(SWARM_DIR)
        if conflicts_swarm:
            mark_conflict(conflicts_swarm)
            relog("❌", "项目三冲突：%s", conflicts_swarm)
        elif not pull_ok_swarm:
            relog("⚠️", "项目三 git pull 失败")
        else:
            relog("✅", "项目三已同步")

        # 项目三工作区检查
        try:
            status_swarm = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(SWARM_DIR),
                capture_output=True, text=True, timeout=10,
            )
            if status_swarm.returncode != 0:
                relog("⚠️", "git status 失败，跳过项目三")
            elif status_swarm.stdout.strip():
                lines = status_swarm.stdout.strip().split("\n")
                relog("⚠️", "有 %d 个未提交文件", len(lines))
                for line in lines:
                    relog("   ", "%s", line)
                run_git_commit_with_retry(
                    SWARM_DIR,
                    f"swarm-evolve: 后勤同步 — {timestamp[:10]}",
                    repo_name="swarm",
                )
            else:
                relog("✅", "工作区干净")
        except subprocess.TimeoutExpired:
            relog("❌", "git status 超时（10s），跳过项目三同步")

        # ── 5. 分层委托诊断（每轮检查子 Agent 成功率） ──
        run_delegation_diagnosis()

        # ── 6. 更新 state.json ──
        state = load_state()
        state["step"] = "done"
        state["completed_at"] = timestamp
        if not state.get("started_at"):
            state["started_at"] = timestamp
        state["project_one_step"] = "done"
        state["project_three_step"] = "done"
        save_state(state)

        relog("")
        relog("提示：")
        relog("  - 主要 A→B→Git 由 Hermes cronjob 每30分钟自动执行")
        relog("  - self_evolve_round.py 做磁盘监控 + 日志轮转 + Git 后勤")
        relog("  - 如遇冲突，请手动解决后修改 state.json 恢复")

    finally:
        release_pid_file()

    relog("=" * 60, "")
    relog("后勤脚本完成 — %s", timestamp)


if __name__ == "__main__":
    main()
