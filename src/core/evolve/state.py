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
from src.core.evolve.config_ext import PID_FILE
LOCK_FILE = PID_FILE.with_suffix(".lock")

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


def acquire_pid_file() -> bool:
    """获取 PID 文件锁（含僵尸自动清理 5 分钟超时）。"""
    if not HAS_FCNTL:
        return True  # 非 Linux 跳过
    try:
        pid_fd = PID_FILE.open("w")
        fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_fd.write(str(os.getpid()))
        pid_fd.flush()
        return True
    except (IOError, BlockingIOError):
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                try:
                    os.kill(old_pid, 0)  # 检查进程是否存在
                    relog("⚠️", "PID 文件锁被占用（pid=%d），跳过", old_pid)
                    return False
                except OSError:
                    relog("🧟", "清理僵尸 PID 锁（pid=%d）", old_pid)
                    PID_FILE.unlink(missing_ok=True)
                    return acquire_pid_file()
            except (ValueError, OSError):
                PID_FILE.unlink(missing_ok=True)
                return acquire_pid_file()
        return False


def release_pid_file():
    """释放 PID 文件锁。"""
    if HAS_FCNTL and PID_FILE.exists():
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass




def load_state() -> dict:
    """加载 state.json。"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    """保存 state.json（原子写入）。"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


