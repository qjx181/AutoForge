"""evolve/cli — 自进化后勤脚本 CLI 入口

解析命令行参数，调用 scheduler.main() 执行完整后勤流程。
支持 --json-logs 参数启用 JSON 格式日志。
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

from src.core.evolve.config import PROJECT1_DIR
from src.core.evolve.config_ext import OPT_DIMENSIONS, _get_config
from src.core.evolve.cost import check_disk_space, check_cost_over_budget
from src.core.evolve.delegation import run_delegation_diagnosis, check_forced_delegation
from src.core.evolve.git_ops import check_and_heal_conflicts
from src.core.evolve.logging import relog
from src.core.evolve.state import load_state, acquire_pid_file, release_pid_file
from src.core.evolve.scheduler import (
    _parse_cli_args, _sync_project, _collect_optimization_targets,
    _run_optimization_engine, _run_deep_scan_and_tasks,
    _run_failure_analysis, _run_log_scan, _update_state_and_cost,
    check_and_heal_heartbeats, plan_parallel_tasks,
)

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


def main() -> None:
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

        if PROJECT1_DIR is not None:
            _sync_project(PROJECT1_DIR, "项目一", timestamp)
        else:
            relog("ℹ️", "项目一目录未配置（PROJECT1_DIR=None），跳过同步")
        _sync_project(SWARM_DIR, "项目三", timestamp)

        targets, is_dry_run, cost_tier = _collect_optimization_targets(cost_warning)
        cfg = _get_config()
        opt_dims = cfg.get("optimization_dimensions", None)
        _run_optimization_engine(targets, timestamp, opt_dims if opt_dims else OPT_DIMENSIONS, is_dry_run)

        _run_deep_scan_and_tasks(targets, cost_tier, timestamp)
        _run_failure_analysis(timestamp)
        _run_log_scan()

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
