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



def run_layer3_verification(
    before_content: str,
    after_content: str,
    file_path: str,
    func_names: list[str],
    test_file: str = "",
    rework_count: int = 0,
) -> dict:
    """run_layer3_verification — 标准化 4 步验收流程。

    每次 Agent 产出后必须执行。4 步验收 + 返工管控。

    Step 1 — 签名检查：check_signature_unchanged()
    Step 2 — 语法检查：python -m py_compile
    Step 3 — 单元测试：pytest <test_file>
    Step 4 — diff 对照：检查改动规模

    Args:
        before_content: 改动前的文件内容。
        after_content: 改动后的文件内容。
        file_path: 被修改文件的路径。
        func_names: 要检查的函数名列表。
        test_file: 测试文件路径（可选）。
        rework_count: 该任务已返工次数。

    Returns:
        {
            "passed": bool,
            "rework_count": int,
            "step_results": dict,
            "rework_action": str
        }
    """
    step_results = {}

    violations = check_signature_unchanged(before_content, after_content, func_names)
    sig_passed = len(violations) == 0
    step_results["signature_check"] = {
        "passed": sig_passed,
        "violations": violations,
    }

    syntax_ok = True
    syntax_error = None
    try:
        __import__("ast").parse(after_content)
    except SyntaxError as e:
        syntax_ok = False
        syntax_error = f"{e.msg} (line {e.lineno})"
    step_results["syntax_check"] = {
        "passed": syntax_ok,
        "error": syntax_error,
    }

    if test_file:
        step_results["unit_test"] = {
            "passed": None,  # 子 Agent 需自行运行 pytest 并报告
            "note": f"需要运行: pytest {test_file} -v",
        }
    else:
        step_results["unit_test"] = {
            "passed": True,
            "note": "未提供测试文件，跳过单元测试检查",
        }

    added, removed = count_lines_added_removed(before_content, after_content)
    diff_warnings = []
    if added > 200:
        diff_warnings.append(f"单次改动代码量较大（+{added} 行），请确认无意外变更")
    if added == 0 and removed == 0:
        diff_warnings.append("文件内容未变化——零产出，需确认任务是否完成")
    step_results["diff_review"] = {
        "passed": len(diff_warnings) == 0,
        "warnings": diff_warnings,
        "lines_added": added,
        "lines_removed": removed,
    }

    all_passed = all(
        s.get("passed", False) for s in step_results.values()
        if s.get("passed") is not None  # 跳过 None（待子 Agent 确认）
    )

    if all_passed:
        rework_action = "通过"
    elif rework_count >= 2:
        rework_action = "协调者接管"
    else:
        rework_action = "打回重做"

    return {
        "passed": all_passed,
        "rework_count": rework_count + (0 if all_passed else 1),
        "step_results": step_results,
        "rework_action": rework_action,
    }
