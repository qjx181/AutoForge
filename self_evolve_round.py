#!/usr/bin/env python3
"""自进化循环 - Git 后勤脚本（带 PID 文件 + flock + git pull --rebase + 重试 + 冲突处理 + 磁盘监控 + 日志轮转 + 成本熔断）。

作用：
  Hermes cronjob 做主要 A→B→Git 调度，本脚本作为后勤兜底：
    1. 用 PID 文件防止重叠执行
    2. 用 fcntl.flock 保护 TODO.md 并发修改
    3. git pull --rebase 后再 commit，避免冲突
    4. 冲突发生时自动中止 rebase、标记冲突文件、暂停流程、等待人工
    5. 对 Git/IO 瞬态失败做指数退避重试（1s, 2s, 4s）
    6. 每步有绝对超时（timeout 参数），防止卡死
    7. 读写 state.json 追踪每步完成状态
    8. 检查磁盘可用空间，低于 500MB 告警、低于 100MB 暂停 Git
    9. 监控日志目录大小，超过 500MB 自动轮转
   10. 检查成本异常（actual > 2x estimated），记录到日志

为什么这样设计：
  - PID 文件：系统 cron 和 Hermes cron 可能重叠调用，需要互斥
  - flock：self_evolve_round.py 和 Hermes cronjob 可能同时改 TODO.md
  - git pull --rebase：多人/多 Agent 协作时，先同步再提交
  - 冲突中止：自动合并可能破坏代码，宁停勿乱
  - 重试：网络抖动/API 限流是高频故障，重试可自动恢复
  - 磁盘监控：日志文件持续膨胀会导致磁盘满，造成 Git 操作失败
  - 成本熔断：防止单任务无限消耗 API token

面试可能追问：
  - Q: 为什么用 shutil.disk_usage 而非 df 命令？ A: df 需解析文本输出，shutil 是 Python 原生接口，跨平台一致。
  - Q: 日志轮转为什么删最旧 3 个而不是压缩？ A: 这些是 git 已提交的日志，压缩后再删的成本高于直接删。如果需归档，增加归档步骤。
  - Q: 成本熔断的 2x 阈值怎么定的？ A: 经验值。任务描述和输出通常正相关，2x 说明 Agent 偏离了方向。
"""

import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── 日志配置 ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | s_evolve | %(message)s",
)
logger = logging.getLogger("self_evolve_round")

# ─── 路径常量 ──────────────────────────────────────────────────────────
PROJECT_ONE = Path("/mnt/c/Users/qjx/Desktop/agent-自进化版/项目一cursor版本/在线部分")
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
TODO_PATH = SWARM_DIR / "TODO.md"
CHANGELOG_PATH = SWARM_DIR / "CHANGELOG.md"
STATE_PATH = SWARM_DIR / "state.json"
LOGS_DIR = SWARM_DIR / "logs"
PID_FILE = Path("/tmp/swarm_evolve.pid")

# ─── 超时常量（秒）─
GIT_TIMEOUT = 120       # 单次 git 操作
STATE_TIMEOUT = 10      # state.json 读写
PULL_TIMEOUT = 60       # git pull --rebase
FILE_IO_TIMEOUT = 30    # TODO/CHANGELOG 读写

# ─── 重试参数 ──────────────────────────────────────────────────────────
RETRY_DELAYS = [1, 2, 4]  # 第1次重试等1s, 第2次等2s, 第3次等4s
MAX_RETRIES = len(RETRY_DELAYS)

# ─── 磁盘监控参数 ──────────────────────────────────────────────────────
LOG_DIR_MAX_MB = 500      # 日志目录超过此大小触发轮转
LOGS_TO_DELETE = 3        # 每次轮转删除最旧的日志文件数
DISK_WARN_MB = 500        # 可用空间低于此值记录警告
DISK_PAUSE_MB = 100       # 可用空间低于此值暂停 Git 操作

# ─── 成本熔断参数 ──────────────────────────────────────────────────────
COST_OVER_BUDGET_RATIO = 2.0  # actual/estimated > 2 → 成本异常


# ═══════════════════════════════════════════════════════════════════════
# 第0层：公共工具
# ═══════════════════════════════════════════════════════════════════════

def relog(emoji: str, msg: str, *args):
    """带 emoji 的 INFO 日志（统一格式）。"""
    logger.info("%s %s", emoji, msg, *args)


