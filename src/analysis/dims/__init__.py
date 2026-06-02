"""src.analysis.dims — 8维度AST扫描器

每个模块导出 scan(blueprint) 函数，返回 dict:
  {"dimension", "score", "issues", "file_count", "issue_count", "summary"}
"""

from .quality_scanner import scan as scan_quality
from .sec_scanner import scan as scan_security
from .perf_scanner import scan as scan_performance
from .async_scanner import scan as scan_asyncification
from .config_scanner import scan as scan_configuration
from .test_scanner import scan as scan_testing
from .doc_scanner import scan as scan_documentation
from .deadcode_scanner import scan as scan_deadcode

DIMENSION_ORDER = [
    "security",
    "performance",
    "asyncification",
    "quality",
    "testing",
    "architecture",
    "documentation",
    "configuration",
    "deadcode",
]

DIMENSION_NAMES = {
    "security": "安全",
    "performance": "性能",
    "asyncification": "异步化",
    "quality": "代码质量",
    "testing": "测试覆盖",
    "architecture": "架构",
    "documentation": "文档",
    "configuration": "配置",
    "deadcode": "死代码",
}

__all__ = [
    "scan_quality",
    "scan_security",
    "scan_performance",
    "scan_asyncification",
    "scan_configuration",
    "scan_testing",
    "scan_documentation",
    "scan_deadcode",
    "DIMENSION_ORDER",
    "DIMENSION_NAMES",
]
