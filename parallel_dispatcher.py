"""parallel_dispatcher.py — 并行任务分发器（Parallel Dispatcher）

作用：
  将串行任务派发模式改为并行分发，同时跑最多 3 个 Agent，提升吞吐量。
  协调者拿需要精确接口保持的任务（sync→async 改造、测试文件），
  delegate_task 拿增量修改/文档更新/配置变更等子任务。

原理：
  Hermes 的 max_concurrent_children 默认为 3。此分发器基于并发上限
  将任务分批，每批最多 3 个，异步并行派发。

用法（在协调者思维中调用）：
    from parallel_dispatcher import dispatch_tasks, parallel_dispatch
    # 模式 1: 直接计算 ready 任务并分批
    tasks = dispatch_tasks(state, todo_tasks)
    # 模式 2: 手动分批
    results = parallel_dispatch(task_batches, coordinator_tasks)

依赖：
  - delegate_optimizer.py（should_delegate, select_best_agent）
  - agent_capability_map.json（Agent 能力画像）
  - state.json（读取预算、已完成任务、并发限制）
"""

import json
import datetime
from pathlib import Path
from typing import Optional

# ─── 路径 ──────────────────────────────────────────────────────────────
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
STATE_FILE = SWARM_DIR / "state.json"
CAPABILITY_MAP = SWARM_DIR / "agent_capability_map.json"

# ─── 默认并发上限 ──────────────────────────────────────────────────────
DEFAULT_MAX_CONCURRENT = 3


# ═══════════════════════════════════════════════════════════════════════
# 核心函数
# ═══════════════════════════════════════════════════════════════════════

