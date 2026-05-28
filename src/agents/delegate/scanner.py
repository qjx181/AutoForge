"""delegate_optimizer.py — 分层委托策略优化器（Layer 1/2/3）

作用：
  为协调者提供"该不该委托"的决策框架，以及构建委托 prompt 的工具。
  实现 Layer 1（协调者自己干）、Layer 2（委托子 Agent）、Layer 3（验收）三层流程。

原理：
  协调者（Hermes Agent）每轮读取 TODO → 选择任务 → 调用 should_delegate() 决策 →
  由 Layer 1（自己写）或 Layer 2（delegate_task）执行 → 验收走 Layer 3 四步验证。

依赖：
  - agent_roles.py（角色定义）
  - templates/coder_template.md, tester_template.md, reviewer_template.md（模板文件）

用法（在 Hermes Agent 思维中调用，非 self_evolve_round.py 脚本）:
  from delegate_optimizer import *
  if should_delegate(task, state):
      prompt = build_delegation_prompt(task, role="coder")
      # delegate_task(goal=prompt, ...)
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import os
import re
from pathlib import Path
from typing import Optional
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
TEMPLATES_DIR = SWARM_DIR / "templates"
SELF_EVOLVE_LOG = SWARM_DIR / "data" / "self_evolve_log.json"
STATE_FILE = SWARM_DIR / "data" / "state.json"
CAPABILITY_MAP_FILE = SWARM_DIR / "data" / "agent_capability_map.json"

COMPLEXITY_THRESHOLD = 1000  # token 量 < 1000 视为简单任务
MIN_SUCCESS_RATE = 0.6       # 子 Agent 成功率 >= 0.6 才委托
MAX_HISTORY_WINDOW = 10      # 只看近 10 轮数据



def _scan_routes_for_sync_defs(routes_dir: Path, issues: list) -> None:
    """扫描 routes/ 中的 sync def 路由。"""
    if not routes_dir.exists():
        return
    for fpath in routes_dir.rglob("*.py"):
        content = fpath.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("def ") and "(" in stripped and ")" in stripped:
                name_match = re.match(r"def\s+(\w+)\s*\(", stripped)
                if name_match:
                    issues.append(
                        ("INFO", "async",
                         f"sync def 路由 {name_match.group(1)} 可改为 async def",
                         str(fpath))
                    )


def _scan_services_for_sync_io(services_dir: Path, issues: list) -> None:
    """扫描 services/ 中缺 async 的 I/O 操作。"""
    if not services_dir.exists():
        return
    for fpath in services_dir.rglob("*.py"):
        content = fpath.read_text(encoding="utf-8", errors="replace")
        has_async_def = "async def" in content
        has_sync_io = any(kw in content for kw in
                           [".get(", ".post(", ".request(", ".write(", ".read(",
                            "open(", "subprocess.", "time.sleep"])
        if has_async_def and has_sync_io:
            if "asyncio.to_thread" not in content and "await" not in content.split("asyncio.to_thread")[0]:
                issues.append(
                    ("WARN", "async_io",
                     "async def 函数中包含未包装的同步 I/O 调用",
                     str(fpath))
                )


def _scan_test_coverage(routes_dir: Path, tests_dir: Path, issues: list) -> None:
    """扫描 tests/ 目录覆盖率。"""
    if not tests_dir.exists():
        return
    py_files = list(routes_dir.rglob("*.py")) if routes_dir.exists() else []
    for fpath in py_files:
        module_name = fpath.stem
        test_suffixes = [f"test_{module_name}.py", f"test_{module_name}s.py"]
        has_test = any((tests_dir / ts).exists() for ts in test_suffixes)
        if not has_test and module_name not in ("__init__", "__pycache__"):
            issues.append(
                ("INFO", "test_coverage",
                 f"模块 {module_name}.py 缺少测试文件",
                 str(fpath))
            )


def scan_codebase_for_issues(project_dir: str) -> list[str]:
    """scan_codebase_for_issues — 扫描代码库发现问题点。

    Layer 1 核心函数。协调者调用此函数扫描目标项目，返回待改进清单。

    Args:
        project_dir: 目标项目根目录。

    Returns:
        扫描发现的问题列表，每项是一个元组 (severity, category, description, file_hint)。

    用法（协调者思维中调用）：
        issues = scan_codebase_for_issues(os.environ.get("PROJECT1_DIR", "/path/to/project"))
        for sev, cat, desc, hint in issues:
            print(f"[{sev}] {cat}: {desc} ({hint})")
    """
    issues = []
    root = Path(project_dir)

    if not root.exists():
        return [("WARN", "path", f"目录不存在: {project_dir}", "")]

    _scan_routes_for_sync_defs(root / "routes", issues)

    _scan_services_for_sync_io(root / "services", issues)

    _scan_test_coverage(root / "routes", root / "tests", issues)

    return issues
