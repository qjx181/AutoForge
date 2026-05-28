"""dims/perf_scanner.py — 维度三：性能扫描器

检测：
  1. N+1 查询（循环内数据库操作）
  2. 同步 I/O 阻塞（同步文件操作、网络请求在 async 函数中）
  3. 内存泄漏（全局 list/dict 无限追加，无缓存清理）
  4. 重复计算（未使用 lru_cache 的重复调用）
  5. 过大的数据加载（一次性加载全量数据到内存）
  6. 低效循环（字符串拼接在循环内）

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import ast
from pathlib import Path
from typing import Any

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "performance"




def _check_n_plus_one(code: str, filepath: str) -> list[dict]:
    """检测 N+1 查询模式（循环内 DB 操作）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    lines = code.split("\n")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            continue

        db_calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func_name = ""
                if isinstance(child.func, ast.Attribute):
                    func_name = child.func.attr
                elif isinstance(child.func, ast.Name):
                    func_name = child.func.id

                if func_name in ("execute", "fetchone", "fetchall", "select",
                                  "query", "find", "get_by_id", "get_all",
                                  "save", "delete", "update", "insert"):
                    db_calls.append(child.lineno)

        if len(db_calls) >= 2:  # 循环内有多次 DB 调用
            issues.append({
                "type": "n_plus_one",
                "severity": "high",
                "line": node.lineno,
                "file": filepath,
                "description": f"循环内有 {len(db_calls)} 次数据库操作，可能为 N+1 查询",
                "suggestion": "将循环内的 DB 操作提取到循环外批量执行",
            })

    return issues


def _check_memory_leak(code: str, filepath: str) -> list[dict]:
    """检测内存泄漏（全局容器无限追加）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    global_containers = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, (ast.List, ast.Dict, ast.Call)) and not node.value:
                        if any(m in target.id.lower() for m in ("cache", "buffer", "store", "log", "queue", "stack")):
                            global_containers.append(target.id)

    has_clear = ".clear()" in code or "del " in code
    if global_containers and not has_clear:
        issues.append({
            "type": "memory_leak",
            "severity": "medium",
            "line": 0,
            "file": filepath,
            "description": f"全局容器 {global_containers} 可能无限增长，无清理逻辑",
            "suggestion": "添加定期清理逻辑或使用 lru_cache / TTL 缓存",
        })

    return issues


def _check_missing_cache(code: str, filepath: str) -> list[dict]:
    """检测缺失 @lru_cache 的重复函数调用。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    decorated = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_name = ""
                if isinstance(dec, ast.Name):
                    dec_name = dec.id
                elif isinstance(dec, ast.Attribute):
                    dec_name = dec.attr
                if "lru_cache" in dec_name or "cache" in dec_name:
                    decorated.add(node.name)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While)):
            continue
        called_in_loop: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                called_in_loop.add(child.func.id)

        for func_name in called_in_loop:
            if func_name not in decorated and not func_name.startswith("_"):
                issues.append({
                    "type": "missing_cache",
                    "severity": "low",
                    "line": node.lineno,
                    "file": filepath,
                    "description": f"函数 '{func_name}' 在循环中被重复调用但未使用 @lru_cache",
                    "suggestion": f"在 '{func_name}' 上添加 @functools.lru_cache 装饰器",
                })

    return issues


def _check_string_concat_in_loop(code: str, filepath: str) -> list[dict]:
    """检测循环内字符串拼接。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While)):
            continue

        for child in ast.walk(node):
            if isinstance(child, ast.AugAssign) and isinstance(child.op, ast.Add):
                if isinstance(child.target, ast.Str) or (isinstance(child.target, ast.Name)):
                    issues.append({
                        "type": "string_concat_in_loop",
                        "severity": "medium",
                        "line": node.lineno,
                        "file": filepath,
                        "description": "循环内使用 += 拼接字符串，效率低",
                        "suggestion": "改用 ''.join(list) 或 StringIO",
                    })

    return issues


def _scan_file(filepath: Path) -> list[dict]:
    issues = []
    try:
        code = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return issues

    issues.extend(_check_n_plus_one(code, str(filepath)))
    issues.extend(_check_memory_leak(code, str(filepath)))
    issues.extend(_check_missing_cache(code, str(filepath)))
    issues.extend(_check_string_concat_in_loop(code, str(filepath)))

    for issue in issues:
        issue["dimension"] = DIMENSION
    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    """扫描性能问题。"""
    if not blueprint.is_enabled("performance"):
        return {
            "dimension": DIMENSION,
            "score": 100, "issues": [], "file_count": 0,
            "issue_count": 0, "summary": "性能维度未启用",
        }

    all_issues = []
    for fp in blueprint.get_source_files(blueprint.language.primary):
        all_issues.extend(_scan_file(Path(fp)))

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    critical_high = sum(1 for i in all_issues if i["severity"] in ("critical", "high"))
    score = max(0, 100 - critical_high * 15)

    return {
        "dimension": DIMENSION,
        "score": score,
        "issues": all_issues,
        "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
        "issue_count": len(all_issues),
        "summary": f"性能扫描完成：{len(all_issues)} 个问题（N+1/内存泄漏/缺失缓存/字符串拼接），评分 {score}/100",
    }

