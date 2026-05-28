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


def _get_config() -> dict:
    """从 config.yaml 读取完整配置（无 yaml 依赖）。"""
    cfg_path = SWARM_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        text = cfg_path.read_text(encoding="utf-8")
        result = {}
        _parse_yaml_top_level(text, result)
        return result
    except Exception:
        return {}


try:
    from src.infra.audit_trail import audit_log
except ImportError:
    def audit_log(*args, **kwargs):
        pass

try:
    from src.infra.safety_interlock import guard_git_push
except ImportError:
    def guard_git_push(*args, **kwargs):
        return True

STATE_FILE = SWARM_DIR / "data" / "state.json"
PID_FILE = SWARM_DIR / ".self_evolve_round.pid"
TODO_FILE = SWARM_DIR / "docs" / "TODO.md"
LOG_FILE = SWARM_DIR / "logs" / "self_evolve.log"

OPT_DIMENSIONS = [
    "security",          # 安全：SQL注入/命令注入/密钥泄露/XSS
    "performance",       # 性能：N+1查询/sync阻塞/内存泄漏
    "asyncification",   # 异步化：sync-async边界问题
    "quality",           # 代码质量：未用import/过深嵌套/硬编码
    "testing",           # 测试：缺失测试/覆盖不足
    "architecture",     # 架构：循环依赖/上帝文件/紧耦合
    "documentation",    # 文档：缺失docstring/无type hint
    "configuration",     # 配置：硬编码配置/不一致配置
    "deadcode",         # 死代码：未调用函数/不可达文件
]
MAX_OPTIMIZATIONS_PER_ROUND = 10
OPT_CONFIDENCE_THRESHOLD = 0.75
