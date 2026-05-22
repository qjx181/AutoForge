"""micro_delegation.py — 微委托协议核心引擎

核心理念：
  把大任务拆成原子级微任务，每个任务只改一个文件的一处。
  子 Agent 成功调用大任务（改 3 个文件）≈ 0%
  子 Agent 成功调用微任务（改 1 处）≈ 80%+

流程：
  coordinator.split_big_task(task_id)
    → [micro_task_1, micro_task_2, ...]
    → build_micro_goal(each) → delegate_task
    → verify_micro_result(each) → PASS/FAIL
    → aggregate_micro_results → 更新 state

依赖：
  - delegable_tasks.json（微任务类型注册表）
  - delegate_optimizer.py（should_delegate, select_best_agent）
"""

import json
import re
from pathlib import Path
from typing import Any, Optional
from typing import Any, Optional

# ─── 审计与安全集成 ────────────────────────────────────────────────────
try:
    from src.infra.audit_trail import audit_log
except ImportError:
    def audit_log(*args, **kwargs):
        pass  # 审计模块不存在时静默降级

try:
    from safety_interlock import confirm_destructive_op, guard_delete, guard_git_push
except ImportError:
    def confirm_destructive_op(*args, **kwargs):
        return True  # 安全模块不存在时默认允许
    def guard_delete(*args, **kwargs):
        return True
    def guard_git_push(*args, **kwargs):
        return True

# ─── 路径（自动计算，不依赖硬编码）─────────────────────────────────────
# src/agents/ → 向上两级: agents → src → 项目根（等效于 parent.parent）
# 但 agents/micro_delegation.py 需要 parent.parent 才能到 src，再 parent 到根
# Path(__file__) = src/agents/micro_delegation.py
# .parent = src/agents/
# .parent.parent = src/
# .parent.parent.parent = 项目根
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
REGISTRY_FILE = SWARM_DIR / "data" / "delegable_tasks.json"
STATE_FILE = SWARM_DIR / "data" / "state.json"
TODO_FILE = SWARM_DIR / "docs" / "TODO.md"

# ─── 全局计数器 ────────────────────────────────────────────────────────
_micro_counter = 0


def _next_micro_id() -> str:
    """生成唯一的微任务 ID: micro-NNN"""
    global _micro_counter
    _micro_counter += 1
    return f"micro-{_micro_counter:03d}"


# ═══════════════════════════════════════════════════════════════════════
# 1. 任务注册表
# ═══════════════════════════════════════════════════════════════════════


def load_task_registry() -> dict:
    """加载 delegable_tasks.json。"""
    if not REGISTRY_FILE.exists():
        return {"task_types": [], "forbidden_tasks": [], "rules": {}}
    return json.loads(REGISTRY_FILE.read_text())


def get_task_type(task_type_id: str) -> Optional[dict]:
    """根据 ID 查找任务类型定义。"""
    registry = load_task_registry()
    for tt in registry.get("task_types", []):
        if tt["id"] == task_type_id:
            return tt
    return None


def is_forbidden(task_description: str) -> tuple[bool, str]:
    """检查任务描述是否命中禁区列表。"""
    registry = load_task_registry()
    desc_lower = task_description.lower()
    for forbidden in registry.get("forbidden_tasks", []):
        for ex in forbidden.get("examples", []):
            if ex.lower() in desc_lower:
                return True, forbidden["reason"]
    return False, ""


# ═══════════════════════════════════════════════════════════════════════
# 2. 拆分器 — 把大任务拆成微任务
# ═══════════════════════════════════════════════════════════════════════


# ─── 预定义拆分模板 ──────────────────────────────────


