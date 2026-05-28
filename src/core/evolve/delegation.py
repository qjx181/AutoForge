"""self_evolve_round.py — 项目三自进化后勤脚本

职责（每 30 分钟由 cronjob 触发）：
  1. PID 文件锁 + 冲突自愈
  2. 磁盘空间检查 + 日志轮转
  3. 成本熔断检查
  4. 项目一同步（git pull + commit）
  5. 项目三同步（git pull + commit）
  6. 🚀 持续优化引擎（九维全覆盖，任意目标项目）：
       扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快
  7. 分层委托诊断 + 强制委托检查
  8. ⬆️ 并行任务规划（微委托集成）
  9. 更新 state.json

注意：
  实际的任务执行（write_file / delegate_task）由 Hermes Agent cronjob 的 prompt 驱动。
  本脚本只做"后勤 + 规划"——打扫战场、生成执行计划。
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
from typing import Any, Optional

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


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


