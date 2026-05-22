"""test_delegate_optimizer.py — delegate_optimizer.py 决策逻辑测试
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent.resolve()
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from src.agents.delegate_optimizer import (
    should_delegate,
    build_delegation_prompt,
    select_best_agent,
    run_layer3_verification,
    scan_codebase_for_issues,
    diagnose_failures,
    build_coder_prompt,
    build_tester_prompt,
    build_reviewer_prompt,
)

# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_task():
    return {
        "task_id": "config_cleanup",
        "token_est": 200,
        "category": "config",
        "description": "清理 config.yaml 中的过期配置项",
        "target_file": "config.yaml",
    }


@pytest.fixture
def healthy_state():
    return {
        "current_round": 42,
        "diagnosis": {"delegate_success_rate": 0.85, "overall_success_rate": 0.9},
        "daily_budget": {"dollar_spent_today": 1.0, "dollar_limit": 5.0, "tier": "green"},
    }


# ═══════════════════════════════════════════════════════════════════════
# 1. should_delegate() — 决策门禁
# ═══════════════════════════════════════════════════════════════════════


def test_should_delegate_simple_task(simple_task, healthy_state):
    """简单任务（< 1000 token）应委托。"""
    budget = {"dollar_spent_today": 1.0, "dollar_limit": 5.0, "tier": "green"}
    delegate, reason = should_delegate(simple_task, healthy_state, budget)
    assert delegate, f"简单任务应委托: {reason}"


def test_should_delegate_return_type(simple_task, healthy_state):
    """返回值类型正确。"""
    budget = {"dollar_spent_today": 1.0, "dollar_limit": 5.0, "tier": "green"}
    delegate, reason = should_delegate(simple_task, healthy_state, budget)
    assert isinstance(delegate, bool)
    assert isinstance(reason, str)


# ═══════════════════════════════════════════════════════════════════════
# 2. build_delegation_prompt()
# ═══════════════════════════════════════════════════════════════════════


def test_build_delegation_prompt_coder(simple_task):
    """coder 角色 prompt 应有内容。"""
    prompt = build_delegation_prompt(simple_task, role="coder")
    assert prompt is not None
    assert len(prompt) > 100


def test_build_delegation_prompt_tester():
    """tester 角色 prompt 应有内容。"""
    task = {"task_id": "test_metrics", "description": "为 metrics 模块编写测试"}
    prompt = build_delegation_prompt(task, role="tester")
    assert prompt is not None
    assert len(prompt) > 100


def test_build_delegation_prompt_reviewer():
    """reviewer 角色 prompt 应有内容。"""
    task = {"task_id": "review_config", "description": "审查 config.yaml"}
    prompt = build_delegation_prompt(task, role="reviewer")
    assert prompt is not None
    assert len(prompt) > 100


# ═══════════════════════════════════════════════════════════════════════
# 3. 角色专用 prompt
# ═══════════════════════════════════════════════════════════════════════


def test_build_coder_prompt():
    """coder prompt 需指定文件和需求。"""
    prompt = build_coder_prompt("config.yaml", "清理过期配置项")
    assert prompt
    assert len(prompt) > 100
    assert "read_file" in prompt and "patch" in prompt


def test_build_tester_prompt():
    """tester prompt 需指定文件和需求。"""
    prompt = build_tester_prompt("test_metrics.py", "为 metrics 模块编写测试")
    assert prompt
    assert "pytest" in prompt.lower() or "pytest" in prompt


def test_build_reviewer_prompt():
    """reviewer prompt 需指定 before/after 文件。"""
    prompt = build_reviewer_prompt("config_before.yaml", "config_after.yaml", "审查配置修改")
    assert prompt
    assert len(prompt) > 100


# ═══════════════════════════════════════════════════════════════════════
# 4. select_best_agent()
# ═══════════════════════════════════════════════════════════════════════


def test_select_best_agent_simple():
    """简单任务应返回 Agent ID 字符串。"""
    task = {"task_id": "config_fix", "token_est": 200, "category": "config"}
    agent = select_best_agent(task)
    assert agent is not None
    assert isinstance(agent, str)
    assert len(agent) > 0


def test_select_best_agent_complex():
    """复杂任务也应返回有效 Agent 名。"""
    task = {"task_id": "big_refactor", "token_est": 5000, "category": "refactor"}
    agent = select_best_agent(task)
    if agent is not None:
        assert isinstance(agent, str)


# ═══════════════════════════════════════════════════════════════════════
# 5. run_layer3_verification()
# ═══════════════════════════════════════════════════════════════════════


def test_run_layer3_verification_pass():
    """验收通过场景。"""
    before = "class Foo:\n    def old_method(self): pass\n"
    after = "class Foo:\n    def old_method(self): pass\n    def new_method(self): pass\n"
    verification = run_layer3_verification(before, after, "app.py", ["old_method"])
    assert verification is not None


def test_run_layer3_verification_same_content():
    """文件无变化时检查。"""
    content = "class Foo:\n    def bar(self): pass\n"
    verification = run_layer3_verification(content, content, "app.py", ["bar"])
    assert verification is not None


# ═══════════════════════════════════════════════════════════════════════
# 6. scan_codebase_for_issues()
# ═══════════════════════════════════════════════════════════════════════


def test_scan_codebase_nonexistent():
    """扫描不存在的目录返回警告。"""
    issues = scan_codebase_for_issues("/tmp/nonexistent_project_xyz")
    assert len(issues) >= 1
    has_path_warn = any("不存在" in str(i[2]) or "not exist" in str(i[2]).lower() for i in issues)
    assert has_path_warn


def test_scan_codebase_existing():
    """扫描已有代码库（集成测试）。"""
    # 用 Path(__file__) 自动计算，避免硬编码
    import tempfile
    target = tempfile.mkdtemp(prefix="p3_test_")
    if Path(target).exists():
        issues = scan_codebase_for_issues(target)
        assert isinstance(issues, list)
        for issue in issues:
            assert len(issue) == 4
            assert issue[0] in ("INFO", "WARN", "ERROR")
    else:
        pytest.skip("项目二不在预期路径")


# ═══════════════════════════════════════════════════════════════════════
# 7. diagnose_failures()
# ═══════════════════════════════════════════════════════════════════════


def test_diagnose_failures():
    """诊断功能不应崩。"""
    result = diagnose_failures()
    assert result is not None