# ═══════════════════════════════════════════════════════════════════════
# 第a层：磁盘监控 + 日志轮转
# ═══════════════════════════════════════════════════════════════════════

def check_disk_space() -> dict:
    """检查磁盘可用空间，更新 state.json 的 disk_status 字段。

    检查两个位置：项目一所在盘（/mnt/c）和 swarm 所在盘（/mnt/f）。

    返回值:
        {"available_mb": int, "warning": str | None}

    为什么检查两个盘：
      - /mnt/c 是 Windows C 盘，空间通常充足但 Docker 镜像/临时文件可能占满
      - /mnt/f 是项目三所在盘，日志文件持续写入，风险更高

    面试追问：
      - Q: 为什么不用 df -h？ A: Python 解析文本脆弱。shutil.disk_usage() 返回 os.statvfs
        的统计值，精确到字节，且不依赖外部命令。
      - Q: 告警阈值如何确定？ A: 500MB 告警等于给用户 5 轮（每轮 100MB）的反应时间。
        100MB 暂停则 Git 操作不会因 ENOSPC 导致仓库损坏。
    """
    try:
        usage_f = shutil.disk_usage("/mnt/f")
        # 取两个盘中最小的可用空间作为实际约束
        available_mb = usage_f.free // (1024 * 1024)

        state = load_state()
        disk = state.setdefault("disk_status", {})
        disk["available_mb"] = available_mb

        if available_mb < DISK_PAUSE_MB:
            disk["warning"] = "low_disk"
            relog("⛔", "磁盘可用空间仅剩 %d MB，低于 %d MB 暂停阈值！",
                  available_mb, DISK_PAUSE_MB)
        elif available_mb < DISK_WARN_MB:
            disk["warning"] = "low_disk"
            relog("⚠️", "磁盘可用空间: %d MB（低于警告线 %d MB）",
                  available_mb, DISK_WARN_MB)
        else:
            disk["warning"] = None
            relog("✅", "磁盘可用空间: %d MB（正常）", available_mb)

        save_state(state)
        return {"available_mb": available_mb,
                "warning": disk.get("warning")}
    except Exception as e:
        relog("⚠️", "磁盘检查失败: %s", e)
        return {"available_mb": -1, "warning": "check_failed"}


def rotate_logs() -> bool:
    """检查日志目录总大小，超过 LOG_DIR_MAX_MB 则删除最旧的 LOGS_TO_DELETE 个文件。

    返回值: True = 已轮转 / 无需轮转；False = 目录不存在或无文件可删

    为什么不压缩归档：
      日志文件每次提交后已 git 版本化，删除已提交的文件不会丢失。
      Compress-then-delete 增加复杂度但收益有限。

    面试追问：
      - Q: 怎么定义"最旧"？ A: 用 os.path.getmtime()（最后修改时间），
        也就是文件的 mtime。旧 cron 日志不会再被修改，mtime 等于创建时间。
      - Q: 为什么不使用 logrotate 系统工具？ A: 本项目日志写入不受系统
        logrotate 控制（Python logging 直接写文件），且用户环境权限受限。
        自实现更可控。
      - Q: 并发安全吗？ A: 本函数有 PID 文件互斥保护，同一时间只有一个进程执行。
    """
    if not LOGS_DIR.exists():
        relog("⚠️", "日志目录不存在，跳过轮转")
        return False

    # 计算日志目录总大小
    total_size = 0
    log_files = []
    for f in LOGS_DIR.iterdir():
        if f.is_file() and not f.name.startswith("."):
            try:
                size = f.stat().st_size
                total_size += size
                log_files.append((f, f.stat().st_mtime))
            except OSError:
                continue

    total_mb = total_size / (1024 * 1024)
    relog("📊", "日志目录大小: %.1f MB / %d MB", total_mb, LOG_DIR_MAX_MB)

    # 更新 state.json
    state = load_state()
    state.setdefault("disk_status", {})["logs_dir_size_mb"] = round(total_mb, 1)
    save_state(state)

    if total_mb <= LOG_DIR_MAX_MB:
        return True  # 无需轮转

    # 按 mtime 升序排列（最旧在前）
    log_files.sort(key=lambda x: x[1])

    deleted = 0
    for f, _ in log_files[:LOGS_TO_DELETE]:
        try:
            f.unlink()
            relog("🗑️", "删除旧日志: %s (%.1f KB)", f.name, f.stat().st_size / 1024)
            deleted += 1
        except OSError as e:
            relog("⚠️", "删除日志失败 %s: %s", f.name, e)

    if deleted > 0:
        relog("✅", "日志轮转完成: 删除了 %d 个文件", deleted)
    else:
        relog("⚠️", "日志轮转尝试失败（没有可删除的文件）")

    return True


