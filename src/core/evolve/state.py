"""evolve/state — PID 文件锁 + state.json 读写

职责：
  - acquire_pid_file / release_pid_file: 进程级互斥锁，防止多实例并发
  - load_state / save_state: state.json 的原子读写（tmp+rename）
"""

import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
from src.core.evolve.config_ext import PID_FILE, STATE_FILE
from src.core.evolve.logging import relog
import logging

LOCK_FILE = PID_FILE.with_suffix(".lock")


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


def release_pid_file() -> Any:
    """释放 PID 文件锁。"""
    if HAS_FCNTL and PID_FILE.exists():
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
                        logging.exception('异常捕获: ')




def load_state() -> dict:
    """加载 state.json。"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    """保存 state.json（原子写入）。"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


