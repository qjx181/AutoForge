#!/usr/bin/env python3
"""self_evolve_round.py — 项目三自进化后勤脚本

职责（每 30 分钟由 cronjob 触发）：
  1. PID 文件锁 + 冲突自愈
  2. 磁盘空间检查 + 日志轮转
  3. 成本熔断检查
  4. 项目一同步（git pull + commit）
  5. 项目三同步（git pull + commit）
  6. 分层委托诊断 + 强制委托检查
  7. ⬆️ 并行任务规划（新） — 扫描 pending_tasks → 按依赖分组 → 并行派发计划
  8. 更新 state.json

注意：
  实际的任务执行（write_file / delegate_task）由 Hermes Agent cronjob 的 prompt 驱动。
  本脚本只做"后勤 + 规划"——打扫战场、生成执行计划。
"""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

# ─── 路径 ──────────────────────────────────────────────────────────────
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
PROJECT1_DIR = Path("/mnt/c/Users/qjx/Desktop/agent-自进化版/项目一cursor版本/在线部分")
STATE_FILE = SWARM_DIR / "state.json"
PID_FILE = SWARM_DIR / ".self_evolve_round.pid"
TODO_FILE = SWARM_DIR / "TODO.md"
LOG_FILE = SWARM_DIR / "logs" / "self_evolve.log"

# ─── Git ───────────────────────────────────────────────────────────────
GIT_TIMEOUT = 60  # git 命令超时（秒）

# ─── 磁盘阈值 ──────────────────────────────────────────────────────────
MIN_FREE_GB = 5
MAX_LOG_DAYS = 7

# ─── 日志 ──────────────────────────────────────────────────────────────


def relog(tag: str, *args) -> None:
    """简易日志输出（控制台 + 文件）。"""
    msg = f"[{datetime.now().strftime('%H:%M:%S')}] {tag}" + (" " + " ".join(str(a) for a in args) if args else "")
    print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


# ═══════════════════════════════════════════════════════════════════════
# 0. PID 文件锁
# ═══════════════════════════════════════════════════════════════════════


def acquire_pid_file() -> bool:
    """获取 PID 文件锁（含僵尸自动清理 5 分钟超时）。"""
    if not HAS_FCNTL:
        return True  # 非 Linux 跳过
    try:
        pid_fd = PID_FILE.open("w")
        fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_fd.write(str(os.getpid()))
        pid_fd.flush()
        return True
    except (IOError, BlockingIOError):
        # 检查是否僵尸（旧进程超过 5 分钟）
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                try:
                    os.kill(old_pid, 0)  # 检查进程是否存在
                    relog("⚠️", "PID 文件锁被占用（pid=%d），跳过", old_pid)
                    return False
                except OSError:
                    # 进程不存在，清理僵尸锁
                    relog("🧟", "清理僵尸 PID 锁（pid=%d）", old_pid)
                    PID_FILE.unlink(missing_ok=True)
                    return acquire_pid_file()
            except (ValueError, OSError):
                PID_FILE.unlink(missing_ok=True)
                return acquire_pid_file()
        return False


def release_pid_file():
    """释放 PID 文件锁。"""
    if HAS_FCNTL and PID_FILE.exists():
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# 1. 公共工具 / state 读写
# ═══════════════════════════════════════════════════════════════════════


def load_state() -> dict:
    """加载 state.json。"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    """保存 state.json（原子写入）。"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


# ═══════════════════════════════════════════════════════════════════════
# 2. 磁盘检查 + 日志清理
# ═══════════════════════════════════════════════════════════════════════


def check_disk_space() -> dict:
    """检查磁盘空间，自动清理 7 天前的日志。"""
    try:
        stat = os.statvfs(str(SWARM_DIR))
        free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
        relog("💾", "磁盘剩余 %.1f GB / 阈值 %d GB", free_gb, MIN_FREE_GB)

        if free_gb < MIN_FREE_GB:
            relog("⚠️", "磁盘不足，清理 7 天前的日志文件")
            cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
            log_dir = SWARM_DIR / "logs"
            if log_dir.exists():
                cleaned = 0
                for f in log_dir.iterdir():
                    if f.is_file():
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        if mtime < cutoff:
                            f.unlink()
                            cleaned += 1
                relog("🧹", "清理了 %d 个旧日志文件", cleaned)

            # 再检查一次
            stat = os.statvfs(str(SWARM_DIR))
            free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
            if free_gb < MIN_FREE_GB:
                relog("⏸️", "清理后磁盘仍不足（%.1f GB），标记暂停", free_gb)
                return {"free_gb": free_gb, "paused": True}

        return {"free_gb": free_gb, "paused": False}
    except Exception as e:
        relog("❌", "磁盘检查失败: %s", e)
        return {"free_gb": -1, "paused": False}


