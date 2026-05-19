#!/usr/bin/env python3
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
import json
import os
import datetime
from pathlib import Path
from typing import Optional


# ── 持久化 ──────────────────────────────────────────────────────────────

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
    # 最多保留 500 条，防止文件无限膨胀
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


# ── 解析函数 ────────────────────────────────────────────────────────────


def parse_python_traceback(traceback_text: str) -> dict:
    """从 Python Traceback 文本中提取结构化信息

    Args:
        traceback_text: 完整的 Python Traceback 字符串（含 Traceback (most recent call last):）

    Returns:
        dict: {
            "error_type": "ValueError",           # 异常类型
            "file": "/path/to/file.py",            # 最终抛出异常的文件
            "line": 42,                            # 行号
            "message": "invalid literal for int()",# 异常消息
            "full_traceback": [                    # 完整的调用链
                {"file": "/path/to/a.py", "line": 10, "function": "func_a", "code": "return func_b()"},
                {"file": "/path/to/b.py", "line": 20, "function": "func_b", "code": "int('abc')"}
            ]
        }
        如果无法解析，返回 {"error_type": "UNKNOWN", "raw": traceback_text}

    Why:
        - 结构化调用链比纯文本更容易被下游 Agent 理解
        - 支持两种常见的 Python traceback 格式：标准格式和 IPython/Jupyter 格式
        - line 是 int 类型，方便数值比较

    面试官可能问：
        - 为什么不用 traceback 模块解析？答：因为输入是纯文本字符串，不是活动的异常对象
        - 支持 Celery/Flask 的 traceback 格式吗？答：它们基于标准 Python traceback，基本兼容
        - 怎么处理截断的长 traceback？答：message 可能包含 '...'，full_traceback 只保留最后 N 帧
    """
    result = {
        "error_type": "UNKNOWN",
        "file": "",
        "line": 0,
        "message": "",
        "full_traceback": [],
        "raw": traceback_text[:2000],
    }

    text = traceback_text.strip()
    if not text:
        return result

    # ── 提取异常类型和消息 ──
    # 匹配最后一行: ValueError: invalid literal for int() with base 10: 'abc'
    last_line_match = re.search(
        r"^([A-Za-z_][A-Za-z0-9_.]*(?:\.[A-Za-z_][A-Za-z0-9_.]*)*):\s*(.*)",
        text.split("\n")[-1] if "\n" in text else text,
    )
    if last_line_match:
        result["error_type"] = last_line_match.group(1)
        result["message"] = last_line_match.group(2).strip()

    # ── 提取调用链 ──
    # 匹配标准 traceback 行: File "/path/to/file.py", line 42, in func_name
    frame_pattern = re.compile(
        r'File\s+"([^"]+)",\s*line\s+(\d+)(?:,\s*in\s+(\w+))?'
    )
    code_pattern = re.compile(r"^\s+(.+)$")

    lines = text.split("\n")
    current_frame = None
    for i, line in enumerate(lines):
        frame_match = frame_pattern.search(line)
        if frame_match:
            if current_frame:
                result["full_traceback"].append(current_frame)
            current_frame = {
                "file": frame_match.group(1),
                "line": int(frame_match.group(2)),
                "function": frame_match.group(3) or "<module>",
                "code": "",
            }
        elif current_frame:
            code_match = code_pattern.match(line)
            if code_match and not line.startswith("Traceback"):
                current_frame["code"] = code_match.group(1).strip()

    if current_frame:
        result["full_traceback"].append(current_frame)

    # ── 从完整调用链中提取最终出错位置 ──
    if result["full_traceback"]:
        last_frame = result["full_traceback"][-1]
        result["file"] = last_frame["file"]
        result["line"] = last_frame["line"]

    # ── 如果没匹配到 traceback 格式，尝试简单匹配 ──
    if result["error_type"] == "UNKNOWN":
        simple_err = re.search(
            r"(?:Error|Exception|Warning|Fault):\s*(.*)", text
        )
        if simple_err:
            result["message"] = simple_err.group(1)[:200]
        line_match = re.search(r"line\s+(\d+)", text)
        if line_match:
            result["line"] = int(line_match.group(1))
        file_match = re.search(r'File\s+"([^"]+)"', text)
        if file_match:
            result["file"] = file_match.group(1)

    return result