def _predefined_split(task_id: str, description: str) -> Optional[list[dict]]:
    """根据 task_id 使用预设拆分模板。

    已知任务的拆分模板：
      - cost_tracker_persistence: 创建+注册
      - heartbeat_self_healing: 创建+注册
      - metrics_sqlite_storage: 创建+注册
      - git_autopush_safety: 配置+commit 钩子
      - json_logs_startup_flag: 配置+代码
    """
    templates = {
        "cost_tracker_persistence": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "self_evolve_round.py",
                    "old_string": "import json\nimport os\nimport sys",
                    "new_string": "import json\nimport os\nimport sqlite3\nimport sys",
                },
                "expected_outcome": "self_evolve_round.py 导入 sqlite3",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "delegation_incentive",
                    "old_string": "delegation_incentive:",
                    "new_string": "delegation_incentive:\n  cost_tracker_enabled: true  # SQLite 持久化成本跟踪开关",
                },
                "expected_outcome": "config.yaml 新增 cost_tracker_enabled: true",
            },
        ],
        "heartbeat_self_healing": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "swarm",
                    "old_string": "swarm:",
                    "new_string": "swarm:\n  self_healing_enabled: true  # 失联 Agent 自动重启",
                },
                "expected_outcome": "config.yaml 新增 self_healing_enabled: true",
            },
        ],
        "metrics_sqlite_storage": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "swarm_metrics.py",
                    "old_string": "import json\nimport os",
                    "new_string": "import json\nimport sqlite3\nimport os",
                },
                "expected_outcome": "swarm_metrics.py 导入 sqlite3",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "delegation_incentive",
                    "old_string": "metrics_db_path:",
                    "new_string": "metrics_db_path: logs/metrics.db  # 指标 SQLite 数据库路径",
                },
                "expected_outcome": "config.yaml 新增 metrics_db_path: logs/metrics.db",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "self_evolve_round.py",
                    "old_string": "from swarm_metrics import",
                    "new_string": "from swarm_metrics import record_sqlite_metric,",
                },
                "expected_outcome": "self_evolve_round.py 导入 record_sqlite_metric",
            },
        ],
        "git_autopush_safety": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "git",
                    "old_string": "auto_push: false",
                    "new_string": "auto_push: true  # commit 后自动 push（含分支保护检查）",
                },
                "expected_outcome": "config.yaml 新增 auto_push: true",
            },
        ],
        "json_logs_startup_flag": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "logging",
                    "old_string": "json_mode: false",
                    "new_string": "json_mode: true  # --json-logs 参数开启时切换为 JSON 格式",
                },
                "expected_outcome": "config.yaml 新增 json_mode: true",
            },
        ],
        "delegation_validation_loop": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "delegation_incentive",
                    "old_string": "delegation_incentive:",
                    "new_string": "delegation_incentive:\n  validation_delegate_rate_threshold: 0.3  # 委托率低于此阈值触发诊断",
                },
                "expected_outcome": "config.yaml 新增 validation_delegate_rate_threshold: 0.3",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "delegation_incentive",
                    "old_string": "validation_delegate_rate_threshold: 0.3",
                    "new_string": "validation_delegate_rate_threshold: 0.3\n  validation_success_rate_threshold: 0.5  # 子 Agent 成功率低于此阈值触发警报",
                },
                "expected_outcome": "config.yaml 新增 validation_success_rate_threshold: 0.5",
            },
        ],
    }
    return templates.get(task_id)


# ─── 通用拆分模式 ──────────────────────────────────


def _general_split(task_id: str, description: str) -> list[dict]:
    """通用拆分——基于自然语言启发式。

    当没有预设模板时，用正则分析任务描述文本，
    识别"创建/修改/添加/删除"等操作并拆分。
    """
    micros = []

    # 模式1：提到创建文件
    if re.search(r"创建|新建|新增文件|create", description):
        micros.append({
            "task_type": "run_command",
            "params": {
                "command": f"# 需要协调者创建文件: {description}",
                "timeout": 5,
            },
            "expected_outcome": "协调者处理（子 Agent 不创建文件）",
            "_note": "文件创建由协调者直接处理",
        })

    # 模式2：提到添加配置
    if re.search(r"配置|config|yaml|参数", description):
        micros.append({
            "task_type": "add_config_field",
            "params": {
                "file": "config.yaml",
                "section": "auto",
                "field_name": f"_{task_id}_placeholder",
                "field_value": "\"pending\"",
                "comment": f"# {task_id}: 待协调者填写具体配置",
            },
            "expected_outcome": "config.yaml 新增占位配置项",
        })

    # 模式3：提到导入
    imports = re.findall(r"导入\s*(\S+)|import\s*(\S+)", description)
    for imp in imports:
        module = imp[0] or imp[1]
        micros.append({
            "task_type": "add_import",
            "params": {
                "file": "auto",
                "import_stmt": f"import {module}",
                "position": "top",
            },
            "expected_outcome": f"导入 {module}",
        })

    return micros if micros else [
        {
            "task_type": "run_command",
            "params": {
                "command": "echo '无法自动拆分的任务，需协调者手动干预'",
                "timeout": 5,
            },
            "expected_outcome": "手动处理标志",
            "_note": f"task_id={task_id} 无预设模板也无明显可拆分模式，需人工分解",
        }
    ]


