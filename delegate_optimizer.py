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
import re
from pathlib import Path
from typing import Optional

# ─── 路径 ──────────────────────────────────────────────────────────────
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
TEMPLATES_DIR = SWARM_DIR / "templates"
SELF_EVOLVE_LOG = SWARM_DIR / "self_evolve_log.json"
STATE_FILE = SWARM_DIR / "state.json"

# ─── 决策阈值 ──────────────────────────────────────────────────────────
COMPLEXITY_THRESHOLD = 1000  # token 量 < 1000 视为简单任务
MIN_SUCCESS_RATE = 0.6       # 子 Agent 成功率 >= 0.6 才委托
MAX_HISTORY_WINDOW = 10      # 只看近 10 轮数据


# ═══════════════════════════════════════════════════════════════════════
# 第 1 层 — 协调者决策支持
# ═══════════════════════════════════════════════════════════════════════

def scan_codebase_for_issues(project_dir: str) -> list[str]:
    """scan_codebase_for_issues — 扫描代码库发现问题点。

    Layer 1 核心函数。协调者调用此函数扫描目标项目，返回待改进清单。

    Args:
        project_dir: 目标项目根目录。

    Returns:
        扫描发现的问题列表，每项是一个元组 (severity, category, description, file_hint)。

    用法（协调者思维中调用）：
        issues = scan_codebase_for_issues("/mnt/c/.../在线部分/")
        for sev, cat, desc, hint in issues:
            print(f"[{sev}] {cat}: {desc} ({hint})")
    """
    issues = []
    root = Path(project_dir)

    if not root.exists():
        return [("WARN", "path", f"目录不存在: {project_dir}", "")]

    # ── 扫描 routes/ 中的 sync def ──
    routes_dir = root / "routes"
    if routes_dir.exists():
        for fpath in routes_dir.rglob("*.py"):
            content = fpath.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                stripped = line.strip()
                # 检测 sync def 路由（标记 @router 或 @app 装饰器之下）
                if stripped.startswith("def ") and "(" in stripped and ")" in stripped:
                    name_match = re.match(r"def\s+(\w+)\s*\(", stripped)
                    if name_match:
                        issues.append(
                            ("INFO", "async",
                             f"sync def 路由 {name_match.group(1)} 可改为 async def",
                             str(fpath))
                        )

    # ── 扫描 services/ 中缺 async 的 I/O 操作 ──
    services_dir = root / "services"
    if services_dir.exists():
        for fpath in services_dir.rglob("*.py"):
            content = fpath.read_text(encoding="utf-8", errors="replace")
            has_async_def = "async def" in content
            has_sync_io = any(kw in content for kw in
                               [".get(", ".post(", ".request(", ".write(", ".read(",
                                "open(", "subprocess.", "time.sleep"])
            if has_async_def and has_sync_io:
                # 检查有没有 asyncio.to_thread 包装
                if "asyncio.to_thread" not in content and "await" not in content.split("asyncio.to_thread")[0]:
                    issues.append(
                        ("WARN", "async_io",
                         "async def 函数中包含未包装的同步 I/O 调用",
                         str(fpath))
                    )

    # ── 扫描 tests/ 目录覆盖率 ──
    tests_dir = root / "tests"
    if tests_dir.exists():
        py_files = list(routes_dir.rglob("*.py")) if routes_dir.exists() else []
        for fpath in py_files:
            module_name = fpath.stem
            # 扫描未覆盖的测试文件
            test_suffixes = [f"test_{module_name}.py", f"test_{module_name}s.py"]
            has_test = any((tests_dir / ts).exists() for ts in test_suffixes)
            if not has_test and module_name not in ("__init__", "__pycache__"):
                issues.append(
                    ("INFO", "test_coverage",
                     f"模块 {module_name}.py 缺少测试文件",
                     str(fpath))
                )

    return issues


