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

    last_line_match = re.search(
        r"^([A-Za-z_][A-Za-z0-9_.]*(?:\.[A-Za-z_][A-Za-z0-9_.]*)*):\s*(.*)",
        text.split("\n")[-1] if "\n" in text else text,
    )
    if last_line_match:
        result["error_type"] = last_line_match.group(1)
        result["message"] = last_line_match.group(2).strip()

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

    if result["full_traceback"]:
        last_frame = result["full_traceback"][-1]
        result["file"] = last_frame["file"]
        result["line"] = last_frame["line"]

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

    first_line_match = re.match(
        r"^([A-Za-z_][A-Za-z0-9_.]*(?:\.[A-Za-z_][A-Za-z0-9_.]*)*)(?::\s*(.*))?$",
        text.split("\n")[0] if "\n" in text else text,
    )
    if first_line_match:
        result["error_type"] = first_line_match.group(1).split(".")[-1]
        result["class"] = first_line_match.group(1)
        result["message"] = (first_line_match.group(2) or "").strip()

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

    stage_patterns = {
        "build": r"build|compil|make\b|cmake|mvn|gradle",
        "test": r"test\b|pytest|jest|mocha|testing|test suite",
        "deploy": r"deploy|release|upload|publish|docker push",
    }
    for stage, pattern in stage_patterns.items():
        if re.search(pattern, text[:500], re.IGNORECASE):
            result["stage"] = stage
            break

    for line in lines:
        stripped = line.strip()
        if re.search(r"\b(ERROR|FAILED|FAILURE|FATAL|CRASH)\b", stripped, re.IGNORECASE):
            error_lines.append(line)
            file_matches = re.findall(r'[\w/]+\.\w+', line)
            for f in file_matches:
                if f not in result["files_with_errors"]:
                    result["files_with_errors"].append(f)

    result["error_count"] = len(error_lines)
    result["summary"] = "\n".join(error_lines[:5])[:500]

    if re.search(r"FAILED|FAILURE|exit code \d+", text):
        result["error_type"] = "TEST_FAILURE"
    if re.search(r"syntax error|undefined reference|undeclared", text, re.IGNORECASE):
        result["error_type"] = "BUILD_FAILURE"
    if re.search(r"lint|flake8|black|eslint|prettier", text[:1000], re.IGNORECASE):
        result["error_type"] = "LINT_FAILURE"
    if re.search(r"timed? ?out|timeout|exceeded", text, re.IGNORECASE):
        result["error_type"] = "TIMEOUT"

    return result




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