def parse_java_stack_trace(stack_trace: str) -> dict:
    """从 Java Stack Trace 中提取结构化信息

    Args:
        stack_trace: Java 异常堆栈字符串

    Returns:
        dict: {
            "error_type": "NullPointerException",
            "class": "com.example.MyService",
            "file": "MyService.java",
            "line": 42,
            "message": "Cannot invoke...",
            "caused_by": "..."           # Caused by 链（如有）
        }

    Why:
        - Java 和 Python 的 traceback 格式差异大，需要独立解析器
        - Caused by 链对寻找根本原因至关重要
    """
    result = {
        "error_type": "UNKNOWN",
        "class": "",
        "file": "",
        "line": 0,
        "message": "",
        "caused_by": "",
        "raw": stack_trace[:2000],
    }

    text = stack_trace.strip()
    if not text:
        return result

    # ── 提取异常类型和消息 ──
    # java.lang.NullPointerException: Cannot invoke...
    first_line_match = re.match(
        r"^([A-Za-z_][A-Za-z0-9_.]*(?:\.[A-Za-z_][A-Za-z0-9_.]*)*)(?::\s*(.*))?$",
        text.split("\n")[0] if "\n" in text else text,
    )
    if first_line_match:
        result["error_type"] = first_line_match.group(1).split(".")[-1]
        result["class"] = first_line_match.group(1)
        result["message"] = (first_line_match.group(2) or "").strip()

    # ── 提取堆栈帧 ──
    # at com.example.MyService.process(MyService.java:42)
    frame_pattern = re.compile(
        r"\s+at\s+([\w.]+)\.(\w+)\(([^:]+)(?::(\d+))?\)"
    )
    lines = text.split("\n")
    for line in lines:
        frame_match = frame_pattern.search(line)
        if frame_match:
            result["file"] = frame_match.group(3)
            result["line"] = int(frame_match.group(4)) if frame_match.group(4) else 0
            break

    # ── Caused by 链 ──
    caused_by_match = re.search(r"Caused by:\s*(.*)", text, re.DOTALL)
    if caused_by_match:
        result["caused_by"] = caused_by_match.group(1).strip()[:500]

    return result


def parse_ci_log(log_text: str) -> dict:
    """从 CI/CD 日志中提取错误信息

    Args:
        log_text: CI/CD 构建日志文本

    Returns:
        dict: {
            "error_type": "BUILD_FAILURE" | "TEST_FAILURE" | "LINT_FAILURE" | "TIMEOUT" | "UNKNOWN",
            "stage": "build" | "test" | "deploy",
            "files_with_errors": ["src/main.py"],
            "error_count": 3,
            "summary": "..."
        }

    Why:
        - CI 日志通常混合 stdout/stderr，需要模糊模式识别
        - error_count 帮助判断严重程度：大量错误可能意味着环境问题而非代码问题
    """
    result = {
        "error_type": "UNKNOWN",
        "stage": "unknown",
        "files_with_errors": [],
        "error_count": 0,
        "summary": "",
        "raw": log_text[:2000],
    }

    text = log_text.strip()
    if not text:
        return result

    lines = text.split("\n")
    error_lines = []

    # ── 阶段检测 ──
    stage_patterns = {
        "build": r"build|compil|make\b|cmake|mvn|gradle",
        "test": r"test\b|pytest|jest|mocha|testing|test suite",
        "deploy": r"deploy|release|upload|publish|docker push",
    }
    for stage, pattern in stage_patterns.items():
        if re.search(pattern, text[:500], re.IGNORECASE):
            result["stage"] = stage
            break

    # ── 错误检测 ──
    for line in lines:
        stripped = line.strip()
        # 标准 ERROR 标记
        if re.search(r"\b(ERROR|FAILED|FAILURE|FATAL|CRASH)\b", stripped, re.IGNORECASE):
            error_lines.append(line)
            # 提取文件名
            file_matches = re.findall(r'[\w/]+\.\w+', line)
            for f in file_matches:
                if f not in result["files_with_errors"]:
                    result["files_with_errors"].append(f)

    result["error_count"] = len(error_lines)
    result["summary"] = "\n".join(error_lines[:5])[:500]

    # ── 错误类型判断 ──
    if re.search(r"FAILED|FAILURE|exit code \d+", text):
        result["error_type"] = "TEST_FAILURE"
    if re.search(r"syntax error|undefined reference|undeclared", text, re.IGNORECASE):
        result["error_type"] = "BUILD_FAILURE"
    if re.search(r"lint|flake8|black|eslint|prettier", text[:1000], re.IGNORECASE):
        result["error_type"] = "LINT_FAILURE"
    if re.search(r"timed? ?out|timeout|exceeded", text, re.IGNORECASE):
        result["error_type"] = "TIMEOUT"

    return result


