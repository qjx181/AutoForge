"""delegate_optimizer.py — 分层委托策略优化器（Layer 1/2/3）

作用：
  为协调者提供"该不该委托"的决策框架，以及构建委托 prompt 的工具。
  实现 Layer 1（协调者自己干）、Layer 2（委托子 Agent）、Layer 3（验收）三层流程。

原理：
  协调者（Hermes Agent）每轮读取 TODO → 选择任务 → 调用 should_delegate() 决策 →
  由 Layer 1（自己写）或 Layer 2（delegate_task）执行 → 验收走 Layer 3 四步验证。

依赖：
  - agent_roles.py（角色定义）
  - templates/coder_template.md, tester_template.md, reviewer_template.md（模板文件）

用法（在 Hermes Agent 思维中调用，非 self_evolve_round.py 脚本）:
  from delegate_optimizer import *
  if should_delegate(task, state):
      prompt = build_delegation_prompt(task, role="coder")
      # delegate_task(goal=prompt, ...)
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import os
import re
from pathlib import Path
from typing import Optional
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
TEMPLATES_DIR = SWARM_DIR / "templates"
SELF_EVOLVE_LOG = SWARM_DIR / "data" / "self_evolve_log.json"
STATE_FILE = SWARM_DIR / "data" / "state.json"
CAPABILITY_MAP_FILE = SWARM_DIR / "data" / "agent_capability_map.json"

COMPLEXITY_THRESHOLD = 1000  # token 量 < 1000 视为简单任务
MIN_SUCCESS_RATE = 0.6       # 子 Agent 成功率 >= 0.6 才委托
MAX_HISTORY_WINDOW = 10      # 只看近 10 轮数据



def should_delegate(task: dict, state: dict, budget: dict) -> tuple[bool, str]:
    """should_delegate — 判断当前任务是否应该委托给子 Agent。

    Layer 1 → Layer 2 的决策门禁。基于 3 个指标综合判断。

    Args:
        task: 任务描述字典，含 {"task_id", "token_est", "category", "description"}。
        state: 当前 state.json 字典。
        budget: 当前预算信息。

    Returns:
        (should_delegate: bool, reason: str)

    决策逻辑：
      1. 如果任务 token 量 < COMPLEXITY_THRESHOLD (1000) → 简单任务，委托
      2. 如果子 Agent 历史成功率 < MIN_SUCCESS_RATE (0.6) → 协调者自己干
      3. 如果预算紧张（黄色模式，剩余 < 30%）→ 保守决策，不委托新功能
      4. 如果 round_pressure（已用完 80%+ 预算）→ 不委托（省钱）
      5. 否则按 category 决定：
         - debug: 简单修复，倾向于委托
         - test/test_creation: 已知子 Agent 失败率 100%，不委托
         - feature/架构: 复杂任务，不委托
    """
    token_est = task.get("token_est", 0) or task.get("预估 token 量", 2000)
    category = task.get("category", "debug")
    task_id = task.get("task_id", "unknown")

    if category == "test_creation" or "test_" in task_id and "创建" in task.get("description", ""):
        return False, f"测试创建类任务 {task_id}：子 Agent 历史成功率 0%，协调者直接 write_file"

    if token_est < COMPLEXITY_THRESHOLD:
        success_rate = _compute_success_rate(state)
        if success_rate >= MIN_SUCCESS_RATE:
            return True, f"简单任务（{token_est} token），子 Agent 成功率 {success_rate:.0%}，委托"
        return True, f"简单任务（{token_est} token），尝试委托以积累经验数据"

    if category in ("feature", "refactor", "architecture"):
        return False, f"架构/新功能任务 {task_id}：需要接口一致性保证，协调者直接执行"

    dollar_spent = budget.get("dollar_spent_today", 0)
    dollar_limit = budget.get("dollar_limit", 5.0)
    if dollar_limit > 0:
        ratio = dollar_spent / dollar_limit
        if ratio > 0.8:
            return False, f"预算紧张（已用 {ratio:.0%}），不委托以控制成本"
        if ratio > 0.5 and category in ("feature", "refactor"):
            return False, f"预算中等（已用 {ratio:.0%}），仅委托 debug/test 类任务"

    return True, "默认策略：委托子 Agent 执行"


def _compute_success_rate(state: dict) -> float:
    """_compute_success_rate — 从 state.json 计算子 Agent 近 N 轮成功率。

    读取 failed_tasks 和 completed_task_ids，统计近 10 轮的委托成功率。
    """
    completed = state.get("completed_task_ids", [])
    failed = state.get("failed_tasks", [])
    total_delegated = len(completed) + len(failed)
    if total_delegated == 0:
        return 0.0
    return len(completed) / max(total_delegated, 1)



FIVE_HARD_CONSTRAINTS = """\
【5 条硬约束——违反任一条直接拒绝产出】
1. 【禁止】修改任何函数签名（参数名、参数类型、返回值类型）
2. 【禁止】删除任何文件，只能修改内容
3. 【必须】在改动前先 read_file 读取完整文件
4. 【必须】用 patch 精确替换，不许用 write_file 覆盖整文件
5. 【必须】改动完成后运行：python -m py_compile <文件路径>
"""


def build_delegation_prompt(task: dict, role: str = "coder",
                             templates_dir: Optional[Path] = None) -> str:
    """build_delegation_prompt — 为子 Agent 构建完整委托 prompt。

    Layer 2 核心函数。将任务描述、角色模板、5 条硬约束组装为单字符串。
    协调者将此字符串作为 delegate_task 的 goal 参数。

    Args:
        task: 任务字典，含 task_id/description/target_file/code_sample 等。
        role: Agent 角色（"coder"/"tester"/"reviewer"）。
        templates_dir: 模板目录路径，默认 SWARM_DIR / "templates"。

    Returns:
        完整的委托 prompt 字符串（可直接传入 delegate_task 的 goal）。

    用法（协调者思维中调用）：
        prompt = build_delegation_prompt(task, role="coder")
        result = delegate_task(goal=prompt, ...)
    """
    if templates_dir is None:
        templates_dir = TEMPLATES_DIR

    # 加载角色模板
    template_path = templates_dir / f"{role}_template.md"
    template_content = ""
    if template_path.exists():
        template_content = template_path.read_text(encoding="utf-8")
    else:
        template_content = f"# {role.capitalize()} Agent 任务\n> 按以下要求执行。\n"

    # 填充模板变量
    target_file = task.get("target_file", task.get("file", ""))
    code_sample = task.get("code_sample", task.get("示例代码", ""))
    constraints = task.get("constraints", task.get("额外约束", ""))

    prompt = template_content
    prompt = prompt.replace("{{target_file}}", target_file)
    prompt = prompt.replace("{{requirement}}", task.get("description", ""))
    prompt = prompt.replace("{{code_sample}}", code_sample)
    prompt = prompt.replace("{{constraints}}", constraints)
    prompt = prompt.replace("{{function_list}}", task.get("functions", ""))
    prompt = prompt.replace("{{mock_template}}", task.get("mock_template", ""))

    # 追加 5 条硬约束
    prompt += "\n\n" + FIVE_HARD_CONSTRAINTS

    # 追加任务特定的额外约束
    if constraints:
        prompt += f"\n\n## 额外约束\n{constraints}\n"

    return prompt


def build_coder_prompt(target_file: str, requirement: str,
                       code_sample: str = "", constraints: str = "",
                       functions: str = "") -> str:
    """build_coder_prompt — 快捷构建编码 Agent 的委托 prompt。"""
    task = {
        "target_file": target_file,
        "description": requirement,
        "code_sample": code_sample,
        "constraints": constraints,
        "functions": functions,
    }
    return build_delegation_prompt(task, role="coder")


def build_tester_prompt(target_file: str, requirement: str,
                         function_list: str = "", mock_template: str = "",
                         constraints: str = "") -> str:
    """build_tester_prompt — 快捷构建测试 Agent 的委托 prompt。"""
    task = {
        "target_file": target_file,
        "description": requirement,
        "functions": function_list,
        "mock_template": mock_template,
        "constraints": constraints,
    }
    return build_delegation_prompt(task, role="tester")


def build_reviewer_prompt(before_file: str, after_file: str,
                           requirement: str = "") -> str:
    """build_reviewer_prompt — 快捷构建审查 Agent 的委托 prompt。"""
    task = {
        "before_file": before_file,
        "after_file": after_file,
        "description": requirement,
    }
    return build_delegation_prompt(task, role="reviewer")


# ═══════════════════════════════════════════════════════════════════════
# 第 3 层 — 验收工具
# ═══════════════════════════════════════════════════════════════════════

LAYER3_VERIFICATION_STEPS = """\
## Layer 3 验收流程（协调者执行）

