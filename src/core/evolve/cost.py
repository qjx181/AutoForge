"""evolve/cost — 磁盘空间检查 + 成本熔断 + 日志轮转

职责：
  - check_disk_space: 检查磁盘剩余空间，低于阈值触发日志轮转
  - check_cost_over_budget: 检查当日 LLM 成本是否超限
  - rotate_logs: 按天数清理旧日志
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
from typing import Optional

from src.core.evolve.config_ext import MIN_FREE_GB, MAX_LOG_DAYS
from src.core.evolve.logging import relog
from src.core.evolve.state import load_state

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


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

            stat = os.statvfs(str(SWARM_DIR))
            free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
            if free_gb < MIN_FREE_GB:
                relog("⏸️", "清理后磁盘仍不足（%.1f GB），标记暂停", free_gb)
                return {"free_gb": free_gb, "paused": True}

        return {"free_gb": free_gb, "paused": False}
    except Exception as e:
        relog("❌", "磁盘检查失败: %s", e)
        return {"free_gb": -1, "paused": False}




def check_cost_over_budget() -> Optional[str]:
    """检查当日 API 花费是否超预算。优先从 cost_tracker_db SQLite 读取。"""
    try:
        from src.infra.cost_tracker_db import get_today_spent  # type: ignore

        dollar_spent = get_today_spent()
    except ImportError:
        state = load_state()
        budget = state.get("daily_budget", {})
        dollar_spent = budget.get("dollar_spent_today", 0)

    state = load_state()
    dollar_limit = state.get("daily_budget", {}).get("dollar_limit", 5.0)

    if dollar_spent >= dollar_limit * 0.9:
        warning = f"当日花费 ${dollar_spent:.2f} / 限额 ${dollar_limit:.2f}，接近橙色模式"
        relog("💰", warning)
        return warning

    relog("💰", "当日花费 $%.2f / $%.2f", dollar_spent, dollar_limit)
    return None


GIT_TIMEOUT = 60  # git 命令超时（秒）