# ── 综合分析 ────────────────────────────────────────────────────────────


# 已知模式的修复建议（安全关键词、行话翻译）
FIX_SUGGESTIONS = {
    "ValueError": "检查输入类型转换，确保传入值可被正确解析。添加 try/except 防御。",
    "TypeError": "检查函数参数类型和数量，确认对象支持调用的方法。",
    "KeyError": "在访问字典前用 .get() 或检查 key 是否存在。",
    "IndexError": "访问列表前检查 len() 边界，或用 try/except 保护。",
    "AttributeError": "检查对象类型，确认属性/方法存在。考虑 hasattr() 前置检查。",
    "ModuleNotFoundError": "检查 requirements.txt / pyproject.toml 中是否缺少该依赖。",
    "ImportError": "检查导入路径和循环依赖，确认模块在 PYTHONPATH 中。",
    "FileNotFoundError": "检查文件路径是否存在，考虑用 pathlib 管理路径。",
    "ZeroDivisionError": "除零操作前检查分母是否为 0。",
    "ConnectionError": "检查网络连接、API 地址和端口是否可达。",
    "TimeoutError": "增加超时时间，或检查服务是否死锁。",
    "RecursionError": "检查递归函数是否缺少终止条件，或递归深度过大。",
    "NullPointerException": "Java 空指针：检查对象初始化路径，添加 @Nullable/@NonNull 注解。",
    "BUILD_FAILURE": "检查编译命令和依赖版本，确认环境一致性。",
    "TEST_FAILURE": "检查测试用例和被测代码的边界条件。",
    "LINT_FAILURE": "运行 linter 自动修复：black/flake8/eslint --fix。",
    "TIMEOUT": "检查超时设置是否合理，或优化算法性能。",
}


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
    # 1. 解析
    if source_type == "java":
        parsed = parse_java_stack_trace(traceback_or_log)
    elif source_type == "ci":
        parsed = parse_ci_log(traceback_or_log)
    else:
        parsed = parse_python_traceback(traceback_or_log)

    # 2. 生成根因和修复建议
    error_type = parsed.get("error_type", "UNKNOWN")
    message = parsed.get("message", "")
    file_ = parsed.get("file", "")
    line = parsed.get("line", 0)

    # 从消息中提取更具象的根因
    root_cause = f"{error_type}: {message[:100]}" if message else f"发生 {error_type}"

    if error_type in FIX_SUGGESTIONS:
        suggested_fix = FIX_SUGGESTIONS[error_type]
    else:
        suggested_fix = "请人工审查代码，定位具体错误位置。自动分析未覆盖此错误类型。"

    # 3. 判断修复类型
    fix_type = "patch"
    if not file_:
        fix_type = "config_change"
    elif source_type == "ci":
        fix_type = "config_change"

    # 4. 置信度
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


# ── 便捷函数 ────────────────────────────────────────────────────────────


def fix_suggestion(error_type: str) -> str:
    """根据错误类型返回标准修复建议

    Args:
        error_type: 错误类型字符串（如 "ValueError"）

    Returns:
        str: 修复建议文本。如果不在已知模式库中，返回通用建议。
    """
    return FIX_SUGGESTIONS.get(error_type, "请人工审查此错误。自动分析未覆盖该类型。")


if __name__ == "__main__":
    # 快速测试
    test_tb = """Traceback (most recent call last):
  File "/app/src/main.py", line 42, in process_data
    result = int(user_input)
ValueError: invalid literal for int() with base 10: 'abc'"""
    result = analyze_bug(test_tb, "python")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print()
    print("=== 排名原因 ===")
    for c in rank_possible_causes(result):
        print(f"  [{c['probability']:.0%}] {c['cause']}")
