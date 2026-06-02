"""dims/async_scanner.py — 异步扫描器"""
import re
from pathlib import Path

DIMENSION = "asyncification"


def _check_sync_http_in_async(code, filepath):
    issues = []
    if code is None:
        return issues
    lines = code.split("\n")
    in_async = False
    async_indent = 0
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("async def "):
            in_async = True
            async_indent = indent
        elif in_async and indent <= async_indent and stripped:
            in_async = False
        if in_async and indent > async_indent:
            if re.search(r'requests\.(get|post|put|delete|patch)\s*\(', stripped):
                issues.append({"type": "sync_http_in_async", "severity": "high",
                               "file": filepath, "line": i,
                               "description": "异步函数中使用同步requests",
                               "suggestion": "改用aiohttp或httpx"})
    return issues


def _check_blocking_in_async(code, filepath):
    issues = []
    lines = code.split("\n")
    in_async = False
    async_indent = 0
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
                issues.append({"type": "blocking_in_async", "severity": "high",
                               "file": filepath, "line": i,
                               "description": "异步函数中使用阻塞调用",
                               "suggestion": "改用asyncio.sleep()"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_sync_http_in_async(code, str(filepath)))
    issues.extend(_check_blocking_in_async(code, str(filepath)))
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
            "summary": "asyncification扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
