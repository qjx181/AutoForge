"""dims/config_scanner.py — 配置扫描器"""
import re
from pathlib import Path

DIMENSION = "configuration"


def _check_hardcoded_ip(code, filepath):
    if code is None:
        return []
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'(test_|example_|demo_)', filepath, re.IGNORECASE):
            continue
        ip_match = re.search(r'["\'](\d{1,3}\.){3}\d{1,3}(?::\d+)?["\']', stripped)
        if ip_match:
            val = ip_match.group()
            if "0.0.0.0" not in val and "127.0.0.1" not in val:
                issues.append({"type": "hardcoded_ip", "severity": "medium",
                               "file": filepath, "line": i,
                               "description": "硬编码IP: " + val,
                               "suggestion": "移到配置文件"})
    return issues


def _check_missing_timeout(code, filepath):
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'requests\.(get|post|put|delete)\s*\(', stripped):
            if "timeout" not in stripped:
                issues.append({"type": "missing_timeout", "severity": "medium",
                               "file": filepath, "line": i,
                               "description": "HTTP请求缺少timeout",
                               "suggestion": "添加timeout=30"})
    return issues


def _check_naked_env(code, filepath):
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'os\.environ\[', stripped) or re.search(r'os\.getenv\(', stripped):
            if "default" not in stripped.lower() and "or " not in stripped:
                issues.append({"type": "naked_env", "severity": "low",
                               "file": filepath, "line": i,
                               "description": "环境变量无默认值",
                               "suggestion": "添加默认值或验证"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_hardcoded_ip(code, str(filepath)))
    issues.extend(_check_missing_timeout(code, str(filepath)))
    issues.extend(_check_naked_env(code, str(filepath)))
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
            "summary": "configuration扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
