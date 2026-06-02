"""evolve/delegation — Agent 委托诊断与强制委托

职责：
  - run_delegation_diagnosis: 检查子 Agent 健康状态，必要时重新委托
  - check_forced_delegation: 检查是否有强制委托任务待执行
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
from src.core.evolve.logging import relog
from src.core.evolve.state import load_state, save_state

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


def run_delegation_diagnosis() -> None:
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

                waste = entry.get("waste", "")
                if "delegate" in waste.lower():
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




def check_forced_delegation() -> None:
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