# ═══════════════════════════════════════════════════════════════════════
# 第b层：成本熔断检查
# ═══════════════════════════════════════════════════════════════════════

def check_cost_over_budget() -> Optional[str]:
    """读取 state.json 的 cost_tracker，检查是否有成本异常的任务。

    返回值: None（无异常）| str（异常描述，格式如"build_ragas_evaluator: 实际6000/预估3000，超支2.0x"）

    为什么 separate 出此函数：
      本脚本在 Hermes cronjob 之前运行，如果 cost_tracker 有异常状态，
      后勤日志会体现异常，提醒用户注意。

    面试追问：
      - Q: 为什么不在这里自动熔断？ A：熔断是协调者的职责（决定该任务是否
        继续派发）。本脚本只负责后勤和日志报告，不干预决策流程。
      - Q: ratio 怎么算的？ A: actual_tokens / estimated_tokens。
        actual_tokens 由 A 队子 Agent 在 report.json 中报告已生成代码的字符数，
        协调者在 Phase 3 更新到此字段。
    """
    state = load_state()
    cost = state.get("cost_tracker", {})
    status = cost.get("status", "normal")

    if status == "over_budget" or status == "terminated":
        task_id = cost.get("current_task_id", "?")
        estimated = cost.get("estimated_tokens", 0)
        actual = cost.get("actual_tokens", 0)
        ratio = cost.get("ratio", 0.0)
        msg = (f"{task_id}: 实际{actual}/预估{estimated}，"
               f"超支{ratio:.1f}x（状态: {status}）")
        relog("💰", "成本异常: %s", msg)
        return msg

    return None


# ═══════════════════════════════════════════════════════════════════════
# 第1层：PID 文件 — 防重叠执行
# ═══════════════════════════════════════════════════════════════════════

def acquire_pid_file() -> bool:
    """获取 PID 文件锁。已存在且进程存活 → 返回 False（不执行）；
    已存在但进程死 → 清理并创建新文件。
    """
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # 检查进程是否存在
            relog("⛔", "前一轮进程 (PID=%d) 仍在运行，跳过本轮", pid)
            return False
        except (ProcessLookupError, OSError):
            relog("→", "前一轮进程已退出，清理 stale PID 文件")
            PID_FILE.unlink(missing_ok=True)
        except ValueError:
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    relog("✅", "PID 文件已创建: PID=%d", os.getpid())
    return True


def release_pid_file():
    """释放 PID 文件。"""
    try:
        PID_FILE.unlink(missing_ok=True)
        relog("✅", "PID 文件已释放")
    except Exception as e:
        relog("⚠️", "PID 文件释放失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 第2层：state.json — 步骤追踪 + 冲突标记 + 暂停状态 + 磁盘/成本状态
# ═══════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    """加载 state.json，失败时返回默认空状态。"""
    if not STATE_PATH.exists():
        return _default_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        relog("⚠️", "state.json 读取失败: %s，返回默认状态", e)
        return _default_state()


def save_state(state: dict):
    """原子写入 state.json。"""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _default_state() -> dict:
    return {
        "current_round": 0, "step": "idle",
        "project_one_step": "idle", "project_three_step": "idle",
        "started_at": None, "completed_at": None,
        "last_error": None, "conflict_files": [],
        "retry_counts": {},
        "paused_due_to_conflict": False,
        "paused_due_to_error": False,
        "manual_intervention_needed": False,
        "cost_tracker": {
            "current_task_id": None,
            "estimated_tokens": 0,
            "actual_tokens": 0,
            "ratio": 0.0,
            "status": "normal",
        },
        "completed_task_ids": [],
        "disk_status": {
            "available_mb": 0,
            "logs_dir_size_mb": 0,
            "warning": None,
        },
    }


def update_step(step: str, error: Optional[str] = None):
    """更新 state.json 的当前步骤。"""
    state = load_state()
    state["step"] = step
    if error:
        state["last_error"] = error
        state["paused_due_to_error"] = True
    save_state(state)


def mark_conflict(conflict_files: list[str]):
    """标记冲突，暂停流程。"""
    state = load_state()
    state["step"] = "conflict"
    state["conflict_files"] = list(set(state.get("conflict_files", []) + conflict_files))
    state["paused_due_to_conflict"] = True
    state["manual_intervention_needed"] = True
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════
# 第3层：指数退避重试
# ═══════════════════════════════════════════════════════════════════════

def with_retry(fn, step_name: str = "unknown"):
    """执行 fn()，失败时以 1s-2s-4s 间隔重试，最多 3 次重试。
    每次重试前更新 state.json 的 retry_counts。
    """
    last_exception = None
    for attempt in range(MAX_RETRIES + 1):  # 首次 + 3次重试
        try:
            return fn()
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                OSError, json.JSONDecodeError) as e:
            last_exception = e
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                relog("⚠️", "[%s] 第%d次失败: %s，%ds后重试...",
                      step_name, attempt + 1, e, delay)
                state = load_state()
                retries = state.setdefault("retry_counts", {})
                retries[step_name] = attempt + 1
                save_state(state)
                time.sleep(delay)
            else:
                relog("❌", "[%s] 重试%d次后仍失败: %s",
                      step_name, MAX_RETRIES, e)
    raise last_exception  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# 第4层：fcntl.flock — TODO.md 并发保护