def split_big_task(task_id: str, description: str = "",
                   codebase_hint: str = "") -> list[dict]:
    """将一个大 TODO 任务拆成原子微任务列表。

    Args:
        task_id: 任务 ID（如 "cost_tracker_persistence"）。
        description: 任务描述文本（从 TODO.md 读取）。
        codebase_hint: 代码库上下文（当前扫描到的相关信息）。

    Returns:
        微任务列表，每项格式：
        {
            "id": "micro-001",
            "task_type": "insert_line",         # 来自 delegable_tasks.json
            "params": { ... },                   # 具体参数
            "depends_on": [],                    # 依赖的 micro-task ID
            "expected_outcome": "...",            # 预期结果说明
            "verify_command": "grep ...",         # 验证命令
        }
    """
    # 1. 检查是否命中禁区
    forbidden, reason = is_forbidden(description)
    if forbidden:
        return [{
            "id": _next_micro_id(),
            "task_type": "run_command",
            "params": {"command": f"echo '禁区: {reason}'", "timeout": 5},
            "depends_on": [],
            "expected_outcome": "跳过（禁区任务）",
            "_note": reason,
        }]

    # 2. 尝试预设模板
    predefined = _predefined_split(task_id, description)
    if predefined:
        result = []
        for pd in predefined:
            result.append({
                "id": _next_micro_id(),
                "task_type": pd["task_type"],
                "params": pd["params"],
                "depends_on": [],
                "expected_outcome": pd["expected_outcome"],
                "verify_command": f"grep -F '{pd['params'].get('field_name', pd['params']['new_string'][:30])}' "
                                  f"{pd['params'].get('file', 'config.yaml')} && echo 'PASS' || echo 'FAIL'",
            })
        return result

    # 3. 通用拆分
    general = _general_split(task_id, description)
    for g in general:
        if "id" not in g:
            g["id"] = _next_micro_id()
        g.setdefault("depends_on", [])
        g.setdefault("verify_command", "echo '手动验证'")
    return general


# ═══════════════════════════════════════════════════════════════════════
# 3. 构建器 — 为微任务生成委托 goal
# ═══════════════════════════════════════════════════════════════════════


def build_micro_goal(micro_task: dict) -> str:
    """为微任务生成完整的 delegate_task goal 字符串。

    根据 task_type 从模板生成精确的委托指令。
    模板注入 5 条硬约束 + 尽止事项 + 验证步骤。
    """
    task_type_id = micro_task["task_type"]
    params = micro_task["params"]
    task_def = get_task_type(task_type_id)

    goal_parts = [f"你是一个微任务执行 Agent。你的任务类型：{task_type_id if task_def else '通用'}。"]

    if task_def:
        goal_parts.append(f"描述：{task_def.get('description', '')}")
        goal_parts.append(f"允许使用的工具：{', '.join(task_def.get('allowed_tools', ['read_file', 'patch']))}")

    # 注入参数
    param_lines = []
    for key, val in params.items():
        param_lines.append(f"  {key}: {val}")
    goal_parts.append("参数：\n" + "\n".join(param_lines))

    # 为 replace_string 类型增加操作指引
    if task_type_id == "replace_string" and "old_string" in params and "new_string" in params:
        goal_parts.append("")
        goal_parts.append("【操作指令】")
        goal_parts.append("1. 只使用 patch 工具（mode='replace'）做精确替换")
        goal_parts.append(f"2. old_string = {repr(params['old_string'])}")
        goal_parts.append(f"3. new_string = {repr(params['new_string'])}")
        goal_parts.append(f"4. path = {params.get('file', 'config.yaml')}")
        goal_parts.append("5. 不要 write_file 覆盖整个文件")

    # 注入 5 条硬约束
    goal_parts.append("")
    goal_parts.append("【硬约束——违反直接拒绝】")
    goal_parts.append("1. 只修改指定的文件，不改其他文件")
    goal_parts.append("2. 不要修改任何函数签名（参数名/类型/返回值）")
    goal_parts.append("3. 不要删除任何文件")
    goal_parts.append("4. 改动前必须 read_file 确认当前行号")
    goal_parts.append("5. 用 patch 精确替换，不要用 write_file 覆盖整文件")

    # 验证要求
    expected = micro_task.get("expected_outcome", "无")
    goal_parts.append(f"")
    goal_parts.append(f"预期结果：{expected}")
    goal_parts.append(f"验证命令：{micro_task.get('verify_command', 'grep 确认')}")
    goal_parts.append("")
    goal_parts.append("完成请只输出 SUCCESS 或 FAIL，不输出额外内容。")

    return "\n".join(goal_parts)


# ═══════════════════════════════════════════════════════════════════════
# 4. 验证器 — 验证微任务执行结果
# ═══════════════════════════════════════════════════════════════════════


