"""dims/doc_scanner.py — 文档扫描器"""
import re
from pathlib import Path

DIMENSION = "documentation"


def _check_missing_docstring(code, filepath):
    issues = []
    if code is None:
        return []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("class ") and stripped.endswith(":"):
            has_doc = False
            for j in range(i, min(i + 3, len(lines))):
                if '"""' in lines[j] or "'''" in lines[j]:
                    has_doc = True
                    break
            if not has_doc:
                issues.append({"type": "missing_docstring", "severity": "low",
                               "file": filepath, "line": i,
                               "description": "类缺少docstring",
                               "suggestion": "添加类文档"})
        if stripped.startswith("def ") and not stripped[4:].startswith("_"):
            has_doc = False
            for j in range(i, min(i + 3, len(lines))):
                if '"""' in lines[j] or "'''" in lines[j]:
                    has_doc = True
                    break
            if not has_doc:
                issues.append({"type": "missing_docstring", "severity": "low",
                               "file": filepath, "line": i,
                               "description": "公开方法缺少docstring",
                               "suggestion": "添加方法文档"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_missing_docstring(code, str(filepath)))
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
            "summary": "documentation扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
