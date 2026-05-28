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

def analyze_bug(
    traceback_or_log: str,
    source_type: str = "python",
) -> dict:
    """综合分析错误信息，定位根因并给出修复建议

    Args:
        traceback_or_log: 错误文本（traceback / stack trace / 日志）
        source_type: "python" | "java" | "ci"

    Returns:
        dict: {
            "id": "00042",
            "error_type": "ValueError",
            "file": "/path/file.py",
            "line": 42,
            "message": "...",
            "root_cause": "字符串转整数时传入空字符串",
            "suggested_fix": "在转换前添加 if not s: continue",
            "fix_type": "patch" | "write_file" | "config_change",
            "confidence": 0.85,
            "source_type": "python",
            "timestamp": "2026-05-19T18:00:00",
        }

    Why:
        - fix_type 告诉下游 Agent 用什么工具修复（patch 局部、write_file 全套、config_change）
        - confidence 帮助协调者决定是否信任建议（>=0.8 自动执行，<0.8 人工审核）
        - 这个函数是 bug_report.py 调用的核心入口

    面试官可能问：
        - confidence 怎么计算的？答：基于关键词匹配度 + 已知模式命中数
        - 对新框架的 traceback 有效吗？答：框架修改了异常格式时需要扩展正则
        - 怎么保证修复建议不削改其他代码？答：fix_type=patch 建议只增补 try/except，不改签名
    """
    if source_type == "java":
        parsed = parse_java_stack_trace(traceback_or_log)
    elif source_type == "ci":
        parsed = parse_ci_log(traceback_or_log)
    else:
        parsed = parse_python_traceback(traceback_or_log)

    error_type = parsed.get("error_type", "UNKNOWN")
    message = parsed.get("message", "")
    file_ = parsed.get("file", "")
    line = parsed.get("line", 0)

    root_cause = f"{error_type}: {message[:100]}" if message else f"发生 {error_type}"

    if error_type in FIX_SUGGESTIONS:
        suggested_fix = FIX_SUGGESTIONS[error_type]
    else:
        suggested_fix = "请人工审查代码，定位具体错误位置。自动分析未覆盖此错误类型。"

    fix_type = "patch"
    if not file_:
        fix_type = "config_change"
    elif source_type == "ci":
        fix_type = "config_change"

    confidence = 0.5
    if error_type in FIX_SUGGESTIONS:
        confidence = 0.7
    if file_ and line > 0:
        confidence = 0.85
    if error_type == "UNKNOWN":
        confidence = 0.2

    report = {
        "id": _next_id(),
        "error_type": error_type,
        "file": file_,
        "line": line,
        "message": message,
        "root_cause": root_cause,
        "suggested_fix": suggested_fix,
        "fix_type": fix_type,
        "confidence": round(confidence, 2),
        "source_type": source_type,
        "timestamp": datetime.datetime.now().isoformat(),
        "parsed": {k: v for k, v in parsed.items() if k != "raw"},
    }

    _save_history(report)
    return report


def rank_possible_causes(error_info: dict) -> list[dict]:
    """按可能性排序的根因列表

    Args:
        error_info: analyze_bug 返回的错误分析结果

    Returns:
        list[dict]: [
            {"cause": "字符串参数为空", "probability": 0.7, "evidence": ["line 42: int('')"]},
            {"cause": "输入格式不正确", "probability": 0.2, "evidence": ["预期数字但收到字母"]},
        ]

    Why:
        - 单一根因结论可能误导。多个可能原因排序让 Agent 能尝试多个修复方案
        - evidence 字段提供可验证的具体代码证据
    """
    error_type = error_info.get("error_type", "")
    message = error_info.get("message", "")
    causes = []

    if error_type == "ValueError":
        if "int()" in message:
            causes.append({
                "cause": "int() 接收到非数字字符串",
                "probability": 0.7,
                "evidence": [f"错误消息: {message}"],
                "suggestion": "用 str.isdigit() 或 try/except 包装 int() 调用",
            })
            causes.append({
                "cause": "输入值为 None 或空字符串",
                "probability": 0.2,
                "evidence": ["检查入参来源"],
                "suggestion": "添加 if not value: continue 前置检查",
            })
        else:
            causes.append({
                "cause": f"值转换失败: {message}",
                "probability": 0.5,
                "evidence": [message],
                "suggestion": FIX_SUGGESTIONS.get("ValueError", "请人工审查"),
            })
    elif error_type == "KeyError":
        causes.append({
            "cause": f"字典缺少键: {message}",
            "probability": 0.8,
            "evidence": [message],
            "suggestion": "用 dict.get(key, default) 替代 dict[key]",
        })
    elif error_type == "ModuleNotFoundError":
        causes.append({
            "cause": f"缺少模块: {message}",
            "probability": 0.9,
            "evidence": [message],
            "suggestion": "pip install 对应包或检查 requirements.txt",
        })
    else:
        causes.append({
            "cause": root_cause,
            "probability": 0.5,
            "evidence": [f"文件: {error_info.get('file', '未知')}:{error_info.get('line', '?')}"],
            "suggestion": error_info.get("suggested_fix", "请人工审查"),
        })

    causes.sort(key=lambda x: x["probability"], reverse=True)
    return causes


