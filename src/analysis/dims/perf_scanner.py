"""dims/perf_scanner.py — 性能扫描器"""
import re
from pathlib import Path

DIMENSION = "performance"


def _check_loop_concat(code, filepath):
    issues = []
    if code is None:
        return []
    try:
        import ast
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.While)):
                for child in ast.walk(node):
                    if isinstance(child, ast.AugAssign) and isinstance(child.op, ast.Add):
                        if isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                            issues.append({"type": "loop_string_concat", "severity": "high",
                                           "file": filepath, "line": child.lineno,
                                           "description": "循环内字符串拼接",
                                           "suggestion": "改用join()"})
    except SyntaxError:
        pass
    return issues


def _check_sync_sleep_in_async(code, filepath):
    """检测异步函数中使用time.sleep()的问题"""
    issues = []
    if code is None:
        return issues
    in_async = False
    async_indent = 0
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("async def "):
            in_async = True
            async_indent = indent
        elif in_async and indent <= async_indent and stripped:
            in_async = False
        if in_async and indent > async_indent:
            if re.search(r'time\.sleep\s*\(', stripped):
                issues.append({"type": "sync_sleep_in_async", "severity": "high",
                               "file": filepath, "line": i,
                               "description": "异步函数中使用time.sleep()",
                               "suggestion": "改用await asyncio.sleep()"})
    return issues


def _check_re_compile_in_loop(code, filepath):
    issues = []
    lines = code.split("\n")
    in_loop = False
    loop_indent = 0
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if re.match(r'(for|while)\b', stripped) and stripped.endswith(":"):
            in_loop = True
            loop_indent = indent
        elif in_loop and indent <= loop_indent and stripped:
            in_loop = False
        if in_loop and indent > loop_indent:
            if re.search(r're\.compile\s*\(', stripped):
                issues.append({"type": "re_compile_in_loop", "severity": "medium",
                               "file": filepath, "line": i,
                               "description": "循环内re.compile()",
                               "suggestion": "提取到循环外"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_loop_concat(code, str(filepath)))
    issues.extend(_check_sync_sleep_in_async(code, str(filepath)))
    issues.extend(_check_re_compile_in_loop(code, str(filepath)))
    for issue in issues:
        issue["dimension"] = DIMENSION
    return issues


def scan(blueprint):
    all_issues = []
    for fp in blueprint.get_source_files(blueprint.language.primary):
        all_issues.extend(_scan_file(fp))
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))
    score = max(0, 100 - len(all_issues) * 5)
    return {"dimension": DIMENSION, "score": score, "issues": all_issues,
            "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
            "issue_count": len(all_issues),
            "summary": "performance扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