# ═══════════════════════════════════════════════════════════════════════

def read_todo_with_flock() -> str:
    """用共享锁读 TODO.md，防止同时写时读到脏数据。"""
    if not TODO_PATH.exists():
        return ""
    with open(TODO_PATH, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        content = f.read()
        fcntl.flock(f, fcntl.LOCK_UN)
    return content


def write_todo_with_flock(content: str):
    """用排他锁写 TODO.md，保证只有一个进程在修改。"""
    with open(TODO_PATH, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)


def read_todo_first_task() -> str:
    """读取 TODO.md 的第一条未完成任务（带 flock 保护）。"""
    try:
        content = read_todo_with_flock()
        if not content:
            return "TODO.md 不存在或为空"
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- [ ] "):
                return stripped.replace("- [ ] ", "").strip()
        return "所有待办已完成"
    except Exception as e:
        return f"读取失败: {e}"


# ═══════════════════════════════════════════════════════════════════════
# 第5层：Git 操作（pull --rebase + commit + 冲突检测）
# ═══════════════════════════════════════════════════════════════════════

def git_pull_rebase(repo_dir: Path) -> tuple[bool, list[str]]:
    """git pull --rebase，成功返回 (True, [])；
    冲突返回 (False, [冲突文件列表]) 且已执行 git rebase --abort。
    """
    result = subprocess.run(
        ["git", "pull", "--rebase"],
        cwd=str(repo_dir),
        capture_output=True, text=True,
        timeout=PULL_TIMEOUT,
    )
    output = result.stdout + result.stderr

    if result.returncode == 0:
        return True, []

    # 检测冲突
    conflict_files = []
    for line in output.split("\n"):
        if "CONFLICT" in line and "content" in line:
            parts = line.split(" in ")
            if len(parts) > 1:
                conflict_files.append(parts[-1].strip())

    if conflict_files:
        relog("❌", "检测到 Git 冲突！冲突文件: %s", conflict_files)
        # 中止 rebase，恢复干净状态
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(repo_dir),
            capture_output=True, timeout=30,
        )
        return False, conflict_files

    # 其他错误（网络、权限等）
    relog("⚠️", "git pull 失败 (rc=%d): %s", result.returncode, output[:300])
    return False, []


def run_git_commit(repo_dir: Path, message: str, skip_pull: bool = False) -> bool:
    """带 git pull --rebase + 冲突检测 + 重试的 commit 函数。"""
    if not skip_pull:
        relog("→", "git pull --rebase...")
        ok, conflict_files = git_pull_rebase(repo_dir)
        if not ok and conflict_files:
            mark_conflict(conflict_files)
            relog("⛔", "因 Git 冲突暂停流程，等待人工介入")
            return False
        if not ok:
            relog("⚠️", "git pull 非冲突失败，仍尝试直接 commit（可能落后 remote）")

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(repo_dir), check=True, capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.CalledProcessError as e:
        relog("❌", "git add 失败: %s", e)
        raise

    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_dir), capture_output=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode == 0:
        relog("→", "无变更，跳过 commit")
        return True

    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_dir),
        capture_output=True, text=True,
        timeout=GIT_TIMEOUT,
    )
    if commit.returncode == 0:
        relog("✅", "commit 成功: %s", commit.stdout.strip()[:120])
        return True
    else:
        relog("⚠️", "commit 失败: %s", commit.stderr.strip()[:200])
        return False


