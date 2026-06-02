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
import logging
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
TEMPLATES_DIR = SWARM_DIR / "templates"
SELF_EVOLVE_LOG = SWARM_DIR / "data" / "self_evolve_log.json"
STATE_FILE = SWARM_DIR / "data" / "state.json"
CAPABILITY_MAP_FILE = SWARM_DIR / "data" / "agent_capability_map.json"

COMPLEXITY_THRESHOLD = 1000  # token 量 < 1000 视为简单任务
MIN_SUCCESS_RATE = 0.6       # 子 Agent 成功率 >= 0.6 才委托
MAX_HISTORY_WINDOW = 10      # 只看近 10 轮数据



def _calc_basic_stats(rounds: list) -> dict:
    """计算基本统计量。"""
    total_rounds = len(rounds)
    success_count = sum(1 for r in rounds if r.get("result") == "success")
    failed_count = sum(1 for r in rounds if r.get("result") != "success")
    success_rate = success_count / total_rounds if total_rounds > 0 else 0
    return {"total_rounds": total_rounds, "success_count": success_count,
            "failed_count": failed_count, "overall_success_rate": round(success_rate, 3)}


def _analyze_delegation_patterns(rounds: list) -> dict:
    """分析委托模式。"""
    delegated_rounds = [r for r in rounds
                        if "delegate" in str(r.get("approach", ""))
                        or "delegate" in r.get("task", "")]
    delegate_count = len(delegated_rounds)
    delegate_success = sum(1 for r in delegated_rounds
                           if r.get("result") == "success")
    delegate_success_rate = delegate_success / delegate_count if delegate_count > 0 else 0
    return {"delegated_rounds": delegate_count,
            "delegate_success_count": delegate_success,
            "delegate_failed_count": delegate_count - delegate_success,
            "delegate_success_rate": round(delegate_success_rate, 3)}


def _analyze_failure_patterns(rounds: list) -> dict:
    """分析失败归因。"""
    failure_patterns = {}
    for r in rounds:
        waste = r.get("waste", "")
        failure_type = _classify_failure(str(r.get("task", "")) + " " + waste)
        if failure_type:
            failure_patterns.setdefault(failure_type, 0)
            failure_patterns[failure_type] += 1

    failure_keywords = {}
    for r in rounds:
        waste = r.get("waste", "")
        for kw in ["mock", "patch", "签名", "import", "语法", "超时",
                    "路径", "cd", "venv", "pip", "ast.parse", "py_compile",
                    "replace_all", "write_file"]:
            if kw in waste.lower():
                failure_keywords.setdefault(kw, 0)
                failure_keywords[kw] += 1

    return {
        "failure_patterns": dict(sorted(failure_patterns.items(), key=lambda x: -x[1])),
        "failure_keywords": dict(sorted(failure_keywords.items(), key=lambda x: -x[1])),
    }


def _analyze_trend(rounds: list, total_rounds: int) -> dict:
    """趋势分析：最近 5 轮和之前 5 轮对比。"""
    recent_count = min(5, total_rounds)
    recent_rounds = rounds[-recent_count:]
    old_rounds = rounds[-recent_count*2:-recent_count] if total_rounds >= recent_count*2 else []

    recent_success = sum(1 for r in recent_rounds if r.get("result") == "success")
    old_success = sum(1 for r in old_rounds if r.get("result") == "success")

    return {
        "recent_5_success_rate": round(recent_success / len(recent_rounds), 3) if recent_rounds else 0,
        "previous_5_success_rate": round(old_success / len(old_rounds), 3) if old_rounds else 0,
        "improving": (recent_success / len(recent_rounds) > old_success / len(old_rounds))
                     if recent_rounds and old_rounds else None,
    }


def _find_token_heavy_rounds(rounds: list) -> list:
    """找出最耗 token 的轮次。"""
    token_heavy = []
    for r in rounds:
        lines_added = r.get("lines_added", 0) or 0
        if lines_added > 300:
            token_heavy.append({
                "round": r.get("round"),
                "task": r.get("task", "")[:60],
                "lines_added": lines_added,
            })
    return token_heavy[:5]


