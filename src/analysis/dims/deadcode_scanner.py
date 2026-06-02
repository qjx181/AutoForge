"""dims/deadcode_scanner.py — 死代码扫描器"""
import re
import ast
from pathlib import Path

DIMENSION = "deadcode"


def _check_unused_import(code, filepath):
    if code is None:
        return []
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            if "import *" in stripped:
                continue
            match = re.search(r'import\s+(\w+)(?:\s+as\s+(\w+))?', stripped)
            if match:
                alias = match.group(2) or match.group(1)
                rest = "\n".join(lines[:i - 1] + lines[i:])
                if rest.count(alias) <= 1:
                    issues.append({"type": "unused_import", "severity": "medium",
                                   "file": filepath, "line": i,
                                   "description": "未使用的导入: " + alias,
                                   "suggestion": "删除未使用的导入"})
    return issues


def _check_unreachable_code(code, filepath):
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped in ("return", "raise", "break", "continue"):
            indent = len(line) - len(line.lstrip())
            for j in range(i, min(i + 5, len(lines))):
                next_stripped = lines[j].strip()
                next_indent = len(lines[j]) - len(lines[j].lstrip())
                if not next_stripped or next_stripped.startswith("#"):
                    continue
                if next_indent > indent and not next_stripped.startswith(("else:", "elif ", "except:", "except ", "finally:")):
                    issues.append({"type": "unreachable_code", "severity": "medium",
                                   "file": filepath, "line": j + 1,
                                   "description": stripped + "后有不可达代码",
                                   "suggestion": "删除或重构"})
                    break
                elif next_indent <= indent:
                    break
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_unused_import(code, str(filepath)))
    issues.extend(_check_unreachable_code(code, str(filepath)))
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
            "summary": "deadcode扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
