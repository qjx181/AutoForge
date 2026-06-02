"""conftest.py — 共享测试配置与 Fixture

为项目三：多Agent 的 pytest 测试提供：
  1. 自动 sys.path 设置（无需每个测试文件自行添加）
  2. 共享 fixture（临时目录、示例数据等）
  3. pytest 配置（忽略特定警告、标记定义）
"""

import sys
from pathlib import Path

import pytest

# ── 自动设置 sys.path ────────────────────────────────────────────────
# 所有测试文件自动获得 src/ 和项目根目录的导入权限
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"

for p in [str(SRC_DIR), str(PROJECT_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════════════════
# 共享 Fixture
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def project_dir() -> Path:
    """返回项目根目录 Path 对象"""
    return PROJECT_DIR


@pytest.fixture
def src_dir() -> Path:
    """返回 src/ 目录 Path 对象"""
    return SRC_DIR


@pytest.fixture
def tmp_state(tmp_path: Path) -> Path:
    """创建一个临时的 state.json 用于测试

    返回 tmp_path 下的 state.json 路径，让测试不影响真实 state。
    """
    state_file = tmp_path / "state.json"
    state_file.write_text(
        '{"current_round": 1, "daily_budget": {"dollar_spent_today": 0.0, "dollar_limit": 5.0, "tier": "green"}}'
    )
    return state_file


@pytest.fixture
def sample_config() -> dict:
    """返回一个最小配置字典供测试使用"""
    return {
        "swarm": {
            "project1_dir": "",
            "round_interval_minutes": 30,
            "heartbeat_timeout_seconds": 30,
        },
        "cost_strategy": {
            "daily_budget_dollars": 5.0,
            "tiers": {"tier2_boundary": 2.0, "tier3_boundary": 4.5},
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# pytest 配置
# ═══════════════════════════════════════════════════════════════════════


def pytest_configure(config) -> None:
    """注册自定义标记"""
    config.addinivalue_line("markers", "integration: 集成测试（可能依赖外部服务）")
    config.addinivalue_line("markers", "slow: 耗时较长的测试（默认跳过）")
    config.addinivalue_line("markers", "fixer: 自动修复器测试")
