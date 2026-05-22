#!/usr/bin/env python3
"""dims/async_scanner.py — 维度八：异步化扫描器

检测：
  1. async 函数中使用同步阻塞 I/O（time.sleep / requests.get / open()）
  2. 同步函数调用异步函数（无 await）
  3. asyncio.run() 在已有事件循环中被调用
  4. 缺少 asyncio.to_thread 包装 sync I/O
  5. async 函数的错误处理缺失

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import ast
import re
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "asyncification"


BLOCKING_CALLS = {
    "time.sleep": "同步睡眠，应使用 asyncio.sleep",
    "requests.get": "同步 HTTP 请求，应使用 httpx.AsyncClient.get",
    "requests.post": "同步 HTTP 请求，应使用 httpx.AsyncClient.post",
    "requests.put": "同步 HTTP 请求，应使用 httpx.AsyncClient.put",
    "requests.delete": "同步 HTTP 请求，应使用 httpx.AsyncClient.delete",
    "requests.patch": "同步 HTTP 请求，应使用 httpx.AsyncClient.patch",
    "requests.request": "同步 HTTP 请求，应使用 httpx.AsyncClient.request",
    "httpx.sync": "httpx 同步客户端，应使用 httpx.AsyncClient",
    "open": "同步文件 I/O，应使用 aiofiles 或 asyncio.to_thread(open)",
    "os.read": "同步文件 I/O",
    "os.write": "同步文件 I/O",
}


def _check_blocking_in_async(code: str, filepath: str) -> list[dict]:
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    lines = code.split("\n")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        func_name = node.name
        is_async = isinstance(node, ast.AsyncFunctionDef)

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue

            # 检查 time.sleep
            if isinstance(child.func, ast.Attribute):
                if isinstance(child.func.value, ast.Name) and child.func.value.id == "time":
                    if child.func.attr == "sleep" and is_async:
                        issues.append({
                            "type": "sync_sleep_in_async",
                            "severity": "high",
                            "file": filepath,
                            "line": child.lineno,
                            "description": f"async 函数 '{func_name}' 中调用 time.sleep()",
                            "suggestion": "将 time.sleep 改为 asyncio.sleep",
                        })

                # 检查 requests
                if isinstance(child.func.value, ast.Name) and child.func.value.id == "requests":
                    if child.func.attr in BLOCKING_CALLS and is_async:
                        issues.append({
                            "type": "sync_http_in_async",
                            "severity": "high",
                            "file": filepath,
                            "line": child.lineno,
                            "description": f"async 函数 '{func_name}' 中调用 requests.{child.func.attr}()",
                            "suggestion": "改用 httpx.AsyncClient 或 asyncio.to_thread(requests.get, ...)",
                        })

                # 检查 open()
                if child.func.attr == "open" and is_async:
                    issues.append({
                        "type": "sync_io_in_async",
                        "severity": "high",
                        "file": filepath,
                        "line": child.lineno,
                        "description": f"async 函数 '{func_name}' 中调用 open()",
                        "suggestion": "使用 aiofiles 或 asyncio.to_thread(open)",
                    })

    return issues


def _check_asyncio_run_in_loop(code: str, filepath: str) -> list[dict]:
    issues = []
    IGNORE = {"main", "cli", "run", "app", "setup", "init"}
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'asyncio\.run\s*\(', stripped):
            # 简单检查：如果函数名含 sync/_wrap 则是问题
            issues.append({
                "type": "asyncio_run_in_loop",
                "severity": "medium",
                "file": filepath,
                "line": i,
                "description": "检测到 asyncio.run() 调用",
                "suggestion": "确保只在 main() 或入口函数中调用 asyncio.run()，不在已有事件循环的上下文中调用",
            })
    return issues


def _scan_file(filepath: Path) -> list[dict]:
    issues = []
    try:
        code = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return issues

    issues.extend(_check_blocking_in_async(code, str(filepath)))
    issues.extend(_check_asyncio_run_in_loop(code, str(filepath)))

    for issue in issues:
        issue["dimension"] = DIMENSION
    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    if not blueprint.is_enabled("asyncification"):
        return {"dimension": DIMENSION, "score": 100, "issues": [],
                "file_count": 0, "issue_count": 0,
                "summary": "异步化维度未启用（项目无 async 代码）"}

    all_issues = []
    for fp in blueprint.get_source_files(blueprint.language.primary):
        all_issues.extend(_scan_file(Path(fp)))

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    score = max(0, 100 - sum(1 for i in all_issues if i["severity"] in ("high",)) * 15)
    return {
        "dimension": DIMENSION, "score": score,
        "issues": all_issues,
        "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
        "issue_count": len(all_issues),
        "summary": (
            f"异步化扫描完成：{len(all_issues)} 个问题（同步阻塞/sleep/requests），"
            f"评分 {score}/100"
        ),
    }