def diagnose_failures(evolve_log_path: Optional[Path] = None) -> dict:
    """diagnose_failures — 分析 self_evolve_log.json 诊断委托失败模式。

    此函数是本模块自带的诊断工具，也可被 diagnose_subagent_failure 委托任务调用。

    Args:
        evolve_log_path: self_evolve_log.json 的路径，默认 SWARM_DIR / "self_evolve_log.json"。

    Returns:
        诊断报告字典，含总委托次数/成功率/失败归因/趋势。
    """
    path = evolve_log_path or SELF_EVOLVE_LOG
    if not path.exists():
        return {"error": f"日志文件不存在: {path}"}

    try:
        rounds = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"日志文件解析失败: {e}"}

    if not isinstance(rounds, list):
        return {"error": "日志格式错误：期望 JSON 数组"}

    stats = _calc_basic_stats(rounds)
    delegation = _analyze_delegation_patterns(rounds)
    failures = _analyze_failure_patterns(rounds)
    trend = _analyze_trend(rounds, stats["total_rounds"])
    token_heavy = _find_token_heavy_rounds(rounds)

    total_lines_added = sum(r.get("lines_added", 0) or 0 for r in rounds)
    total_lines_removed = sum(r.get("lines_removed", 0) or 0 for r in rounds)
    recent_rounds = rounds[-recent_count:] if recent_count > 0 else []
    return {
        "previous_5_success_rate": round(old_success / len(old_rounds), 3) if old_rounds else 0,
        **delegation,
        **failures,
        "trend": trend,
        "improving": (recent_success / len(recent_rounds) > old_success / len(old_rounds)) if recent_rounds and old_rounds else None,
        "total_lines_added": total_lines_added,
        "total_lines_removed": total_lines_removed,
    }


def _classify_failure(text: str) -> Optional[str]:
    """_classify_failure — 从文本中分类失败类型。"""
    patterns = [
        (r"(?:mock|patch).*(?:fail|error|import)", "mock_import_failure"),
        (r"ast\.parse|py_compile|SyntaxError", "syntax_error"),
        (r"venv|pip.*install|externally.managed", "environment_dependency"),
        (r"cd|中文字|路径", "chinese_path_issue"),
        (r"签名|signature|函数.*签", "signature_drift"),
        (r"超时|timeout", "timeout"),
        (r"(?:空文件|零文件|假产出|fake_output|0 files)", "zero_file_output"),
        (r"git.*conflict|pull.*rebase", "git_conflict"),
        (r"r(?:eplace_all|eplace.*all)", "replace_all_issue"),
    ]
    for pattern, failure_type in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return failure_type
    return None


