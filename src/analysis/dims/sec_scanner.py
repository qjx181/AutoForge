"""dims/sec_scanner.py — 安全扫描器"""
import re
from pathlib import Path

DIMENSION = "security"
SKIP_PATTERN = re.compile(os.environ.get('SKIP_PATTERN', r'(test_|example_|demo_|your_|placeholder|xxx)'), re.IGNORECASE)


def _check_sql_injection(code, filepath):
    issues = []
    if code is None:
        return issues
    patterns = [
        (r'execute\s*\(\s*f["\']', "SQL注入：f-string拼接SQL"),
        (r'execute\s*\(\s*["\'].*%s.*["\']\s*%', "SQL注入：%格式化"),
        (r'execute\s*\(\s*[^"\']+\s*\+\s*', "SQL注入：字符串拼接"),
        (r'execute\s*\(\s*[^"\',\)]+\s*\)', "SQL注入：变量拼接execute"),
    ]
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat, desc in patterns:
            if re.search(pat, stripped):
                issues.append({"type": "sql_injection", "severity": "critical",
                               "file": filepath, "line": i, "description": desc,
                               "suggestion": "使用参数化查询"})
    return issues


def _check_command_injection(code, filepath):
    issues = []
    patterns = [
        (r'os\.system\s*\(', "命令注入：os.system()"),
        (r'shell\s*=\s*True', "命令注入：shell=True"),
    ]
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat, desc in patterns:
            if re.search(pat, stripped):
                issues.append({"type": "command_injection", "severity": "critical",
                               "file": filepath, "line": i, "description": desc,
                               "suggestion": "使用subprocess.run([...], shell=False)"})
    return issues


def _check_secret_leak(code, filepath):
    issues = []
    secret_patterns = [
        (r'password\s*[=:]\s*["\'][^"\']+["\']', "硬编码Password"),
        (r'sk-[A-Za-z0-9]{32,}', "疑似API Secret Key"),
    ]
    for i, line in enumerate(code.split("\n"), 1):
        code_part = line[:line.index("#")] if "#" in line else line
        for pat, desc in secret_patterns:
            if re.search(pat, code_part) and not SKIP_PATTERN.search(code_part):
                issues.append({"type": "secret_leak", "severity": "critical",
                               "file": filepath, "line": i, "description": desc,
                               "suggestion": "使用环境变量"})
    return issues


def _check_eval(code, filepath):
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'\beval\s*\(', stripped) or re.search(r'\bexec\s*\(', stripped):
            issues.append({"type": "dangerous_eval", "severity": "high",
                           "file": filepath, "line": i, "description": "使用eval()/exec()",
                           "suggestion": "重构为直接逻辑"})
    return issues


def _scan_file(filepath):
    fp = Path(filepath)
    try:
        code = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    issues = []
    issues.extend(_check_sql_injection(code, str(filepath)))
    issues.extend(_check_command_injection(code, str(filepath)))
    issues.extend(_check_secret_leak(code, str(filepath)))
    issues.extend(_check_eval(code, str(filepath)))
    for issue in issues:
        issue["dimension"] = DIMENSION
    return issues


def scan(blueprint):
    all_issues = []
    for fp in blueprint.get_source_files(blueprint.language.primary):
        # 跳过扫描器自身文件，避免规则代码被误报
        if Path(fp).name.endswith("_scanner.py"):
            continue
        all_issues.extend(_scan_file(fp))
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))
    crit = sum(1 for i in all_issues if i["severity"] == "critical")
    high = sum(1 for i in all_issues if i["severity"] == "high")
    score = max(0, 100 - crit * 25 - high * 10)
    return {"dimension": DIMENSION, "score": score, "issues": all_issues,
            "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
            "issue_count": len(all_issues),
            "summary": "安全扫描完成：%d 个问题，评分 %d/100" % (len(all_issues), score)}
