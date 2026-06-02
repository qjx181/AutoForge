"""evolve/config_ext — 自进化系统的运行时配置常量

从 config.yaml 读取项目配置，同时定义所有模块共享的路径常量和调优参数。
不含任何业务逻辑，只做"读配置 + 暴露常量"。
"""

import os
from pathlib import Path
from typing import Any, Optional

from src.core.evolve.config import _parse_yaml_top_level

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
    def audit_log(*args, **kwargs) -> Any:
        pass

try:
    from src.infra.safety_interlock import guard_git_push
except ImportError:
    def guard_git_push(*args, **kwargs) -> Any:
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

# 磁盘/日志常量
MIN_FREE_GB = 5       # 磁盘剩余低于此值触发清理
MAX_LOG_DAYS = 7       # 日志保留天数

# 日志格式开关（CLI --json-logs 可动态开启）
_JSON_MODE = False