def write_diagnosis_to_log(diagnosis: dict) -> bool:
    """write_diagnosis_to_log — 将诊断报告写入 self_evolve_log.json。

    在 self_evolve_log.json 的根级增加 diagnosis 字段。如果已有 diagnosis 字段则更新。

    Args:
        diagnosis: diagnose_failures() 返回的诊断报告字典。

    Returns:
        True: 写入成功。False: 写入失败。
    """
    if not SELF_EVOLVE_LOG.exists():
        return False
    try:
        data = json.loads(SELF_EVOLVE_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if isinstance(data, list):
        data = {"rounds": data}

    data["diagnosis"] = diagnosis
    data["diagnosis_updated_at"] = __import__("datetime").datetime.now().isoformat()

    SELF_EVOLVE_LOG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return True





def get_agent_capability(agent_id: str) -> Optional[dict]:
    """get_agent_capability — 查询指定 Agent 的能力画像。

    Layer 1 辅助函数。协调者根据此画像决定是否委托给该 Agent。

    Args:
        agent_id: Agent 标识符（如 "agent-coder", "agent-tester"）。

    Returns:
        Agent 能力画像字典，或 None（文件不存在/Agent 不存在）。

    用法（协调者思维中调用）：
        cap = get_agent_capability("agent-coder")
        if cap and cap["success_rate"] > 0.5:
            # 可靠，可以委托
        elif cap and cap["success_rate"] > 0.3:
            # 仅委托简单任务
        else:
            # 协调者自己干
    """
    if not CAPABILITY_MAP_FILE.exists():
        return None
    try:
        data = json.loads(CAPABILITY_MAP_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data.get("agents", {}).get(agent_id)
    except (json.JSONDecodeError, OSError):
        return None


def update_agent_capability(agent_id: str, task_result: dict) -> bool:
    """update_agent_capability — 更新 Agent 能力画像。

    每次委托完成后由协调者调用，记录任务结果（成功/失败、消耗 token、失败原因）。

    Args:
        agent_id: Agent 标识符。
        task_result: 任务结果字典，含：
            - success: bool
            - tokens_used: int
            - failure_pattern: str
            - task_type: str

    Returns:
        True: 更新成功。False: 更新失败。
    """
    if not CAPABILITY_MAP_FILE.exists():
        return False
    try:
        data = json.loads(CAPABILITY_MAP_FILE.read_text(encoding="utf-8"))
        agents = data.get("agents", {})
        if agent_id not in agents:
            return False

        agent = agents[agent_id]
        agent["total_tasks_assigned"] = agent.get("total_tasks_assigned", 0) + 1
        agent["last_assigned"] = __import__("datetime").datetime.now().isoformat()

        if task_result.get("success"):
            agent["successful_tasks"] = agent.get("successful_tasks", 0) + 1
        else:
            fp = task_result.get("failure_pattern", "")
            if fp:
                existing = agent.get("failure_pattern", "")
                existing_parts = [p.strip() for p in existing.split(",") if p.strip()]
                if fp not in existing_parts:
                    existing_parts.append(fp)
                agent["failure_pattern"] = ", ".join(existing_parts)

        total = agent.get("total_tasks_assigned", 0)
        succeeded = agent.get("successful_tasks", 0)
        agent["success_rate"] = round(succeeded / max(total, 1), 2)

        prev_avg = agent.get("avg_tokens_used", 0)
        prev_count = max(total - 1, 0)
        new_tokens = task_result.get("tokens_used", 0)
        if prev_count > 0:
            agent["avg_tokens_used"] = round(
                (prev_avg * prev_count + new_tokens) / total, 0
            )
        else:
            agent["avg_tokens_used"] = new_tokens

        history_entry = {
            "timestamp": __import__("datetime").datetime.now().isoformat(),
            "agent_id": agent_id,
            "task_type": task_result.get("task_type", "unknown"),
            "success": task_result.get("success", False),
            "tokens_used": task_result.get("tokens_used", 0),
        }
        data.setdefault("agent_selection_history", []).append(history_entry)
        data["coordination_rules"]["last_updated"] = history_entry["timestamp"]

        CAPABILITY_MAP_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return True
    except (json.JSONDecodeError, OSError) as e:
        logging.info(f"[⚠️ 能力画像] 更新失败: {e}")
        return False


def select_best_agent(task_type: str) -> str:
    """select_best_agent — 根据任务类型选择最佳 Agent。

    Layer 1 决策辅助。基于 Agent 能力画像，选择成功率最高且适合该任务类型的 Agent。

    Args:
        task_type: 任务类型（"incremental_modification", "code_review",
                   "test_creation", "elastic"）。

    Returns:
        选中的 Agent ID（如 "agent-coder"）。

    规则：
        - success_rate > 0.5: 优先派任务
        - success_rate 0.3~0.5: 仅派简单任务
        - success_rate < 0.3 或 rate=0: 仅派探索性/低风险任务
        - 新 Agent（rate=0）优先派简单探索任务以积累数据
    """
    if not CAPABILITY_MAP_FILE.exists():
        return "agent-coder"  # 默认

    try:
        data = json.loads(CAPABILITY_MAP_FILE.read_text(encoding="utf-8"))
        agents = data.get("agents", {})
    except (json.JSONDecodeError, OSError):
        return "agent-coder"

    sorted_agents = sorted(
        agents.items(),
        key=lambda item: (
            0 if item[1].get("success_rate", 0) == 0 else 1,
            item[1].get("success_rate", 0)
        ),
        reverse=True,
    )

    for agent_id, capability in sorted_agents:
        rate = capability.get("success_rate", 0)
        total = capability.get("total_tasks_assigned", 0)

        if total == 0 and task_type in ("elastic", "config_update"):
            return agent_id

        if rate >= 0.5:
            return agent_id

        if rate >= 0.3 and task_type in ("incremental_modification",
                                          "config_update"):
            return agent_id

    return "agent-coder"

