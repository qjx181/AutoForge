"""src — 项目三：多Agent 源码包

本 __init__.py 在任何 `from src.* import` 语句前自动执行，
将项目根目录和 src/ 目录加入 sys.path，确保向后兼容。

新代码应使用：
    from src.analysis.bug_analysis_engine import analyze_bug
    from src.infra.swarm_logger import SwarmLogger

旧代码（从根目录运行时）可继续使用：
    from bug_analysis_engine import analyze_bug
    from swarm_logger import SwarmLogger
（前提是根目录脚本先执行 sys.path.insert(0, str(PROJECT_ROOT))）
"""

import sys as _sys
from pathlib import Path as _Path

PROJECT_ROOT = _Path(__file__).parent.parent.resolve()
SRC_DIR = _Path(__file__).parent.resolve()

# 将项目根目录加入 sys.path（供旧代码的 `from X import` 使用）
_root_str = str(PROJECT_ROOT)
if _root_str not in _sys.path:
    _sys.path.insert(0, _root_str)

# 将 src/ 目录加入 sys.path（供新代码的 `from src.X import` 使用）
_src_str = str(SRC_DIR)
if _src_str not in _sys.path:
    _sys.path.insert(0, _src_str)

# ─── 公开顶层子模块的常用符号（可选，方便 from src import xxx）──────
# 如果需要从 src 直接 import xx，请在子模块的 __init__.py 中暴露