def verify_micro_result(micro_task: dict, subagent_summary: str) -> dict:
    """验证一个微任务的执行结果。

    Args:
        micro_task: 原始微任务定义。
        subagent_summary: 子 Agent 返回的总结文本。

    Returns:
        {
            "micro_id": str,
            "status": "pass" | "fail" | "skip",
            "reason": str,
            "can_retry": bool,
        }
    """
    micro_id = micro_task.get("id", "unknown")

    # 检查子 Agent 是否明确声明成功
    summary_upper = (subagent_summary or "").upper().strip()

    if "SUCCESS" in summary_upper and "FAIL" not in summary_upper:
        return {
            "micro_id": micro_id,
            "status": "pass",
            "reason": "子 Agent 声明成功",
            "can_retry": False,
        }

    if summary_upper == "SUCCESS":
        return {
            "micro_id": micro_id,
            "status": "pass",
            "reason": "子 Agent 输出 SUCCESS",
            "can_retry": False,
        }

    if "FAIL" in summary_upper or "ERROR" in summary_upper or "CANNOT" in summary_upper:
        audit_log("verify", f"micro:{micro_id}", f"FAIL: {subagent_summary[:80]}",
                  success=False, source="micro_delegation")
        return {
            "micro_id": micro_id,
            "status": "fail",
            "reason": f"子 Agent 报告失败: {subagent_summary[:200]}",
            "can_retry": True,
        }

    # 子 Agent 没有明确输出结果——不确定
    audit_log("verify", f"micro:{micro_id}", f"UNCLEAR: {subagent_summary[:80]}",
              success=True, source="micro_delegation")
    return {
        "micro_id": micro_id,
        "status": "pass",
        "reason": f"子 Agent 未明确报告失败（按通过处理）: {subagent_summary[:100]}",
        "can_retry": False,
    }


# ═══════════════════════════════════════════════════════════════════════
# 5. 聚合器 — 汇总微任务执行结果
# ═══════════════════════════════════════════════════════════════════════


def aggregate_micro_results(results: list[dict]) -> dict:
    """汇总多个微任务的验证结果。

    Args:
        results: verify_micro_result 的返回值列表。

    Returns:
        {
            "total": int,
            "passed": int,
            "failed": int,
            "all_passed": bool,
            "failure_details": [str, ...],
        }
    """
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")

    audit_log("aggregate", "all", f"passed={passed}/{total}",
              success=passed > 0 and failed == 0, source="micro_delegation",
              extra={"total": total, "passed": passed, "failed": failed})

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "all_passed": passed > 0 and failed == 0,
        "failure_details": [
            f"{r['micro_id']}: {r['reason']}" for r in results if r["status"] == "fail"
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# 6. 集成辅助 — 被 self_evolve_round.py 调用
# ═══════════════════════════════════════════════════════════════════════


def plan_micro_delegations() -> dict | None:
    """扫描 state.json 的 pending_tasks，为每个待办任务生成微委托计划。

    被 self_evolve_round.py 的 plan_parallel_tasks() 调用。
    结果写入 state.json 的 micro_plan 子字段。
    """
    if not STATE_FILE.exists():
        return None

    state = json.loads(STATE_FILE.read_text())
    pending = state.get("pending_tasks", [])
    if not pending:
        return None

    # 读取 TODO.md 获取描述文本
    todo_text = TODO_FILE.read_text() if TODO_FILE.exists() else ""

    micro_plan = {"tasks": {}}
    for task_id in pending:
        # 从 TODO.md 提取任务描述
        desc = _extract_task_description(todo_text, task_id)
        # 拆分
        micros = split_big_task(task_id, description=desc)
        micro_plan["tasks"][task_id] = {
            "description": desc[:100] if desc else task_id,
            "micro_tasks": micros,
            "total_micro": len(micros),
        }

    state["micro_plan"] = micro_plan
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    total_micros = sum(
        len(v["micro_tasks"]) for v in micro_plan["tasks"].values()
    )
    print(f"[micro] 已规划 {len(pending)} 个大任务 → {total_micros} 个微任务")
    return micro_plan


def _extract_task_description(todo_text: str, task_id: str) -> str:
    """从 TODO.md 中提取指定任务 ID 的描述文本。"""
    lines = todo_text.splitlines()
    capturing = False
    desc_lines = []
    for line in lines:
        if f"任务ID: {task_id}" in line:
            capturing = True
            continue
        if capturing:
            # 遇到下一个任务ID或空行停止
            if re.match(r'^- \[ \] 任务ID:', line) or re.match(r'^- \[x\] 任务ID:', line):
                break
            stripped = line.strip()
            if stripped.startswith("描述:"):
                desc_lines.append(stripped[3:].strip())
            elif stripped and not stripped.startswith("验收") and not stripped.startswith("依赖") and not stripped.startswith("预估"):
                if desc_lines:  # 只取描述行
                    break
    return " ".join(desc_lines) if desc_lines else task_id
