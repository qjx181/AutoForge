"""dims/test_scanner.py — 测试扫描器"""
import re
from pathlib import Path

DIMENSION = "testing"


def _check_empty_test(code, filepath):
    issues = []
    if code is None:
        return issues
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r'def (test_\w+)\s*\(', stripped):
            func_indent = len(line) - len(line.lstrip())
            body_lines = []
            for j in range(i, min(i + 50, len(lines))):
                bl = lines[j].strip()
                bindent = len(lines[j]) - len(lines[j].lstrip())
                if bindent <= func_indent and j > i:
                    break
                if bl and not bl.startswith("#") and bl != "pass":
                    body_lines.append(bl)
            if not body_lines:
                issues.append({"type": "empty_test", "severity": "high",
                               "file": filepath, "line": i,
                               "description": "空测试函数",
                               "suggestion": "实现测试逻辑"})
    return issues


def _check_missing_assert(code, filepath):
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r'def (test_\w+)\s*\(', stripped):
            func_indent = len(line) - len(line.lstrip())
            has_assert = False
            for j in range(i, min(i + 20, len(lines))):
                bl = lines[j].strip()
                bindent = len(lines[j]) - len(lines[j].lstrip())
                if bindent <= func_indent and j > i:
                    break
                if "assert" in bl or "pytest.raises" in bl:
                    has_assert = True
                    break
            if not has_assert:
                issues.append({"type": "missing_assert", "severity": "medium",
                               "file": filepath, "line": i,
                               "description": "测试缺少assert断言",
                               "suggestion": "添加assert验证"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_empty_test(code, str(filepath)))
    issues.extend(_check_missing_assert(code, str(filepath)))
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
            "summary": "testing扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