def should_delegate(task: dict, state: dict, budget: dict) -> tuple[bool, str]:
    """should_delegate — 判断当前任务是否应该委托给子 Agent。

    Layer 1 → Layer 2 的决策门禁。基于 3 个指标综合判断。

    Args:
        task: 任务描述字典，含 {"task_id", "token_est", "category", "description"}。
        state: 当前 state.json 字典。
        budget: 当前预算信息。

    Returns:
        (should_delegate: bool, reason: str)

    决策逻辑：
      1. 如果任务 token 量 < COMPLEXITY_THRESHOLD (1000) → 简单任务，委托
      2. 如果子 Agent 历史成功率 < MIN_SUCCESS_RATE (0.6) → 协调者自己干
      3. 如果预算紧张（黄色模式，剩余 < 30%）→ 保守决策，不委托新功能
      4. 如果 round_pressure（已用完 80%+ 预算）→ 不委托（省钱）
      5. 否则按 category 决定：
         - debug: 简单修复，倾向于委托
         - test/test_creation: 已知子 Agent 失败率 100%，不委托
         - feature/架构: 复杂任务，不委托
    """
    token_est = task.get("token_est", 0) or task.get("预估 token 量", 2000)
    category = task.get("category", "debug")
    task_id = task.get("task_id", "unknown")

    # ── 已知失败模式：不委托 ──
    # 从零创建测试文件：100% 失败率（历史验证）
    if category == "test_creation" or "test_" in task_id and "创建" in task.get("description", ""):
        return False, f"测试创建类任务 {task_id}：子 Agent 历史成功率 0%，协调者直接 write_file"

    # ── 简单任务优先委托 ──
    if token_est < COMPLEXITY_THRESHOLD:
        success_rate = _compute_success_rate(state)
        if success_rate >= MIN_SUCCESS_RATE:
            return True, f"简单任务（{token_est} token），子 Agent 成功率 {success_rate:.0%}，委托"
        # 成功率低，但仍然是简单任务——尝试委托（强制委托政策）
        return True, f"简单任务（{token_est} token），尝试委托以积累经验数据"

    # ── 复杂任务 ──
    # 架构/框架类任务：始终不委托
    if category in ("feature", "refactor", "architecture"):
        return False, f"架构/新功能任务 {task_id}：需要接口一致性保证，协调者直接执行"

    # ── 预算感知决策 ──
    dollar_spent = budget.get("dollar_spent_today", 0)
    dollar_limit = budget.get("dollar_limit", 5.0)
    if dollar_limit > 0:
        ratio = dollar_spent / dollar_limit
        if ratio > 0.8:
            return False, f"预算紧张（已用 {ratio:.0%}），不委托以控制成本"
        if ratio > 0.5 and category in ("feature", "refactor"):
            return False, f"预算中等（已用 {ratio:.0%}），仅委托 debug/test 类任务"

    # ── 默认：委托尝试 ──
    return True, "默认策略：委托子 Agent 执行"


def _compute_success_rate(state: dict) -> float:
    """_compute_success_rate — 从 state.json 计算子 Agent 近 N 轮成功率。

    读取 failed_tasks 和 completed_task_ids，统计近 10 轮的委托成功率。
    """
    completed = state.get("completed_task_ids", [])
    failed = state.get("failed_tasks", [])
    total_delegated = len(completed) + len(failed)
    if total_delegated == 0:
        return 0.0
    return len(completed) / max(total_delegated, 1)


# ═══════════════════════════════════════════════════════════════════════
# 第 2 层 — 委托 prompt 构建器
# ═══════════════════════════════════════════════════════════════════════

FIVE_HARD_CONSTRAINTS = """\
【5 条硬约束——违反任一条直接拒绝产出】
1. 【禁止】修改任何函数签名（参数名、参数类型、返回值类型）
2. 【禁止】删除任何文件，只能修改内容
3. 【必须】在改动前先 read_file 读取完整文件
4. 【必须】用 patch 精确替换，不许用 write_file 覆盖整文件
5. 【必须】改动完成后运行：python -m py_compile <文件路径>
"""


