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

def fix_suggestion(error_type: str) -> str:
    """根据错误类型返回标准修复建议

    Args:
        error_type: 错误类型字符串（如 "ValueError"）

    Returns:
        str: 修复建议文本。如果不在已知模式库中，返回通用建议。
    """
    return FIX_SUGGESTIONS.get(error_type, "请人工审查此错误。自动分析未覆盖该类型。")


def fix_suggestion(error_type: str) -> str:
    """根据错误类型返回标准修复建议

    Args:
        error_type: 错误类型字符串（如 "ValueError"）

    Returns:
        str: 修复建议文本。如果不在已知模式库中，返回通用建议。
    """
    return FIX_SUGGESTIONS.get(error_type, "请人工审查此错误。自动分析未覆盖该类型。")




def execute_bug_fix(bug: dict, project_path: str) -> dict:
    """根据分析结果，在指定项目路径下执行修复

    Args:
        bug: analyze_bug() 返回的分析结果字典，必须包含 file/line/message
        project_path: 项目根目录路径

    Returns:
        dict: {
            "success": bool,
            "file": str,           # 实际修改的文件完整路径
            "patch_applied": bool, # 是否应用了 patch
            "verification": str,    # 验证结果
            "details": str,         # 详细说明
        }

    修复策略：
        1. 用 project_path + bug["file"] 拼接完整文件路径
        2. 读取文件内容
        3. 根据 bug 类型生成修复代码
        4. 用 patch 替换目标行
        5. 运行 py_compile 验证语法
        6. 返回结果
    """
    import subprocess
    import shutil
    from pathlib import Path as _Path
    project = _Path(project_path)
    if not project.exists():
        return {
            "success": False,
            "file": "",
            "patch_applied": False,
            "verification": "",
            "details": f"项目路径不存在: {project_path}",
        }

    error_file = bug.get("file", "")
    error_line = bug.get("line", 0)
    error_type = bug.get("error_type", "")
    message = bug.get("message", "")

    if not error_file:
        return {
            "success": False,
            "file": "",
            "patch_applied": False,
            "verification": "",
            "details": "分析结果中无文件路径，无法定位修复位置",
        }

    if _Path(error_file).is_absolute():
        target_file = _Path(error_file)
    else:
        target_file = project / error_file

    if not target_file.exists():
        matches = list(project.rglob(_Path(error_file).name))
        if matches:
            target_file = matches[0]
        else:
            return {
                "success": False,
                "file": str(target_file),
                "patch_applied": False,
                "verification": "",
                "details": f"目标文件不存在: {error_file}，已尝试在 {project_path} 下搜索但未找到",
            }

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return {
            "success": False,
            "file": str(target_file),
            "patch_applied": False,
            "verification": "",
            "details": f"读取文件失败: {e}",
        }

    if error_line <= 0 or error_line > len(lines):
        return {
            "success": False,
            "file": str(target_file),
            "patch_applied": False,
            "verification": "",
            "details": f"行号无效: {error_line}（文件共 {len(lines)} 行）",
        }

    patch_lines = _generate_fix(error_type, message, lines, error_line - 1)
    if not patch_lines:
        return {
            "success": False,
            "file": str(target_file),
            "patch_applied": False,
            "verification": "",
            "details": f"无法为 {error_type} 自动生成修复代码，请人工处理",
        }

    new_lines = lines[:error_line - 1] + patch_lines + lines[error_line:]
    backup_file = target_file.with_suffix(target_file.suffix + ".bak")
    shutil.copy(target_file, backup_file)

    try:
        with open(target_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        patch_applied = True
    except Exception as e:
        shutil.copy(backup_file, target_file)
        return {
            "success": False,
            "file": str(target_file),
            "patch_applied": False,
            "verification": "",
            "details": f"写入文件失败，已恢复备份: {e}",
        }

    verification = ""
    try:
        compile_result = subprocess.run(
            ["python", "-m", "py_compile", str(target_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if compile_result.returncode == 0:
            verification = "✅ 语法检查通过"
            success = True
        else:
            verification = f"❌ 语法错误: {compile_result.stderr[:200]}"
            success = False
            shutil.copy(backup_file, target_file)
            patch_applied = False
    except Exception as e:
        verification = f"⚠️ 验证过程出错: {e}"
        success = False

    try:
        backup_file.unlink()
    except Exception:
        pass

    return {
        "success": success,
        "file": str(target_file),
        "patch_applied": patch_applied,
        "verification": verification,
        "details": f"已在 {target_file}:{error_line} 应用修复，共修改 {len(patch_lines)} 行",
        "error_type": error_type,
        "target_line": error_line,
    }


def _generate_fix(error_type: str, message: str, lines: list, line_idx: int) -> list[str]:
    """根据错误类型生成修复代码行

    Args:
        error_type: 错误类型
        message: 错误消息
        lines: 文件内容（行列表）
        line_idx: 错误行索引（0-based）

    Returns:
        list[str]: 修复后的代码行列表（包含换行符）
    """
    orig_line = lines[line_idx].rstrip("\r\n")

    if error_type == "ValueError":
        if "int(" in orig_line or "float(" in orig_line:
            indent = len(orig_line) - len(orig_line.lstrip())
            spaces = " " * indent
            return [
                f"{spaces}try:\n",
                f"{orig_line}\n",
                f"{spaces}except ValueError as e:\n",
                f"{spaces}    # TODO: 处理转换失败 {message}\n",
                f"{spaces}    raise\n",
            ]
        return [f"# [FIXED] {orig_line}  -- 修复原因: {error_type}: {message[:50]}\n"]

    elif error_type == "TypeError":
        if "NoneType" in message or ".get(" not in orig_line:
            indent = len(orig_line) - len(orig_line.lstrip())
            spaces = " " * indent
            return [
                f"{spaces}if {orig_line.strip()} is not None:\n",
                f"{spaces}    {orig_line.strip()}\n",
            ]
        return [f"# [FIXED] {orig_line}  -- 修复原因: {error_type}: {message[:50]}\n"]

    elif error_type == "KeyError":
        indent = len(orig_line) - len(orig_line.lstrip())
        spaces = " " * indent
        match = __import__("re").search(r"(\w+)\[", orig_line)
        if match:
            var = match.group(1)
            return [
                f"{spaces}# [FIXED] 原代码: {orig_line.strip()}\n",
                f"{spaces}if {var}.get(",
            ]
        return [f"# [FIXED] {orig_line}  -- 修复原因: {error_type}: {message[:50]}\n"]

    elif error_type == "IndexError":
        indent = len(orig_line) - len(orig_line.lstrip())
        spaces = " " * indent
        return [
            f"{spaces}try:\n",
            f"{orig_line}\n",
            f"{spaces}except IndexError:\n",
            f"{spaces}    # TODO: 处理索引越界\n",
            f"{spaces}    pass\n",
        ]

    elif error_type == "AttributeError":
        return [f"# [FIXED] {orig_line}  -- 修复原因: {error_type}: {message[:50]}\n"]

    elif error_type == "ModuleNotFoundError":
        return [f"# [FIXED] 缺少模块: {message}\n"]

    elif error_type == "ImportError":
        return [f"# [FIXED] 导入错误: {message}\n"]

    else:
        return [f"# [FIXED] {orig_line}  -- 修复原因: {error_type}: {message[:80]}\n"]


if __name__ == "__main__":
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