def run_git_commit_with_retry(repo_dir: Path, message: str,
                              repo_name: str = "project") -> bool:
    """包装 run_git_commit，加 3 次重试（1s-2s-4s）。"""
    def _do():
        return run_git_commit(repo_dir, message)
    try:
        return with_retry(_do, step_name=f"git_commit_{repo_name}")
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════

def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    relog("=" * 60, "")
    relog("后勤脚本启动 — %s", timestamp)
    relog("=" * 60, "")

    # ── 0. PID 文件检查（防重叠）─
    if not acquire_pid_file():
        relog("本轮跳过（前一轮未结束）")
        return

    try:
        # ── 1. 检查暂停状态 ──
        state = load_state()
        if state.get("paused_due_to_conflict"):
            relog("⛔", "流程因 Git 冲突暂停，冲突文件: %s",
                  state.get("conflict_files", []))
            relog("   ", "请手动解决冲突后，将 state.json 中 'paused_due_to_conflict' 设为 false")
            return
        if state.get("manual_intervention_needed"):
            relog("⛔", "流程因等待人工介入而暂停")
            return

        # ── 2. 磁盘监控 + 日志轮转（在 Git 操作之前执行）─
        relog("=" * 30, "")
        relog("磁盘检查 + 日志轮转:")
        relog("=" * 30, "")
        disk = check_disk_space()
        rotate_logs()

        # 磁盘空间太低时跳过 Git 操作
        skip_git = False
        if disk.get("available_mb", 9999) < DISK_PAUSE_MB:
            relog("⛔", "磁盘可用空间 %d MB < %d MB，安全暂停 → 跳过 Git 操作",
                  disk.get("available_mb", 0), DISK_PAUSE_MB)
            skip_git = True
        elif disk.get("warning") == "low_disk":
            relog("⚠️", "磁盘空间低 (%d MB)，Git 操作仍尝试但风险较高",
                  disk.get("available_mb", 0))

        # ── 3. 成本异常检查 ──
        cost_warning = check_cost_over_budget()
        if cost_warning:
            relog("💰", "成本异常（仅记录，不暂停 Git）: %s", cost_warning)

        # ── 4. 读取当前待办（带 flock 保护）─
        update_step("reading_status")
        current_task = read_todo_first_task()
        relog("当前待办: %s", current_task)

        # ── 5. 项目一 Git 后勤（skip if disk too low）─
        if skip_git:
            relog("⛔", "磁盘空间不足，跳过项目一 Git 后勤")
        else:
            update_step("project_one_sync")
            relog("=" * 30, "")
            relog("项目一 Git 后勤:")
            relog("=" * 30, "")
            try:
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(PROJECT_ONE),
                    capture_output=True, text=True,
                    timeout=10,
                )
                if status.returncode != 0:
                    relog("⚠️", "git status 失败，跳过项目一")
                elif status.stdout.strip():
                    lines = status.stdout.strip().split("\n")
                    relog("⚠️", "有 %d 个未提交文件", len(lines))
                    for line in lines:
                        relog("   ", "%s", line)
                    run_git_commit_with_retry(
                        PROJECT_ONE,
                        f"swarm-evolve: 后勤自动提交 — {timestamp[:10]}",
                        repo_name="project_one",
                    )
                else:
                    relog("✅", "工作区干净")
            except subprocess.TimeoutExpired:
                relog("❌", "git status 超时（10s），跳过项目一同步")

        # ── 6. 项目三 Git 后勤（skip if disk too low）─
        if skip_git:
            relog("⛔", "磁盘空间不足，跳过项目三 Git 后勤")
        else:
            update_step("project_three_sync")
            relog("=" * 30, "")
            relog("项目三（Swarm）Git 后勤:")
            relog("=" * 30, "")
            try:
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(SWARM_DIR),
                    capture_output=True, text=True,
                    timeout=10,
                )
                if status.returncode != 0:
                    relog("⚠️", "git status 失败，跳过项目三")
                elif status.stdout.strip():
                    lines = status.stdout.strip().split("\n")
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

        # ── 7. 更新 state.json 完成状态 ──
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