def dispatch_tasks(
    state: dict,
    todo_tasks: list[dict],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> dict:
    """dispatch_tasks — 分配本轮任务到不同执行路径。

    根据任务类型和 Agent 能力画像，决策哪些任务由协调者直接 write_file，
    哪些委托给子 Agent。生成分批并发计划。

    Args:
        state: 当前 state.json 字典。
        todo_tasks: 待办任务列表，每项含 task_id/category/token_est/depends。
        max_concurrent: 每批最大并发数（默认 3）。

    Returns:
        分配计划字典：
        {
            "coordinator_tasks": [task, ...],  # 协调者自己写的任务
            "delegate_tasks": [task, ...],      # 委托给子 Agent 的任务
            "batches": [                        # 分批计划（每批 ≤ max_concurrent）
                [task, ...],
                [task, ...],
            ],
            "skipped_tasks": [task, ...],       # 因依赖/预算跳过的任务
            "stats": {"total": N, "coordinator": N, "delegate": N, "skipped": N}
        }
    """
    # 尝试导入 delegate_optimizer
    try:
        from delegate_optimizer import should_delegate, get_agent_capability
        OPTIMIZER_AVAILABLE = True
    except ImportError:
        OPTIMIZER_AVAILABLE = False

    budget = state.get("daily_budget", {})
    completed = set(state.get("completed_task_ids", []))
    in_progress = set(state.get("in_progress_tasks", []))
    permanently_failed = set(state.get("permanently_failed", []))

    coordinator_tasks = []
    delegate_tasks = []
    skipped_tasks = []

    for task in todo_tasks:
        task_id = task.get("task_id", "unknown")

        # 跳过已完成/进行中/永久失败的任务
        if task_id in completed or task_id in in_progress or task_id in permanently_failed:
            continue

        # 检查依赖
        deps = task.get("depends", [])
        dep_blocked = False
        for dep in deps:
            if dep not in completed:
                skipped_tasks.append({**task, "skip_reason": f"依赖 {dep} 未完成"})
                dep_blocked = True
                break
        if dep_blocked:
            continue

        # 决策：协调者干 vs 委托
        task_with_meta = {
            "task_id": task_id,
            "token_est": task.get("token_est", task.get("预估 token 量", 2000)),
            "category": _infer_category(task),
            "description": task.get("description", ""),
        }

        if OPTIMIZER_AVAILABLE:
            should_del, reason = should_delegate(task_with_meta, state, budget)
        else:
            should_del, reason = _fallback_should_delegate(task_with_meta)

        if should_del:
            delegate_tasks.append(task)
        else:
            coordinator_tasks.append(task)

    # 生成分批计划
    all_executable = coordinator_tasks + delegate_tasks
    batches = _batch_tasks(delegate_tasks, max_concurrent)

    return {
        "coordinator_tasks": coordinator_tasks,
        "delegate_tasks": delegate_tasks,
        "batches": batches,
        "skipped_tasks": skipped_tasks,
        "stats": {
            "total": len(todo_tasks),
            "coordinator": len(coordinator_tasks),
            "delegate": len(delegate_tasks),
            "skipped": len(skipped_tasks),
        },
    }


def _infer_category(task: dict) -> str:
    """从任务描述推断类别（debug/test/feature/refactor）"""
    desc = (task.get("description", "") + " " + task.get("任务ID", "")).lower()

    # 测试类
    if any(kw in desc for kw in ["test", "测试", "单元测试", "压力测试", "pytest"]):
        return "test"

    # 调试/修复类
    if any(kw in desc for kw in ["async", "修复", "bug", "加固", "重试",
                                   "diagnos", "诊断", "参数", "调优",
                                   "清理", "删除", "import"]):
        return "debug"

    # 重构类
    if any(kw in desc for kw in ["重构", "refactor", "重写", "接口重设计"]):
        return "refactor"

    # 默认：功能类
    return "feature"


def _fallback_should_delegate(task: dict) -> tuple[bool, str]:
    """fallback 方案（delegate_optimizer 不可用时）"""
    category = task.get("category", "debug")
    token_est = task.get("token_est", 2000)

    # 从零创建测试/框架类 — 不委托
    if category in ("test_creation",):
        return False, "回退策略：测试创建类不委托"

    # 简单任务 — 委托
    if token_est < 1000 and category in ("debug", "config"):
        return True, "回退策略：简单任务委托"

    # 复杂/新功能 — 不委托
    return False, "回退策略：复杂任务协调者直接处理"


def _batch_tasks(tasks: list[dict], max_concurrent: int) -> list[list[dict]]:
    """将任务分批，每批不超过 max_concurrent 个"""
    batches = []
    for i in range(0, len(tasks), max_concurrent):
        batch = tasks[i:i + max_concurrent]
        batches.append(batch)
    return batches


# ═══════════════════════════════════════════════════════════════════════
# 并发控制辅助
# ═══════════════════════════════════════════════════════════════════════

def get_max_concurrent_from_config() -> int:
    """从 state.json 读取 max_concurrent_children 配置。"""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state.get("delegation_config", {}).get(
            "max_concurrent_children", DEFAULT_MAX_CONCURRENT
        )
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return DEFAULT_MAX_CONCURRENT


def estimate_round_tokens(tasks: list[dict]) -> int:
    """估算本轮总 token 消耗（用于预算控制）。"""
    total = 0
    for task in tasks:
        total += task.get("token_est", task.get("预估 token 量", 2000))
    return total


# ═══════════════════════════════════════════════════════════════════════
# 协调者工作分配
# ═══════════════════════════════════════════════════════════════════════

def get_coordinator_workload(coordinator_tasks: list[dict]) -> dict:
    """计算协调者本轮工作负载统计。

    Returns:
        {
            "total_tasks": N,
            "estimated_lines": N,  # 粗略估计：token_est / 5
            "over_threshold": bool,  # 是否超过 50 行阈值
        }
    """
    total_est_token = sum(
        t.get("token_est", t.get("预估 token 量", 2000))
        for t in coordinator_tasks
    )
    estimated_lines = total_est_token // 5  # 粗略估算
    return {
        "total_tasks": len(coordinator_tasks),
        "estimated_lines": estimated_lines,
        "over_threshold": estimated_lines > 50,
    }


def log_dispatch_plan(plan: dict) -> None:
    """输出派发计划到 stderr（人类可读）。"""
    stats = plan["stats"]
    print(
        f"[并行分发] 本轮计划：总计 {stats['total']} 个任务，"
        f"协调者 {stats['coordinator']} 个，"
        f"委托 {stats['delegate']} 个，"
        f"跳过 {stats['skipped']} 个",
        flush=True,
    )

    for skipped in plan.get("skipped_tasks", []):
        print(
            f"  ⏭️ 跳过 {skipped.get('task_id', '?')}: "
            f"{skipped.get('skip_reason', '原因未知')}",
            flush=True,
        )

    for i, batch in enumerate(plan.get("batches", [])):
        task_names = [t.get("task_id", t.get("任务ID", "?")) for t in batch]
        print(f"  📦 第 {i+1} 批（{len(batch)} 个）: {', '.join(task_names)}", flush=True)

    for task in plan.get("coordinator_tasks", []):
        print(
            f"  ✍️  协调者: {task.get('task_id', task.get('任务ID', '?'))}",
            flush=True,
        )
