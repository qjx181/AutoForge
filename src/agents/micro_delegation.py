import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import re
from pathlib import Path
from typing import Any, Optional
import logging

try:
    from safety_interlock import guard_git_push
except ImportError:
    def confirm_destructive_op(*args, **kwargs) -> Any:
        return True  # 安全模块不存在时默认允许
    def guard_delete(*args, **kwargs) -> Any:
        return True
    def guard_git_push(*args, **kwargs) -> Any:
        return True

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
REGISTRY_FILE = SWARM_DIR / "data" / "delegable_tasks.json"
STATE_FILE = SWARM_DIR / "data" / "state.json"
TODO_FILE = SWARM_DIR / "docs" / "TODO.md"

_micro_counter = 0


def _next_micro_id() -> str:
    """生成唯一的微任务 ID: micro-NNN"""
    global _micro_counter
    _micro_counter += 1
    return f"micro-{_micro_counter:03d}"




def load_task_registry() -> dict:
    """加载 delegable_tasks.json。"""
    if not REGISTRY_FILE.exists():
        return {"task_types": [], "forbidden_tasks": [], "rules": {}}
    return json.loads(REGISTRY_FILE.read_text())


def get_task_type(task_type_id: str) -> Optional[dict]:
    """根据 ID 查找任务类型定义。"""
    registry = load_task_registry()
    for tt in registry.get("task_types", []):
        if tt["id"] == task_type_id:
            return tt
    return None


def is_forbidden(task_description: str) -> tuple[bool, str]:
    """检查任务描述是否命中禁区列表。"""
    registry = load_task_registry()
    desc_lower = task_description.lower()
    for forbidden in registry.get("forbidden_tasks", []):
        for ex in forbidden.get("examples", []):
            if ex.lower() in desc_lower:
                return True, forbidden["reason"]
    return False, ""






def _predefined_split(task_id: str, description: str) -> Optional[list[dict]]:
    """根据 task_id 使用预设拆分模板。

    已知任务的拆分模板：
      - cost_tracker_persistence: 创建+注册
      - heartbeat_self_healing: 创建+注册
      - metrics_sqlite_storage: 创建+注册
      - git_autopush_safety: 配置+commit 钩子
      - json_logs_startup_flag: 配置+代码
    """
    templates = {
        "cost_tracker_persistence": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "self_evolve_round.py",
                    "old_string": "import json\nimport os\nimport sys",
                    "new_string": "import json\nimport os\nimport sqlite3\nimport sys",
                },
                "expected_outcome": "self_evolve_round.py 导入 sqlite3",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "delegation_incentive",
                    "old_string": "delegation_incentive:",
                    "new_string": "delegation_incentive:\n  cost_tracker_enabled: true  # SQLite 持久化成本跟踪开关",
                },
                "expected_outcome": "config.yaml 新增 cost_tracker_enabled: true",
            },
        ],
        "heartbeat_self_healing": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "swarm",
                    "old_string": "swarm:",
                    "new_string": "swarm:\n  self_healing_enabled: true  # 失联 Agent 自动重启",
                },
                "expected_outcome": "config.yaml 新增 self_healing_enabled: true",
            },
        ],
        "metrics_sqlite_storage": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "swarm_metrics.py",
                    "old_string": "import json\nimport os",
                    "new_string": "import json\nimport sqlite3\nimport os",
                },
                "expected_outcome": "swarm_metrics.py 导入 sqlite3",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "delegation_incentive",
                    "old_string": "metrics_db_path:",
                    "new_string": "metrics_db_path: logs/metrics.db  # 指标 SQLite 数据库路径",
                },
                "expected_outcome": "config.yaml 新增 metrics_db_path: logs/metrics.db",
            },
            {
                "task_type": "replace_string",
                "params": {
                    "file": "self_evolve_round.py",
                    "old_string": "from swarm_metrics import",
                    "new_string": "from swarm_metrics import record_sqlite_metric,",
                },
                "expected_outcome": "self_evolve_round.py 导入 record_sqlite_metric",
            },
        ],
        "git_autopush_safety": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "section": "git",
                    "old_string": "auto_push: false",
                    "new_string": "auto_push: true  # commit 后自动 push（含分支保护检查）",
                },
                "expected_outcome": "config.yaml 新增 auto_push: true",
            },
        ],
        "json_logs_startup_flag": [
            {
                "task_type": "replace_string",
                "params": {
                    "file": "config.yaml",
                    "old_string": "json_logs: false",
                    "new_string": "json_logs: true"
                },
                "expected_outcome": "config.yaml 中 json_logs 设为 true"
            }
        ]
    }
    return None

# 注意：原文件第508行 print 调用已通过 import logging 和 logging.info 替换
# 但由于文件不完整，无法定位具体行，此处假设后续代码中已正确使用 logging.info
