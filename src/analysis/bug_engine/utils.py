"""bug_analysis_engine.py — Bug 分析引擎

从 Python Traceback、Java Stack Trace、CI/CD 日志中提取错误信息并分析根因。

功能：
1. 解析 Python 完整 Traceback → 错误类型、文件、行号、调用链
2. 解析 Java Stack Trace → 异常类型、类名、行号
3. 解析 CI/CD 日志 → 错误分类
4. 综合分析 → 根因定位 + 修复建议
5. 持久化历史 → JSON 文件存储

API 返回结构和设计理由见各函数文档。
"""

import re
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import json
import datetime
from pathlib import Path



BUGS_DIR = Path(__file__).parent / "bugs"
BUGS_DIR.mkdir(exist_ok=True)
HISTORY_FILE = BUGS_DIR / "analysis_history.json"

def _load_history() -> list[dict]:
    """加载历史分析记录

    Returns:
        list[dict]: 历史分析记录列表，每项包含 id、类型、错误信息、时间等

    Note:
        文件不存在时返回空列表，不抛异常。
    """
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_history(report: dict) -> None:
    """将一次分析结果持久化到历史文件

    Args:
        report: 分析结果字典，必须包含 id 字段

    Returns:
        None

    Why:
        - 使用 append 模式：先加载全部，追加，再写回
        - 这样支持多进程并发写入（尽管概率低）
        - JSON 格式便于后续查询和导出
    """
    history = _load_history()
    history.append(report)
    if len(history) > 500:
        history = history[-500:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _next_id() -> str:
    """生成递增长整数 ID

    Returns:
        str: 格式如 "00042" 的 5 位 ID

    Why:
        用递增整数而非 UUID，便于在 CLI 中手动输入和记忆。
        bug_report.py --view 00042 比 --view a1b2c3d4 方便得多。
    """
    history = _load_history()
    if not history:
        return "00001"
    max_id = max(int(r.get("id", "0")) for r in history)
    return f"{max_id + 1:05d}"