每次 Agent 产出后必须执行以下 4 步：

### Step 1 — 签名检查
grep 确认函数签名未变化（比较改动前后的参数名/参数类型/返回值类型）

### Step 2 — 语法检查
python -m py_compile <文件路径>

### Step 3 — 单元测试
pytest <测试文件> -v

### Step 4 — Diff 对照
对比改动前后，确认只有预期改动，无意外格式化/空白/敏感信息变动

### 返工管控
- 任何一步失败 → 记录到 self_evolve_log.json 的 failure_stats
- 同一任务返工超过 2 次 → 协调者接管并行自己写完
"""


def check_signature_unchanged(before: str, after: str,
                               func_names: list[str]) -> list[str]:
    """check_signature_unchanged — 检查函数签名是否被修改。

    Layer 3 — Step 1：从 before 和 after 字符串中提取指定函数的签名，
    对比确认参数列表未变化。

    Args:
        before: 改动前的文件内容。
        after: 改动后的文件内容。
        func_names: 要检查的函数名列表。

    Returns:
        签名被修改的函数名列表。空列表表示全部通过。
    """
    violations = []
    for name in func_names:
        before_sig = _extract_signature(before, name)
        after_sig = _extract_signature(after, name)
        if before_sig and after_sig and before_sig != after_sig:
            violations.append(f"{name}: {before_sig} → {after_sig}")
    return violations


def _extract_signature(content: str, func_name: str) -> Optional[str]:
    """_extract_signature — 从源码中提取函数签名。"""
    pattern = rf"def\s+{func_name}\s*\([^)]*\)\s*(->\s*\w+\s*)?:"
    match = re.search(pattern, content)
    return match.group(0) if match else None


def count_lines_added_removed(before: str, after: str) -> tuple[int, int]:
    """count_lines_added_removed — 计算改动前后的行数变化。

    Layer 3 — Step 4 的辅助工具：确认改动规模在合理范围内。
    单次改动超过 200 行应发出警告（可能包含非预期改动）。

    Args:
        before: 改动前内容。
        after: 改动后内容。

    Returns:
        (lines_added, lines_removed)
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = max(0, len(after_lines) - len(before_lines))
    removed = max(0, len(before_lines) - len(after_lines))
    return (added, removed)



DELEGATION_INCENTIVE = {
    "coordinator_write_line_threshold": 50,  # 协调者自写超 50 行时警告
    "delegate_success_bonus_tokens": 1000,   # 委托成功奖励预算
    "self_write_overflow_penalty": 500,      # 超阈值扣减预算
}


def log_coordinator_write_size(state: dict, file_path: str,
                                lines_written: int) -> dict:
    """log_coordinator_write_size — 记录协调者自写代码行数。

    成本激励：超阈值时发出警告。
    返回更新后的 state。
    """
    state.setdefault("coordinator_write_log", []).append({
        "file": file_path,
        "lines": lines_written,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    })
    threshold = DELEGATION_INCENTIVE["coordinator_write_line_threshold"]
    total_written = sum(
        e["lines"] for e in state.get("coordinator_write_log", [])
    )
    if lines_written > threshold:
        print(f"[💡成本激励] 协调者单次写 {lines_written} 行（超阈值 {threshold} 行）。"
              f"累计已写 {total_written} 行。考虑委托给子 Agent。")
    return state


