"""src/optimizer/__init__.py — 持续优化引擎

9 维度扫描器目录：
  1. code_quality  — 代码质量（复用现有 code_review.py QualityReviewer）
  2. security      — 安全审查（复用 SecurityReviewer）
  3. performance   — 性能分析（复用 PerformanceReviewer + AsyncSyncBoundaryChecker）
  4. testing       — 测试覆盖分析
  5. architecture  — 架构问题检测
  6. documentation — 文档覆盖率检查
  7. configuration  — 配置合理性分析
  8. asyncification — 异步化改造建议
  9. dead_code     — 死代码检测

核心引擎：optimizer_core.py — 统一调度所有扫描器
"""

from . import code_quality
from . import security
from . import performance
from . import testing
from . import architecture
from . import documentation
from . import configuration
from . import asyncification
from . import dead_code

__all__ = [
    "code_quality",
    "security",
    "performance",
    "testing",
    "architecture",
    "documentation",
    "configuration",
    "asyncification",
    "dead_code",
]
