"""optimizer/security.py — 维度 2：安全审查扫描器

检测：SQL 注入、命令注入、密钥泄露、XSS、路径遍历、eval/shadowing 等。

依赖：复用 src.analysis.code_review.SecurityReviewer

使用：
    from optimizer.security import scan as scan_security
    result = scan_security("/path/to/project")
"""

import re
import ast
from pathlib import Path
from typing import Optional




def scan(project_path: str | Path) -> dict:
    """扫描项目安全漏洞。

    Args:
        project_path: 要扫描的项目根目录。

    Returns:
        dict: {
            "dimension": "security",
            "total_issues": int,
            "score": int,
            "issues": [dict, ...],
            "files_scanned": int,
        }
    """
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

        issues.extend(_check_sql_injection(code, rel_path))
        issues.extend(_check_command_injection(code, rel_path))
        issues.extend(_check_secret_leak(code, rel_path))
        issues.extend(_check_xss(code, rel_path))
        issues.extend(_check_eval_usage(code, rel_path))
        issues.extend(_check_path_traversal(code, rel_path))

    score = _compute_score(issues)
    return {
        "dimension": "security",
        "total_issues": len(issues),
        "score": score,
        "issues": issues,
        "files_scanned": files_scanned,
    }




def _check_sql_injection(code: str, file: Path) -> list[dict]:
    issues = []
    patterns = [
        (r'execute\s*\(\s*f["\']', "f-string SQL 拼接"),
        (r'execute\s*\(\s*["\'].*?%s.*?["\'].*?%\s*%', "%-format SQL 拼接"),
        (r'cursor\.execute\s*\(\s*f["\']', "f-string cursor.execute"),
        (r'"|\'.*?".*?\.format\s*\(.*?\).*?["\']', ".format() SQL 拼接"),
    ]
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        for pattern, desc in patterns:
            if re.search(pattern, line):
                issues.append({
                    "file": str(file),
                    "line": i,
                    "type": "sql_injection",
                    "severity": "critical",
                    "description": f"可能的 SQL 注入风险: {desc}",
                    "suggestion": "使用参数化查询（? 或 %s 参数绑定），避免字符串拼接 SQL",
                })
    return issues


def _check_command_injection(code: str, file: Path) -> list[dict]:
    issues = []
    patterns = [
        (r'os\.system\s*\(', "os.system() 存在命令注入风险"),
        (r'subprocess\.run\s*\([^)]*shell\s*=\s*True', "subprocess shell=True 风险"),
        (r'os\.popen\s*\(', "os.popen() 存在命令注入风险"),
        (r'eval\s*\(', "eval() 存在代码注入风险"),
        (r'exec\s*\(', "exec() 存在代码注入风险"),
    ]
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, desc in patterns:
            if re.search(pattern, line):
                issues.append({
                    "file": str(file),
                    "line": i,
                    "type": "command_injection",
                    "severity": "critical",
                    "description": desc,
                    "suggestion": "使用 subprocess.run([], shell=False) 列表参数形式，避免 shell=True",
                })
    return issues


def _check_secret_leak(code: str, file: Path) -> list[dict]:
    issues = []
    SECRET_RE = re.compile(
        r'(?i)(api[_-]?key|secret[_-]?key|access[_-]?key|'
        r'password|passwd|token|auth[_-]?token|'
        r'private[_-]?key|jwt[_-]?secret|'
        r'aws[_-]?secret|db[_-]?password|openai[_-]?key)'
    )
    SKIP_VALUES = re.compile(
        r'(?i)(your_|example_|test_|changeme|placeholder|'
        r'xxx|sk-[A-Za-z0-9]{5,10})',
    )
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        if re.search(SECRET_RE, line) and not re.search(SKIP_VALUES, line):
            if re.search(r'=\s*["\'][^"\']{10,}', line):
                issues.append({
                    "file": str(file),
                    "line": i,
                    "type": "secret_leak",
                    "severity": "critical",
                    "description": "发现疑似硬编码密钥或凭证",
                    "suggestion": "使用环境变量或 .env 文件存储敏感信息，代码中用 os.getenv() 读取",
                })
    return issues


def _check_xss(code: str, file: Path) -> list[dict]:
    issues = []
    patterns = [
        (r'render_template_string\s*\(', "render_template_string 可能导致 XSS"),
        (r'Markup\s*\(', "Markup() 可能绕过转义导致 XSS"),
    ]
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        for pattern, desc in patterns:
            if re.search(pattern, line):
                issues.append({
                    "file": str(file),
                    "line": i,
                    "type": "xss",
                    "severity": "high",
                    "description": desc,
                    "suggestion": "使用 Markup.escape() 显式转义，或使用 render_template() 而非 render_template_string",
                })
    return issues


def _check_eval_usage(code: str, file: Path) -> list[dict]:
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#") or "ast.literal_eval" in line:
            continue
        if re.search(r'\beval\s*\(', line):
            issues.append({
                "file": str(file),
                "line": i,
                "type": "eval_usage",
                "severity": "high",
                "description": "eval() 可能执行任意代码",
                "suggestion": "尽量避免 eval()，使用 ast.literal_eval() 或其他安全替代方案",
            })
    return issues


def _check_path_traversal(code: str, file: Path) -> list[dict]:
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'open\s*\([^)]*\+\s*request\.', line) or re.search(r'Path\s*\([^)]*\+\s*request\.', line):
            issues.append({
                "file": str(file),
                "line": i,
                "type": "path_traversal",
                "severity": "high",
                "description": "可能存在路径遍历漏洞（用户输入直接拼接到文件路径）",
                "suggestion": "对用户输入进行路径规范化（os.path.realpath）和边界检查",
            })
    return issues




def _should_skip(path: Path) -> bool:
    skip = {"__pycache__", ".pytest_cache", "venv", "env", ".venv", "node_modules", ".git", "build", "dist"}
    return any(p in skip for p in path.parts)


def _compute_score(issues: list[dict]) -> int:
    if not issues:
        return 100
    weights = {"critical": 15, "high": 8, "medium": 3, "low": 1}
    penalty = sum(weights.get(i.get("severity", "low"), 1) for i in issues)
    return max(0, min(100, 100 - penalty))


def _empty_result(msg: str) -> dict:
    return {
        "dimension": "security",
        "total_issues": 0,
        "score": 100,
        "issues": [],
        "files_scanned": 0,
        "error": msg,
    }
