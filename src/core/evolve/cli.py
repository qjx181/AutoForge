#!/usr/bin/env python3
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

# ─── 路径（自动计算，不依赖硬编码）─────────────────────────────────────
# self_evolve_round.py 现在位于 src/core/，需要向上两级回到项目根目录
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()

# ─── PROJECT1_DIR：从环境变量或配置读取，不硬编码路径 ──────────────────
# 用法：export PROJECT1_DIR=/path/to/project1
# 或在 config.yaml 中设置 project1_dir 字段

def main():
    """主入口 — 完整流程，调用各个子函数。"""
    timestamp = _parse_cli_args()
    relog("=" * 60, "")
    relog("后勤脚本启动 — %s", timestamp)

    if not acquire_pid_file():
        relog("⏭️", "另一个实例正在运行，退出")
        sys.exit(1)

    try:
        state = load_state()
        check_and_heal_conflicts()

        disk = check_disk_space()
        if disk.get("paused"):
            relog("⏸️", "磁盘空间不足，跳过本轮主要操作")

        cost_warning = check_cost_over_budget()
        if cost_warning:
            relog("⏸️", "成本超限，跳过 LLM 密集型操作")

        # 项目同步
        if PROJECT1_DIR is not None:
            _sync_project(PROJECT1_DIR, "项目一", timestamp)
        else:
            relog("ℹ️", "项目一目录未配置（PROJECT1_DIR=None），跳过同步")
        _sync_project(SWARM_DIR, "项目三", timestamp)

        # 优化引擎
        targets, is_dry_run, cost_tier = _collect_optimization_targets(cost_warning)
        cfg = _get_config()
        opt_dims = cfg.get("optimization_dimensions", None)
        _run_optimization_engine(targets, timestamp, opt_dims if opt_dims else OPT_DIMENSIONS, is_dry_run)

        # 深层修复任务 + 失败分析 + 日志扫描
        _run_deep_scan_and_tasks(targets, cost_tier, timestamp)
        _run_failure_analysis(timestamp)
        _run_log_scan()

        # 委托诊断 + 心跳 + 并行规划
        run_delegation_diagnosis()
        check_forced_delegation()
        check_and_heal_heartbeats()
        plan_parallel_tasks()

        try:
            sys.path.insert(0, str(SWARM_DIR))
            from src.agents.micro_delegation import plan_micro_delegations
            plan_micro_delegations()
            relog("📋", "微委托规划完成")
        except ImportError as e:
            relog("⚠️", "micro_delegation 不可用: %s", e)
        except Exception as e:
            relog("⚠️", "微委托规划失败: %s", e)

        # 成本 + 状态
        _update_state_and_cost(state, timestamp)

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