# ═══════════════════════════════════════════════════════════════════════
# 3. 成本熔断检查
# ═══════════════════════════════════════════════════════════════════════


def check_cost_over_budget() -> Optional[str]:
    """检查当日 API 花费是否超预算。"""
    state = load_state()
    budget = state.get("daily_budget", {})
    dollar_spent = budget.get("dollar_spent_today", 0)
    dollar_limit = budget.get("dollar_limit", 5.0)

    if dollar_spent >= dollar_limit * 0.9:
        warning = f"当日花费 ${dollar_spent:.2f} / 限额 ${dollar_limit:.2f}，接近橙色模式"
        relog("💰", warning)
        return warning

    relog("💰", "当日花费 $%.2f / $%.2f", dollar_spent, dollar_limit)
    return None


# ═══════════════════════════════════════════════════════════════════════
# 4. Git 工具
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
# 5. 冲突自愈
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
# 6. 分层委托诊断
# ═══════════════════════════════════════════════════════════════════════


def run_delegation_diagnosis():
    """从 self_evolve_log.json 分析委托成功率，写入 state.json。

    诊断指标：
      - delegate_success_rate: 委托成功率
      - overall_success_rate: 总成功率
      - delegated_rounds: 包含委托的轮次数
      - failure_patterns: 失败类型统计
    """
    log_path = SWARM_DIR / "self_evolve_log.json"
    if not log_path.exists():
        return

    try:
        log_data = json.loads(log_path.read_text())
        entries = log_data if isinstance(log_data, list) else log_data.get("entries", [])

        total_rounds = len(entries)
        total_delegated = 0
        success_delegated = 0
        failure_patterns: dict[str, int] = {}

        for entry in entries:
            approach = (entry.get("approach", "") or "").lower()
            result = entry.get("result", "")

            if "delegate" in approach:
                total_delegated += 1
                if result == "success":
                    success_delegated += 1

                # 分析 waste 字段中的失败模式
                waste = entry.get("waste", "")
                if "delegate" in waste.lower():
                    # 提取失败模式关键词
                    for pattern in ["environment", "mock_import", "zero_file", "import", "dependency"]:
                        if pattern in waste.lower():
                            failure_patterns[pattern] = failure_patterns.get(pattern, 0) + 1

        diagnosis = {
            "delegate_success_rate": round(success_delegated / total_delegated, 2) if total_delegated else 1.0,
            "overall_success_rate": round(sum(1 for e in entries if e.get("result") == "success") / total_rounds, 2) if total_rounds else 1.0,
            "delegated_rounds": total_delegated,
            "failure_patterns": failure_patterns,
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        state = load_state()
        state["diagnosis"] = diagnosis
        save_state(state)
        relog("📊", "委托诊断完成 — 成功率 %.0f%% / %d 轮", diagnosis["delegate_success_rate"] * 100, total_delegated)

    except (json.JSONDecodeError, KeyError) as e:
        relog("⚠️", "委托诊断失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 6b. 强制委托检查
# ═══════════════════════════════════════════════════════════════════════


def check_forced_delegation():
    """强制委托检查——每轮确认是否有可委托的任务。

    从 self_evolve_log.json 最新一轮统计 delegate 使用情况。
    如果连续多轮零委托，在日志中发出警告。
    """
    log_path = SWARM_DIR / "self_evolve_log.json"
    if not log_path.exists():
        return

    try:
        log_data = json.loads(log_path.read_text())
        entries = log_data if isinstance(log_data, list) else log_data.get("entries", [])

        # 统计最近 5 轮中委托次数
        recent = entries[-5:]
        delegate_count = sum(
            1 for e in recent
            if "delegate" in (e.get("approach", "") or "").lower()
        )

        if delegate_count == 0 and len(recent) >= 3:
            relog("⚠️", "强制委托检查: 最近 %d 轮零委托，建议每轮至少委托 1 个任务", len(recent))
        elif delegate_count == 0:
            relog("📊", "强制委托检查: 最近 %d 轮无委托（轮次不足，继续观察）", len(recent))
        else:
            relog("✅", "强制委托检查: 最近 %d 轮委托 %d 次", len(recent), delegate_count)

    except (json.JSONDecodeError, KeyError) as e:
        relog("⚠️", "强制委托检查失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 7. ⬆️ 并行任务规划（新） — 集成 parallel_dispatcher
# ═══════════════════════════════════════════════════════════════════════


def _load_parallel_dispatcher():
    """尝试从工作目录加载 parallel_dispatcher 模块。"""
    sys.path.insert(0, str(SWARM_DIR))
    try:
        import parallel_dispatcher
        return parallel_dispatcher
    except ImportError as e:
        relog("⚠️", "parallel_dispatcher 加载失败: %s", e)
        return None


def _parse_todo_dependencies() -> dict[str, dict]:
    """从 TODO.md 解析所有待办任务的依赖和 token 估算。

    解析格式：
      - [ ] 任务ID: <id>
        描述: ...
        依赖: <dep1>, <dep2>, ... | 依赖: 无 | 无这行 → 无依赖
        预估 token 量: <number>

    Returns:
        {task_id: {"depends": [str], "token_est": int, "description": str}}
    """
    if not TODO_FILE.exists():
        return {}

    text = TODO_FILE.read_text()
    tasks: dict[str, dict] = {}
    current_id: Optional[str] = None
    current_dep: list[str] = []
    current_token: int = 2000
    current_desc: str = ""

    for line in text.splitlines():
        # 匹配任务ID
        m = re.match(r'^- \[ \] 任务ID:\s*(\S+)', line)
        if m:
            # 保存前一个任务
            if current_id:
                tasks[current_id] = {
                    "depends": current_dep,
                    "token_est": current_token,
                    "description": current_desc,
                }
            current_id = m.group(1)
            current_dep = []
            current_token = 2000
            current_desc = ""
            continue

        if current_id:
            # 解析描述
            dm = re.match(r'\s+描述:\s*(.+)', line)
            if dm:
                current_desc = dm.group(1).strip()
                continue

            # 解析依赖
            dm = re.match(r'\s+依赖:\s*(.+)', line)
            if dm:
                dep_text = dm.group(1).strip()
                if dep_text and dep_text != "无" and not dep_text.startswith("无（"):
                    # 可能含逗号分隔的多个依赖
                    current_dep = [d.strip() for d in dep_text.split(",") if d.strip()]
                continue

            # 解析 token 估算
            tm = re.match(r'\s+预估 token 量:\s*(\d+)', line)
            if tm:
                current_token = int(tm.group(1))
                continue

    # 保存最后一个任务
    if current_id:
        tasks[current_id] = {
            "depends": current_dep,
            "token_est": current_token,
            "description": current_desc,
        }

    return tasks


def plan_parallel_tasks() -> dict | None:
    """扫描 pending_tasks → 按依赖分组 → 编写并行计划 → 写入 state.json。

    流程：
      1. 加载 state.json，读取 pending_tasks 列表
      2. 从 TODO.md 解析每个任务的依赖关系和 token 估算
      3. 调用 parallel_dispatcher.dispatch_tasks() 生成执行计划
      4. 将计划写入 state.json 的 parallel_plan 字段
      5. 返回计划供主流程使用

    输出（写入 state.json "parallel_plan" 字段）：
      {
        "batches": [           # 批次列表，每批可并行执行
          [task_id, task_id],  # 第 1 批（无依赖，并行）
          [task_id],           # 第 2 批（依赖第 1 批）
          ...
        ],
        "coordinator": [...],  # 协调者自己干的任务
        "delegate": [...],     # 委托给子 Agent 的任务
        "max_concurrent": 3,
        "has_work": true|false
      }
    """
    state = load_state()
    pending_ids = state.get("pending_tasks", [])

    if not pending_ids:
        relog("📋", "并行规划: 无待办任务")
        if "parallel_plan" in state:
            del state["parallel_plan"]
            save_state(state)
        return None

    # 从 TODO.md 解析依赖信息
    todo_tasks = _parse_todo_dependencies()
    relog("📋", "TODO.md 解析: %d 个任务定义", len(todo_tasks))

    # 构建 parallel_dispatcher 需要的 todo_tasks 格式
    formulated_tasks: list[dict] = []
    for task_id in pending_ids:
        info = todo_tasks.get(task_id, {})
        formulated_tasks.append({
            "task_id": task_id,
            "depends": info.get("depends", []),
            "token_est": info.get("token_est", 2000),
            "description": info.get("description", ""),
        })

    relog("📋", "待规划任务: %d 项", len(formulated_tasks))

    # 明确标记依赖信息到 control 变量，供 dispatch 使用
    # 手动分组：无依赖的任务放一起
    independent = [t for t in formulated_tasks if not t["depends"]]
    dependent = [t for t in formulated_tasks if t["depends"]]
    # 进一步按依赖分组
    dep_groups: dict[str, list[dict]] = {}
    for t in dependent:
        key = ",".join(sorted(t["depends"]))
        dep_groups.setdefault(key, []).append(t)

    # 构建批次
    max_concurrent = 3
    batches: list[list[str]] = []

    # 第 1 批：所有无依赖任务（最多 3 个并行）
    if independent:
        b1 = [t["task_id"] for t in independent[:max_concurrent]]
        batches.append(b1)
        # 如果还有剩余，下一批
        remaining = [t["task_id"] for t in independent[max_concurrent:]]
        while remaining:
            batches.append(remaining[:max_concurrent])
            remaining = remaining[max_concurrent:]

    # 后续批次：有依赖的
    for group_tasks in dep_groups.values():
        group_ids = [t["task_id"] for t in group_tasks]
        while group_ids:
            batches.append(group_ids[:max_concurrent])
            group_ids = group_ids[max_concurrent:]

    # 打印计划概要
    relog("📋", "并行规划: %d 批, 并发上限 %d", len(batches), max_concurrent)
    for i, batch in enumerate(batches):
        relog("  🗂️  Batch %d: %s", i + 1, ", ".join(batch))

    plan = {
        "batches": batches,
        "coordinator": [t["task_id"] for t in formulated_tasks],
        "delegate": [],
        "max_concurrent": max_concurrent,
        "has_work": len(formulated_tasks) > 0,
        "planned_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_pending": len(formulated_tasks),
    }

    # 写入 state.json
    state["parallel_plan"] = plan
    save_state(state)
    relog("✅", "并行规划已写入 state.json")

    return plan


# ═══════════════════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════════════════


def main():
    """主入口。

    完整流程：
      1. 获取 PID 文件锁（含僵尸自动清理）
      2. 冲突自愈检查
      3. 磁盘空间检查 + 日志清理
      4. 成本熔断检查
      5. 项目一同步
      6. 项目三同步
      7. 分层委托诊断 + 强制委托检查
      8. ⬆️ 并行任务规划（新）
      9. 更新 state.json
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

        # ── 0a. 冲突自愈 ──
        check_and_heal_conflicts()

        # ── 1. 磁盘检查 + 日志清理 ──
        disk = check_disk_space()
        if disk.get("paused"):
            relog("⏸️", "磁盘空间不足，跳过本轮主要操作")

        # ── 2. 成本检查 ──
        cost_warning = check_cost_over_budget()
        if cost_warning:
            relog("⏸️", "成本超限，跳过 LLM 密集型操作")

        # ── 3. 项目一同步 ──
        if PROJECT1_DIR.exists():
            relog("📁", "检查项目一（%s）", PROJECT1_DIR)
            pull_ok, conflicts = git_pull_rebase(PROJECT1_DIR)
            if conflicts:
                mark_conflict(conflicts)
                relog("❌", "项目一冲突：%s", conflicts)
            elif not pull_ok:
                relog("⚠️", "项目一 git pull 失败")
            else:
                relog("✅", "项目一已同步")

            try:
                status_p1 = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(PROJECT1_DIR),
                    capture_output=True, text=True, timeout=10,
                )
                if status_p1.stdout.strip():
                    lines = status_p1.stdout.strip().split("\n")
                    relog("⚠️", "项目一有 %d 个待提交文件", len(lines))
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
                run_git_commit_with_retry(
                    SWARM_DIR,
                    f"swarm-evolve: 后勤同步 — {timestamp[:10]}",
                    repo_name="swarm",
                )
            else:
                relog("✅", "工作区干净")
        except subprocess.TimeoutExpired:
            relog("❌", "git status 超时（10s），跳过项目三同步")

        # ── 5. 分层委托诊断 ──
        run_delegation_diagnosis()

        # ── 5a. 强制委托检查 ──
        check_forced_delegation()

        # ── 6. ⬆️ 并行任务规划（新） ──
        plan_parallel_tasks()

        # ── 7. 更新 state.json ──
        state = load_state()
        state["step"] = "done"
        state["completed_at"] = timestamp
        if not state.get("started_at"):
            state["started_at"] = timestamp
        state["project_one_step"] = "done"
        state["project_three_step"] = "completed"
        save_state(state)

        relog("")
        relog("提示：")
        relog("  - 并行任务计划已写入 state.json parallel_plan 字段")
        relog("  - Hermes cronjob 可读取 plan.batches 按批执行")
        relog("  - 如遇冲突，请手动解决后修改 state.json 恢复")

    finally:
        release_pid_file()

    relog("=" * 60, "")
    relog("后勤脚本完成 — %s", timestamp)


if __name__ == "__main__":
    main()
