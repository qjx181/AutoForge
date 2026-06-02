import ast
import re
from pathlib import Path

DIMENSION = "quality"


def _check_nesting(code, filepath):
    if code is None:
        return []
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        indent = len(line) - len(line.lstrip())
        if indent >= 24 and line.strip() and not line.strip().startswith("#"):
            issues.append({"type": "deep_nesting", "severity": "medium",
                           "file": filepath, "line": i,
                           "description": "嵌套深度 %d 层" % (indent // 4),
                           "suggestion": "提取子函数"})
    return issues


def _check_todo_fixme(code, filepath):
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        if re.search(r'\b(TODO|FIXME|HACK|XXX)\b', line, re.IGNORECASE):
            if re.search(r'(test_|example_|demo_)', filepath, re.IGNORECASE):
                continue
            issues.append({"type": "todo_fixme", "severity": "low",
                           "file": filepath, "line": i,
                           "description": "遗留TODO/FIXME",
                           "suggestion": "完成实现或删除"})
    return issues


def _check_long_line(code, filepath):
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        if len(line) > 120 and not line.strip().startswith("#"):
            issues.append({"type": "long_line", "severity": "low",
                           "file": filepath, "line": i,
                           "description": "行长度 %d > 120" % len(line),
                           "suggestion": "拆分语句或换行"})
    return issues


def _check_empty_except(code, filepath):
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped == "except:" or stripped.startswith("except Exception"):
            if i < len(lines) and (not lines[i].strip() or lines[i].strip() in ("pass", "...")):
                issues.append({"type": "empty_except", "severity": "high",
                               "file": filepath, "line": i,
                               "description": "空except块吞异常",
                               "suggestion": "记录日志或指定异常类型"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_nesting(code, str(filepath)))
    issues.extend(_check_todo_fixme(code, str(filepath)))
    issues.extend(_check_long_line(code, str(filepath)))
    issues.extend(_check_empty_except(code, str(filepath)))
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
            "summary": "quality扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