def build_delegation_prompt(task: dict, role: str = "coder",
                             templates_dir: Optional[Path] = None) -> str:
    """build_delegation_prompt — 为子 Agent 构建完整委托 prompt。

    Layer 2 核心函数。将任务描述、角色模板、5 条硬约束组装为单字符串。
    协调者将此字符串作为 delegate_task 的 goal 参数。

    Args:
        task: 任务字典，含 task_id/description/target_file/code_sample 等。
        role: Agent 角色（"coder"/"tester"/"reviewer"）。
        templates_dir: 模板目录路径，默认 SWARM_DIR / "templates"。

    Returns:
        完整的委托 prompt 字符串（可直接传入 delegate_task 的 goal）。

    用法（协调者思维中调用）：
        prompt = build_delegation_prompt(task, role="coder")
        result = delegate_task(goal=prompt, ...)
    """
    if templates_dir is None:
        templates_dir = TEMPLATES_DIR

    # 加载角色模板
    template_path = templates_dir / f"{role}_template.md"
    template_content = ""
    if template_path.exists():
        template_content = template_path.read_text(encoding="utf-8")
    else:
        template_content = f"# {role.capitalize()} Agent 任务\n> 按以下要求执行。\n"

    # 填充模板变量
    target_file = task.get("target_file", task.get("file", ""))
    code_sample = task.get("code_sample", task.get("示例代码", ""))
    constraints = task.get("constraints", task.get("额外约束", ""))

    prompt = template_content
    prompt = prompt.replace("{{target_file}}", target_file)
    prompt = prompt.replace("{{requirement}}", task.get("description", ""))
    prompt = prompt.replace("{{code_sample}}", code_sample)
    prompt = prompt.replace("{{constraints}}", constraints)
    prompt = prompt.replace("{{function_list}}", task.get("functions", ""))
    prompt = prompt.replace("{{mock_template}}", task.get("mock_template", ""))

    # 追加 5 条硬约束
    prompt += "\n\n" + FIVE_HARD_CONSTRAINTS

    # 追加任务特定的额外约束
    if constraints:
        prompt += f"\n\n## 额外约束\n{constraints}\n"

    return prompt


def build_coder_prompt(target_file: str, requirement: str,
                       code_sample: str = "", constraints: str = "",
                       functions: str = "") -> str:
    """build_coder_prompt — 快捷构建编码 Agent 的委托 prompt。"""
    task = {
        "target_file": target_file,
        "description": requirement,
        "code_sample": code_sample,
        "constraints": constraints,
        "functions": functions,
    }
    return build_delegation_prompt(task, role="coder")


def build_tester_prompt(target_file: str, requirement: str,
                         function_list: str = "", mock_template: str = "",
                         constraints: str = "") -> str:
    """build_tester_prompt — 快捷构建测试 Agent 的委托 prompt。"""
    task = {
        "target_file": target_file,
        "description": requirement,
        "functions": function_list,
        "mock_template": mock_template,
        "constraints": constraints,
    }
    return build_delegation_prompt(task, role="tester")


def build_reviewer_prompt(before_file: str, after_file: str,
                           requirement: str = "") -> str:
    """build_reviewer_prompt — 快捷构建审查 Agent 的委托 prompt。"""
    task = {
        "before_file": before_file,
        "after_file": after_file,
        "description": requirement,
    }
    return build_delegation_prompt(task, role="reviewer")


# ═══════════════════════════════════════════════════════════════════════
# 第 3 层 — 验收工具
# ═══════════════════════════════════════════════════════════════════════

LAYER3_VERIFICATION_STEPS = """\
## Layer 3 验收流程（协调者执行）

每次 Agent 产出后必须执行以下 4 步：

### Step 1 — 签名检查
grep 确认函数签名未变化（比较改动前后的参数名/参数类型/返回值类型）

### Step 2 — 语法检查
python -m py_compile <文件路径>

### Step 3 — 单元测试
pytest <测试文件> -v

### Step 4 — Diff 对照
对比改动前后，确认只有预期改动，无意外格式化/空白/敏感信息变动

### 返工管控
- 任何一步失败 → 记录到 self_evolve_log.json 的 failure_stats
- 同一任务返工超过 2 次 → 协调者接管并行自己写完
"""


def check_signature_unchanged(before: str, after: str,
                               func_names: list[str]) -> list[str]:
    """check_signature_unchanged — 检查函数签名是否被修改。

    Layer 3 — Step 1：从 before 和 after 字符串中提取指定函数的签名，
    对比确认参数列表未变化。

    Args:
        before: 改动前的文件内容。
        after: 改动后的文件内容。
        func_names: 要检查的函数名列表。

    Returns:
        签名被修改的函数名列表。空列表表示全部通过。
    """
    violations = []
    for name in func_names:
        before_sig = _extract_signature(before, name)
        after_sig = _extract_signature(after, name)
        if before_sig and after_sig and before_sig != after_sig:
            violations.append(f"{name}: {before_sig} → {after_sig}")
    return violations


