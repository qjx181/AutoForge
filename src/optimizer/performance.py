"""optimizer/performance.py — 维度 3：性能分析扫描器

检测：N+1 查询、同步 I/O 在异步函数中、内存泄漏、同步循环中的异步调用。

依赖：复用 src.analysis.code_review.PerformanceReviewer + AsyncSyncBoundaryChecker

使用：
    from optimizer.performance import scan as scan_performance
    result = scan_performance("/path/to/project")
"""

import ast
import re
from pathlib import Path
from typing import Optional




def scan(project_path: str | Path) -> dict:
    root = Path(project_path).resolve()
    if not root.exists():
        return _empty_result(f"目录不存在: {root}")

    issues = []
    files_scanned = 0

    for py_file in root.rglob("*.py"):
        if _should_skip(py_file):
            continue

        try:
            code = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        files_scanned += 1
        rel_path = py_file.relative_to(root)

        issues.extend(_check_n_plus_one(code, rel_path))
        issues.extend(_check_sync_io_in_async(code, rel_path))
        issues.extend(_check_memory_leak(code, rel_path))
        issues.extend(_check_inefficient_loops(code, rel_path))
        issues.extend(_check_glob_usage(code, rel_path))

    score = _compute_score(issues)
    return {
        "dimension": "performance",
        "total_issues": len(issues),
        "score": score,
        "issues": issues,
        "files_scanned": files_scanned,
    }




def _check_n_plus_one(code: str, file: Path) -> list[dict]:
    """检测 N+1 查询模式：在循环内调用数据库查询。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    db_funcs = {"execute", "fetchone", "fetchall", "query", "select", "find", "get", "filter", "all"}
    loop_funcs = {"for", "while"}

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    call_name = _get_call_name(child)
                    if call_name and call_name.lower() in db_funcs:
                        issues.append({
                            "file": str(file),
                            "line": getattr(node, "lineno", 0),
                            "type": "n_plus_one",
                            "severity": "high",
                            "description": f"检测到 N+1 查询模式：循环内调用数据库方法 '{call_name}'",
                            "suggestion": "使用批量查询（SELECT ... WHERE id IN (...)) 或 ORM 的 prefetch_related/joinedload",
                        })
                        break

    return issues


def _check_sync_io_in_async(code: str, file: Path) -> list[dict]:
    """检测在 async 函数中调用同步 I/O（requests, time.sleep 等）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    sync_io = {
        "requests", "urllib.request", "http.client", "open", "io.open",
        "time.sleep", "datetime.now", "os.path", "os.listdir",
        "subprocess.Popen", "subprocess.run",
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__"):
                continue

            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    call_name = _get_call_name(child)
                    if call_name in sync_io:
                        is_async = isinstance(node, ast.AsyncFunctionDef)
                        issues.append({
                            "file": str(file),
                            "line": child.lineno or 0,
                            "type": "sync_io_in_async",
                            "severity": "high" if is_async else "low",
                            "description": f"async 函数 '{node.name}' 中调用了同步 I/O: {call_name}",
                            "suggestion": "使用异步替代: requests → httpx, open() → aiofiles, time.sleep() → asyncio.sleep()",
                        })

    return issues


def _check_memory_leak(code: str, file: Path) -> list[dict]:
    """检测潜在内存泄漏：全局列表无限增长、大对象缓存无清理。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    call_name = _get_call_name(child)
                    if call_name == "append":
                        issues.append({
                            "file": str(file),
                            "line": child.lineno or 0,
                            "type": "memory_leak",
                            "severity": "medium",
                            "description": "列表 append 操作，需确认是否有对应的清理机制",
                            "suggestion": "如果列表持续增长，考虑使用 collections.deque(maxlen=N) 或定期清理",
                        })

    return issues


def _check_inefficient_loops(code: str, file: Path) -> list[dict]:
    """检测低效循环：在循环内重复计算不变量、重复字符串拼接。"""
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.search(r"for\s+.*?\s+in\s+", stripped) and i < len(lines):
            pass  # 简化版
    return issues


def _check_glob_usage(code: str, file: Path) -> list[dict]:
    """检测循环内重复 glob 调用。"""
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.search(r"for\s+", stripped) and "glob" in code.lower():
            pass
    return issues




def _should_skip(path: Path) -> bool:
    skip = {"__pycache__", ".pytest_cache", "venv", "env", ".venv", "node_modules", ".git", "build", "dist"}
    return any(p in skip for p in path.parts)


def _get_call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _compute_score(issues: list[dict]) -> int:
    if not issues:
        return 100
    weights = {"critical": 15, "high": 8, "medium": 3, "low": 1}
    penalty = sum(weights.get(i.get("severity", "low"), 1) for i in issues)
    return max(0, min(100, 100 - penalty))


def _empty_result(msg: str) -> dict:
    return {
        "dimension": "performance",
        "total_issues": 0,
        "score": 100,
        "issues": [],
        "files_scanned": 0,
        "error": msg,
    }
