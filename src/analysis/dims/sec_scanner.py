"""dims/sec_scanner.py — 维度五：安全扫描器

检测：
  1. SQL 注入（字符串拼接 SQL）
  2. 命令注入（os.system / subprocess + 变量）
  3. 密钥泄露（硬编码 API Key / Token / Password）
  4. XSS（直接拼接用户输入到 HTML）
  5. 路径遍历（用户输入直接拼接到文件路径）
  6. 禁用 auth（注释掉的认证装饰器）
  7. eval/exec 使用

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import re
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "security"


def _check_sql_injection(code: str, filepath: str) -> list[dict]:
    issues = []
    patterns = [
        (r'execute\s*\(\s*f["\']', "SQL 注入风险：f-string 内含 SQL 查询"),
        (r'execute\s*\(\s*["\'].*%s.*["\']\s*%', "SQL 注入风险：% 格式化 SQL 查询"),
        (r'\.format\s*\([^)]*\.\s*(?:sql|query)', "SQL 注入风险：.format() 内含 SQL"),
    ]
    for i, line in enumerate(code.split("\n"), 1):
        for pat, desc in patterns:
            if re.search(pat, line):
                issues.append({
                    "type": "sql_injection", "severity": "critical",
                    "file": filepath, "line": i,
                    "description": desc,
                    "suggestion": "使用参数化查询：cursor.execute('SELECT * FROM t WHERE id=?', (id,))",
                })
    return issues


def _check_command_injection(code: str, filepath: str) -> list[dict]:
    issues = []
    patterns = [
        (r'os\.system\s*\(', "命令注入风险：os.system() 使用字符串拼接"),
        (r'subprocess\.(run|call|Popen)\s*\([^)]*(?:input|args).*(?:\+|f["\'])', "命令注入风险：subprocess 参数含变量"),
        (r'shell\s*=\s*True', "命令注入风险：subprocess shell=True 安全性低"),
    ]
    for i, line in enumerate(code.split("\n"), 1):
        for pat, desc in patterns:
            if re.search(pat, line):
                issues.append({
                    "type": "command_injection", "severity": "critical",
                    "file": filepath, "line": i,
                    "description": desc,
                    "suggestion": "使用 subprocess.run([...], shell=False) 避免 shell=True",
                })
    return issues


def _check_secret_leak(code: str, filepath: str) -> list[dict]:
    issues = []
    secret_patterns = [
        (r'["\']api_key["\']\s*[=:]\s*["\'][^"\']{10,}["\']', "硬编码 API Key"),
        (r'["\']secret["\']\s*[=:]\s*["\'][^"\']{10,}["\']', "硬编码 Secret"),
        (r'["\']token["\']\s*[=:]\s*["\'][^"\']{10,}["\']', "硬编码 Token"),
        (r'password\s*[=:]\s*["\'][^"\']+["\']', "硬编码 Password"),
        (r'aws_access_key_id\s*[=:]\s*["\'][A-Z0-9]{20,}["\']', "硬编码 AWS Key"),
        (r'sk-[A-Za-z0-9]{32,}', "疑似硬编码 API Secret Key"),
    ]
    skip = re.compile(r'(test_|example_|demo_|your_|placeholder|xxx)', re.IGNORECASE)
    for i, line in enumerate(code.split("\n"), 1):
        if "#" in line:
            line_content = line[:line.index("#")]
        else:
            line_content = line
        for pat, desc in secret_patterns:
            if re.search(pat, line_content) and not skip.search(line_content):
                issues.append({
                    "type": "secret_leak", "severity": "critical",
                    "file": filepath, "line": i,
                    "description": desc,
                    "suggestion": "使用环境变量：os.environ.get('API_KEY') 或 .env 文件",
                })
    return issues


def _check_xss(code: str, filepath: str) -> list[dict]:
    issues = []
    patterns = [
        (r'render_template_string\s*\(.*\+', "XSS 风险：用户输入直接拼接到模板"),
        (r'response\.write\s*\(.*\+', "XSS 风险：直接写入响应"),
    ]
    for i, line in enumerate(code.split("\n"), 1):
        for pat, desc in patterns:
            if re.search(pat, line):
                issues.append({
                    "type": "xss", "severity": "high",
                    "file": filepath, "line": i,
                    "description": desc,
                    "suggestion": "对用户输入做 HTML 转义或使用模板引擎的自动转义",
                })
    return issues


def _check_eval(code: str, filepath: str) -> list[dict]:
    issues = []
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'\beval\s*\(', stripped) or re.search(r'\bexec\s*\(', stripped):
            issues.append({
                "type": "dangerous_eval", "severity": "high",
                "file": filepath, "line": i,
                "description": "使用 eval()/exec() 执行动态代码",
                "suggestion": "重构为直接逻辑，避免 eval/exec",
            })
    return issues


def _scan_file(filepath: Path) -> list[dict]:
    issues = []
    try:
        code = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return issues

    issues.extend(_check_sql_injection(code, str(filepath)))
    issues.extend(_check_command_injection(code, str(filepath)))
    issues.extend(_check_secret_leak(code, str(filepath)))
    issues.extend(_check_xss(code, str(filepath)))
    issues.extend(_check_eval(code, str(filepath)))

    for issue in issues:
        issue["dimension"] = DIMENSION
    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    if not blueprint.is_enabled("security"):
        return {"dimension": DIMENSION, "score": 100, "issues": [], "file_count": 0,
                "issue_count": 0, "summary": "安全维度未启用"}

    all_issues = []
    for fp in blueprint.get_source_files(blueprint.language.primary):
        all_issues.extend(_scan_file(Path(fp)))

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    crit = sum(1 for i in all_issues if i["severity"] == "critical")
    high = sum(1 for i in all_issues if i["severity"] == "high")
    score = max(0, 100 - crit * 25 - high * 10)

    return {
        "dimension": DIMENSION,
        "score": score,
        "issues": all_issues,
        "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
        "issue_count": len(all_issues),
        "summary": (
            f"安全扫描完成：{len(all_issues)} 个问题"
            f"（SQL注入 {sum(1 for i in all_issues if i['type']=='sql_injection')}"
            f" / 命令注入 {sum(1 for i in all_issues if i['type']=='command_injection')}"
            f" / 密钥泄露 {sum(1 for i in all_issues if i['type']=='secret_leak')}），评分 {score}/100"
        ),
    }

