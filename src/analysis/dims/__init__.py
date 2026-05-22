"""src.analysis.dims — 9 维度扫描器包

各维度扫描器均实现统一的 scan(blueprint) -> dict 接口。

维度映射：
  1. quality_scanner    -> 代码质量 + 死代码函数级
  2. test_scanner       -> 测试覆盖
  3. perf_scanner       -> 性能热点
  4. arch_scanner       -> 架构模式
  5. sec_scanner        -> 安全漏洞
  6. doc_scanner        -> 文档完整性
  7. config_scanner     -> 配置规范性
  8. async_scanner      -> 异步化
  9. deadcode_scanner   -> 死文件/重复文件
"""

from .quality_scanner import scan as scan_quality
from .test_scanner import scan as scan_testing
from .perf_scanner import scan as scan_performance
from .arch_scanner import scan as scan_architecture
from .sec_scanner import scan as scan_security
from .doc_scanner import scan as scan_documentation
from .config_scanner import scan as scan_configuration
from .async_scanner import scan as scan_asyncification
from .deadcode_scanner import scan as scan_deadcode

# 维度注册表（顺序固定，用于报告输出）
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

# 维度名称映射（中英双语）
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
    "scan_testing",
    "scan_performance",
    "scan_architecture",
    "scan_security",
    "scan_documentation",
    "scan_configuration",
    "scan_asyncification",
    "scan_deadcode",
    "DIMENSION_ORDER",
    "DIMENSION_NAMES",
]