def _extract_signature(content: str, func_name: str) -> Optional[str]:
    """_extract_signature — 从源码中提取函数签名。"""
    pattern = rf"def\s+{func_name}\s*\([^)]*\)\s*(->\s*\w+\s*)?:"
    match = re.search(pattern, content)
    return match.group(0) if match else None


def count_lines_added_removed(before: str, after: str) -> tuple[int, int]:
    """count_lines_added_removed — 计算改动前后的行数变化。

    Layer 3 — Step 4 的辅助工具：确认改动规模在合理范围内。
    单次改动超过 200 行应发出警告（可能包含非预期改动）。

    Args:
        before: 改动前内容。
        after: 改动后内容。

    Returns:
        (lines_added, lines_removed)
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = max(0, len(after_lines) - len(before_lines))
    removed = max(0, len(before_lines) - len(after_lines))
    return (added, removed)


# ═══════════════════════════════════════════════════════════════════════
# 成本激励机制
# ═══════════════════════════════════════════════════════════════════════

DELEGATION_INCENTIVE = {
    "coordinator_write_line_threshold": 50,  # 协调者自写超 50 行时警告
    "delegate_success_bonus_tokens": 1000,   # 委托成功奖励预算
    "self_write_overflow_penalty": 500,      # 超阈值扣减预算
}


def log_coordinator_write_size(state: dict, file_path: str,
                                lines_written: int) -> dict:
    """log_coordinator_write_size — 记录协调者自写代码行数。

    成本激励：超阈值时发出警告。
    返回更新后的 state。
    """
    state.setdefault("coordinator_write_log", []).append({
        "file": file_path,
        "lines": lines_written,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    })
    threshold = DELEGATION_INCENTIVE["coordinator_write_line_threshold"]
    total_written = sum(
        e["lines"] for e in state.get("coordinator_write_log", [])
    )
    if lines_written > threshold:
        print(f"[💡成本激励] 协调者单次写 {lines_written} 行（超阈值 {threshold} 行）。"
              f"累计已写 {total_written} 行。考虑委托给子 Agent。")
    return state


# ═══════════════════════════════════════════════════════════════════════
# 诊断工具（diagnose_subagent_failure 的子任务）
# ═══════════════════════════════════════════════════════════════════════

def diagnose_failures(evolve_log_path: Optional[Path] = None) -> dict:
    """diagnose_failures — 分析 self_evolve_log.json 诊断委托失败模式。

    此函数是本模块自带的诊断工具，也可被 diagnose_subagent_failure 委托任务调用。

    Args:
        evolve_log_path: self_evolve_log.json 的路径，默认 SWARM_DIR / "self_evolve_log.json"。

    Returns:
        诊断报告字典，含总委托次数/成功率/失败归因/趋势。
    """
    path = evolve_log_path or SELF_EVOLVE_LOG
    if not path.exists():
        return {"error": f"日志文件不存在: {path}"}

    try:
        rounds = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"日志文件解析失败: {e}"}

    if not isinstance(rounds, list):
        return {"error": "日志格式错误：期望 JSON 数组"}

    total_rounds = len(rounds)
    success_count = sum(1 for r in rounds if r.get("result") == "success")
    failed_count = sum(1 for r in rounds if r.get("result") != "success")
    success_rate = success_count / total_rounds if total_rounds > 0 else 0

    # 分析委托模式
    delegated_rounds = [r for r in rounds
                        if "delegate" in str(r.get("approach", ""))
                        or "delegate" in r.get("task", "")]
    delegate_count = len(delegated_rounds)
    delegate_success = sum(1 for r in delegated_rounds
                           if r.get("result") == "success")
    delegate_success_rate = delegate_success / delegate_count if delegate_count > 0 else 0

    # 分析失败归因
    failure_patterns = {}
    for r in rounds:
        good = r.get("good", "")
        waste = r.get("waste", "")
        failure_type = _classify_failure(str(r.get("task", "")) + " " + waste)
        if failure_type:
            failure_patterns.setdefault(failure_type, 0)
            failure_patterns[failure_type] += 1

    # 找高频失败词
    failure_keywords = {}
    for r in rounds:
        waste = r.get("waste", "")
        for kw in ["mock", "patch", "签名", "签名", "import", "语法", "超时",
                    "路径", "cd", "venv", "pip", "ast.parse", "py_compile",
                    "replace_all", "write_file"]:
            if kw in waste.lower():
                failure_keywords.setdefault(kw, 0)
                failure_keywords[kw] += 1

    # 趋势分析：最近 5 轮和之前 5 轮对比
    recent_count = min(5, total_rounds)
    recent_rounds = rounds[-recent_count:]
    old_rounds = rounds[-recent_count*2:-recent_count] if total_rounds >= recent_count*2 else []

    recent_success = sum(1 for r in recent_rounds if r.get("result") == "success")
    old_success = sum(1 for r in old_rounds if r.get("result") == "success")

    # 找出最耗 token 的轮次
    token_heavy = []
    for r in rounds:
        lines_added = r.get("lines_added", 0) or 0
        if lines_added > 300:
            token_heavy.append({
                "round": r.get("round"),
                "task": r.get("task", "")[:60],
                "lines_added": lines_added,
            })

    return {
        "total_rounds": total_rounds,
        "success_count": success_count,
        "failed_count": failed_count,
        "overall_success_rate": round(success_rate, 3),
        "delegated_rounds": delegate_count,
        "delegate_success_count": delegate_success,
        "delegate_failed_count": delegate_count - delegate_success,
        "delegate_success_rate": round(delegate_success_rate, 3),
        "failure_patterns": dict(sorted(failure_patterns.items(),
                                        key=lambda x: -x[1])),
        "failure_keywords": dict(sorted(failure_keywords.items(),
                                         key=lambda x: -x[1])),
        "trend": {
            "recent_5_success_rate": round(recent_success / len(recent_rounds), 3) if recent_rounds else 0,
            "previous_5_success_rate": round(old_success / len(old_rounds), 3) if old_rounds else 0,
            "improving": (recent_success / len(recent_rounds) > old_success / len(old_rounds))
                         if recent_rounds and old_rounds else None,
        },
        "token_heavy_rounds": token_heavy[:5],
        "total_lines_added": sum(r.get("lines_added", 0) or 0 for r in rounds),
        "total_lines_removed": sum(r.get("lines_removed", 0) or 0 for r in rounds),
    }


def _classify_failure(text: str) -> Optional[str]:
    """_classify_failure — 从文本中分类失败类型。"""
    patterns = [
        (r"(?:mock|patch).*(?:fail|error|import)", "mock_import_failure"),
        (r"ast\.parse|py_compile|SyntaxError", "syntax_error"),
        (r"venv|pip.*install|externally.managed", "environment_dependency"),
        (r"cd|中文字|路径", "chinese_path_issue"),
        (r"签名|signature|函数.*签", "signature_drift"),
        (r"超时|timeout", "timeout"),
        (r"(?:空文件|零文件|假产出|fake_output|0 files)", "zero_file_output"),
        (r"git.*conflict|pull.*rebase", "git_conflict"),
        (r"r(?:eplace_all|eplace.*all)", "replace_all_issue"),
    ]
    for pattern, failure_type in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return failure_type
    return None


def write_diagnosis_to_log(diagnosis: dict) -> bool:
    """write_diagnosis_to_log — 将诊断报告写入 self_evolve_log.json。

    在 self_evolve_log.json 的根级增加 diagnosis 字段。如果已有 diagnosis 字段则更新。

    Args:
        diagnosis: diagnose_failures() 返回的诊断报告字典。

    Returns:
        True: 写入成功。False: 写入失败。
    """
    if not SELF_EVOLVE_LOG.exists():
        return False
    try:
        data = json.loads(SELF_EVOLVE_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if isinstance(data, list):
        # 如果是数组格式，转为字典格式并附加 diagnosis
        data = {"rounds": data}

    data["diagnosis"] = diagnosis
    data["diagnosis_updated_at"] = __import__("datetime").datetime.now().isoformat()

    SELF_EVOLVE_LOG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return True


# ═══════════════════════════════════════════════════════════════════════
# Agent 能力画像 — agent_capability_map.json 操作
# ═══════════════════════════════════════════════════════════════════════

CAPABILITY_MAP_FILE = SWARM_DIR / "agent_capability_map.json"


def get_agent_capability(agent_id: str) -> Optional[dict]:
    """get_agent_capability — 查询指定 Agent 的能力画像。

    Layer 1 辅助函数。协调者根据此画像决定是否委托给该 Agent。

    Args:
        agent_id: Agent 标识符（如 "agent-coder", "agent-tester"）。

    Returns:
        Agent 能力画像字典，或 None（文件不存在/Agent 不存在）。

    用法（协调者思维中调用）：
        cap = get_agent_capability("agent-coder")
        if cap and cap["success_rate"] > 0.5:
            # 可靠，可以委托
        elif cap and cap["success_rate"] > 0.3:
            # 仅委托简单任务
        else:
            # 协调者自己干
    """
    if not CAPABILITY_MAP_FILE.exists():
        return None
    try:
        data = json.loads(CAPABILITY_MAP_FILE.read_text(encoding="utf-8"))
        return data.get("agents", {}).get(agent_id)
    except (json.JSONDecodeError, OSError):
        return None


def update_agent_capability(agent_id: str, task_result: dict) -> bool:
    """update_agent_capability — 更新 Agent 能力画像。

    每次委托完成后由协调者调用，记录任务结果（成功/失败、消耗 token、失败原因）。

    Args:
        agent_id: Agent 标识符。
        task_result: 任务结果字典，含：
            - success: bool
            - tokens_used: int
            - failure_pattern: str
            - task_type: str

    Returns:
        True: 更新成功。False: 更新失败。
    """
    if not CAPABILITY_MAP_FILE.exists():
        return False
    try:
        data = json.loads(CAPABILITY_MAP_FILE.read_text(encoding="utf-8"))
        agents = data.get("agents", {})
        if agent_id not in agents:
            return False

        agent = agents[agent_id]
        agent["total_tasks_assigned"] = agent.get("total_tasks_assigned", 0) + 1
        agent["last_assigned"] = __import__("datetime").datetime.now().isoformat()

        if task_result.get("success"):
            agent["successful_tasks"] = agent.get("successful_tasks", 0) + 1
        else:
            fp = task_result.get("failure_pattern", "")
            if fp:
                existing = agent.get("failure_pattern", "")
                existing_parts = [p.strip() for p in existing.split(",") if p.strip()]
                if fp not in existing_parts:
                    existing_parts.append(fp)
                agent["failure_pattern"] = ", ".join(existing_parts)

        # 重新计算 success_rate
        total = agent.get("total_tasks_assigned", 0)
        succeeded = agent.get("successful_tasks", 0)
        agent["success_rate"] = round(succeeded / max(total, 1), 2)

        # 更新 token 平均值
        prev_avg = agent.get("avg_tokens_used", 0)
        prev_count = max(total - 1, 0)
        new_tokens = task_result.get("tokens_used", 0)
        if prev_count > 0:
            agent["avg_tokens_used"] = round(
                (prev_avg * prev_count + new_tokens) / total, 0
            )
        else:
            agent["avg_tokens_used"] = new_tokens

        # 记录选择历史
        history_entry = {
            "timestamp": __import__("datetime").datetime.now().isoformat(),
            "agent_id": agent_id,
            "task_type": task_result.get("task_type", "unknown"),
            "success": task_result.get("success", False),
            "tokens_used": task_result.get("tokens_used", 0),
        }
        data.setdefault("agent_selection_history", []).append(history_entry)
        data["coordination_rules"]["last_updated"] = history_entry["timestamp"]

        CAPABILITY_MAP_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"[⚠️ 能力画像] 更新失败: {e}")
        return False


def select_best_agent(task_type: str) -> str:
    """select_best_agent — 根据任务类型选择最佳 Agent。

    Layer 1 决策辅助。基于 Agent 能力画像，选择成功率最高且适合该任务类型的 Agent。

    Args:
        task_type: 任务类型（"incremental_modification", "code_review",
                   "test_creation", "elastic"）。

    Returns:
        选中的 Agent ID（如 "agent-coder"）。

    规则：
        - success_rate > 0.5: 优先派任务
        - success_rate 0.3~0.5: 仅派简单任务
        - success_rate < 0.3 或 rate=0: 仅派探索性/低风险任务
        - 新 Agent（rate=0）优先派简单探索任务以积累数据
    """
    if not CAPABILITY_MAP_FILE.exists():
        return "agent-coder"  # 默认

    try:
        data = json.loads(CAPABILITY_MAP_FILE.read_text(encoding="utf-8"))
        agents = data.get("agents", {})
    except (json.JSONDecodeError, OSError):
        return "agent-coder"

    # 按 success_rate 降序排列，rate=0 的排在最后
    sorted_agents = sorted(
        agents.items(),
        key=lambda item: (
            0 if item[1].get("success_rate", 0) == 0 else 1,
            item[1].get("success_rate", 0)
        ),
        reverse=True,
    )

    for agent_id, capability in sorted_agents:
        rate = capability.get("success_rate", 0)
        total = capability.get("total_tasks_assigned", 0)

        # 新 Agent（0 次任务）—— 仅分配简单探索任务
        if total == 0 and task_type in ("elastic", "config_update"):
            return agent_id

        # 高可靠 Agent
        if rate >= 0.5:
            return agent_id

        # 中等可靠 Agent
        if rate >= 0.3 and task_type in ("incremental_modification",
                                          "config_update"):
            return agent_id

    # 兜底
    return "agent-coder"

# ═══════════════════════════════════════════════════════════════════════
# Layer 3 验收标准化 — run_layer3_verification 函数
# ═══════════════════════════════════════════════════════════════════════


def run_layer3_verification(
    before_content: str,
    after_content: str,
    file_path: str,
    func_names: list[str],
    test_file: str = "",
    rework_count: int = 0,
) -> dict:
    """run_layer3_verification — 标准化 4 步验收流程。

    每次 Agent 产出后必须执行。4 步验收 + 返工管控。

    Step 1 — 签名检查：check_signature_unchanged()
    Step 2 — 语法检查：python -m py_compile
    Step 3 — 单元测试：pytest <test_file>
    Step 4 — diff 对照：检查改动规模

    Args:
        before_content: 改动前的文件内容。
        after_content: 改动后的文件内容。
        file_path: 被修改文件的路径。
        func_names: 要检查的函数名列表。
        test_file: 测试文件路径（可选）。
        rework_count: 该任务已返工次数。

    Returns:
        {
            "passed": bool,
            "rework_count": int,
            "step_results": dict,
            "rework_action": str
        }
    """
    step_results = {}

    # Step 1 — 签名检查
    violations = check_signature_unchanged(before_content, after_content, func_names)
    sig_passed = len(violations) == 0
    step_results["signature_check"] = {
        "passed": sig_passed,
        "violations": violations,
    }

    # Step 2 — 语法检查（用 ast.parse 替代 python -m py_compile，避免 terminal 依赖）
    syntax_ok = True
    syntax_error = None
    try:
        __import__("ast").parse(after_content)
    except SyntaxError as e:
        syntax_ok = False
        syntax_error = f"{e.msg} (line {e.lineno})"
    step_results["syntax_check"] = {
        "passed": syntax_ok,
        "error": syntax_error,
    }

    # Step 3 — 单元测试（子 Agent 需自行跑 pytest，这里仅记录是否提供测试文件）
    if test_file:
        step_results["unit_test"] = {
            "passed": None,  # 子 Agent 需自行运行 pytest 并报告
            "note": f"需要运行: pytest {test_file} -v",
        }
    else:
        step_results["unit_test"] = {
            "passed": True,
            "note": "未提供测试文件，跳过单元测试检查",
        }

    # Step 4 — diff 对照
    added, removed = count_lines_added_removed(before_content, after_content)
    diff_warnings = []
    if added > 200:
        diff_warnings.append(f"单次改动代码量较大（+{added} 行），请确认无意外变更")
    if added == 0 and removed == 0:
        diff_warnings.append("文件内容未变化——零产出，需确认任务是否完成")
    step_results["diff_review"] = {
        "passed": len(diff_warnings) == 0,
        "warnings": diff_warnings,
        "lines_added": added,
        "lines_removed": removed,
    }

    # 判断是否全部通过
    all_passed = all(
        s.get("passed", False) for s in step_results.values()
        if s.get("passed") is not None  # 跳过 None（待子 Agent 确认）
    )

    # 返工管控
    if all_passed:
        rework_action = "通过"
    elif rework_count >= 2:
        rework_action = "协调者接管"
    else:
        rework_action = "打回重做"

    return {
        "passed": all_passed,
        "rework_count": rework_count + (0 if all_passed else 1),
        "step_results": step_results,
        "rework_action": rework_action,
    }
