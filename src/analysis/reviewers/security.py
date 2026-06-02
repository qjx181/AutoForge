"""code_review.py — PR 代码审查 Agent 模块
    SKIP_VALUES = re.compile(os.environ.get('SKIP_VALUES_PATTERN', r"(your_|example_|test_|changeme|placeholder|xxx|sk-[A-Za-z0-9]{5,10}|'[^']{1,5}')"), re.IGNORECASE)
自动审查代码变更，检测安全、性能、代码质量问题，输出质量报告。

模块结构:
  - SecurityReviewer    安全审查（SQL注入、命令注入、密钥泄露、XSS）
  - PerformanceReviewer 性能审查（N+1查询、sync I/O阻塞、内存泄漏）
  - QualityReviewer     代码质量审查（未用import、过深嵌套、硬编码值）
  - PRReviewer          综合 PR 审查（调用三个审查器，输出总评分和审核结论）

设计理由:
  - 纯 Python 标准库，无外部依赖，开箱即用
  - 正则 + AST 双重检测：正则捕获字面量，AST 捕获结构问题
  - 按严重级别分级（critical/high/medium/low），下游可灵活决策

面试官可能问:
  - 为什么不用 flake8/pylint？答：它们只检查质量不检查安全；此模块兼做安全和性能
  - 覆盖 JavaScript 吗？答：XSS 检测部分覆盖 JS，完整 JS 支持需要补充
  - 误报率怎么控制？答：白名单 + AST 分析减少正则误报
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import re
from pathlib import Path




class SecurityReviewer:
    """安全审查器

    检测 SQL 注入、命令注入、密钥泄露、XSS 四类安全问题。
    每项检测返回格式：{
        "type": str,              # 检测类型
        "severity": str,          # critical/high/medium/low
        "line": int,              # 行号
        "code": str,              # 问题代码片段
        "description": str,       # 问题描述
        "suggestion": str,        # 修复建议
    }
    """

    SECRET_KEYWORDS = re.compile(
        r"(?i)(api_key|secret_key|api_secret|access_key|secret_access|"
        r"private_key|password|passwd|token|auth_token|"
        r"aws_secret|db_password|jwt_secret|openai_key|app_secret)",
    )
    SKIP_VALUES = re.compile(
        r"(your_|example_|test_|changeme|placeholder|xxx|"
        r"sk-[A-Za-z0-9]{5,10}|'[^']{1,5}')",
        re.IGNORECASE,
    )

    @staticmethod
    def check_sql_injection(code: str) -> list[dict]:
        """检测 SQL 注入风险

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: SQL 注入问题列表

        Why:
            - 覆盖 f-string SQL、.format() SQL、% 格式化 SQL、字符串拼接 SQL 四种模式
            - 使用 row 内的 execute/sql 函数名作为锚点，减少误报
        """
        issues = []

        fstring_sql = re.finditer(
            r'(?:execute|executescript|cursor\.execute|\.sql)\s*\(\s*f["\']',
            code,
            re.IGNORECASE,
        )
        for m in fstring_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "critical",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "使用 f-string 拼接 SQL 查询，存在 SQL 注入风险",
                "suggestion": "使用参数化查询：cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))",
            })

        format_sql = re.finditer(
            r"(?:execute|cursor\.execute|\.sql)\s*\(\s*['\"].*?\{.*?\}.*?['\"]\s*\.\s*format\b",
            code,
        )
        for m in format_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "critical",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "使用 .format() 拼接 SQL 查询参数",
                "suggestion": "使用参数化查询替代 .format()",
            })

        pct_sql = re.finditer(
            r"(?:execute|cursor\.execute|\.sql)\s*\(\s*['\"].*?%[sd].*?['\"]\s*%\s*\(",
            code,
        )
        for m in pct_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "high",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "使用 % 格式化拼接 SQL 参数",
                "suggestion": "使用参数化查询：cursor.execute('...', (param,))",
            })

        concat_sql = re.finditer(
            r"""['"](SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b.*?['"]\s*\+""",
            code,
            re.IGNORECASE,
        )
        for m in concat_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "high",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "通过字符串拼接构建 SQL 查询",
                "suggestion": "使用参数化查询或 ORM 框架",
            })

        return issues

    @staticmethod
    def check_command_injection(code: str) -> list[dict]:
        """检测命令注入风险

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 命令注入问题列表

        Why:
            - subprocess.run/shell=True 是最危险的组合
            - os.system 和 os.popen 直接调用 shell，同样危险
            - 检测 String 模板中是否混入用户输入变量
        """
        issues = []

        patterns = [
            (r"subprocess\.run\(.*shell\s*=\s*True", "subprocess.run + shell=True", "critical"),
            (r"subprocess\.Popen\(.*shell\s*=\s*True", "subprocess.Popen + shell=True", "critical"),
            (r"subprocess\.call\(.*shell\s*=\s*True", "subprocess.call + shell=True", "critical"),
            (r"os\.system\s*\(", "os.system() 调用", "high"),
            (r"os\.popen\s*\(", "os.popen() 调用", "high"),
            (r"commands\.getoutput\s*\(", "commands.getoutput() 调用", "medium"),
            (r"shutil\.which\s*\(.*\+\s*", "shutil.which 参数拼接", "medium"),
        ]

        for pattern, desc, severity in patterns:
            for m in re.finditer(pattern, code):
                line = code[: m.start()].count("\n") + 1
                var_in_cmd = re.search(
                    r'["\'].*?\b(f|format|\%|{|\+|join)\b',
                    code.split("\n")[line - 1],
                )
                if var_in_cmd or severity == "critical":
                    issues.append({
                        "type": "command_injection",
                        "severity": severity,
                        "line": line,
                        "code": code.split("\n")[line - 1].strip()[:80],
                        "description": f"检测到 {desc}，存在命令注入风险",
                        "suggestion": "使用 subprocess.run(cmd, shell=False, capture_output=True) 替代，避免 shell 解释",
                    })

        return issues

    @staticmethod
    def check_secret_leak(code: str) -> list[dict]:
        """检测密钥泄露

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 密钥泄露问题列表

        Why:
            - 使用正则匹配常见密钥变量名
            - 跳过测试密钥和占位符，减少误报
            - 不检测 .env 文件引用（那是正确实践）
        """
        issues = []

        lines = code.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            if "import " in stripped:
                continue

            match = SecurityReviewer.SECRET_KEYWORDS.search(stripped)
            if match:
                key = match.group()
                value_match = re.search(r'=\s*["\'](.+?)["\']', stripped)
                if value_match:
                    value = value_match.group(1)
                    if SecurityReviewer.SKIP_VALUES.search(value):
                        continue
                    issues.append({
                        "type": "secret_leak",
                        "severity": "critical" if "password" in key.lower() or "secret" in key.lower() else "high",
                        "line": i,
                        "code": stripped[:80],
                        "description": f"在代码中硬编码了 {key}，值: '{value[:20]}...'",
                        "suggestion": "将敏感信息移至 .env 文件，通过 os.getenv() 读取",
                    })

        return issues

    @staticmethod
    def check_xss(code: str) -> list[dict]:
        """检测 XSS 风险

        Args:
            code: 源代码字符串（Python/JavaScript/HTML 混合）

        Returns:
            list[dict]: XSS 风险问题列表
        """
        issues = []

        patterns = [
            (r"innerHTML\s*=\s*[\"']", "innerHTML 设置用户内容", "high"),
            (r"outerHTML\s*=\s*[\"']", "outerHTML 设置用户内容", "high"),
            (r"document\.write\s*\(", "document.write() 写入内容", "high"),
            (r"\bmark_safe\s*\(", "Django mark_safe 绕过转义", "high"),
            (r"\|safe\b", "Jinja2 safe 过滤器（跳过转义）", "medium"),
            (r"insertAdjacentHTML\s*\(", "insertAdjacentHTML 未转义", "high"),
            (r"v-html\s*=", "Vue v-html（不转义）", "medium"),
        ]

        for pattern, desc, severity in patterns:
            for m in re.finditer(pattern, code):
                line = code[: m.start()].count("\n") + 1
                line_text = code.split("\n")[line - 1].strip()
                if "escaped" in line_text.lower() or "sanitize" in line_text.lower():
                    continue
                issues.append({
                    "type": "xss",
                    "severity": severity,
                    "line": line,
                    "code": line_text[:80],
                    "description": f"检测到 {desc}，可能导致 XSS",
                    "suggestion": "使用 textContent 替代 innerHTML，或对用户输入做 escape/DOMPurify 处理",
                })

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合安全审查

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 所有安全问题，按严重级别排序（critical → low）
        """
        issues = []
        issues.extend(cls.check_sql_injection(code))
        issues.extend(cls.check_command_injection(code))
        issues.extend(cls.check_secret_leak(code))
        issues.extend(cls.check_xss(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


