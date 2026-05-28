"""code_review.py — PR 代码审查 Agent 模块

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




class QualityReviewer:
    """代码质量审查器

    检测未使用的 import、过深嵌套、硬编码值、缺失异常处理、过长函数。
    """

    @staticmethod
    def check_unused_import(code: str) -> list[dict]:
        """检测未使用的 import

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 未使用 import 问题列表

        Why:
            - 通过 AST 解析精确检测，比正则更可靠
            - 只检测简单 import（from X import Y），不处理动态导入
        """
        issues = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        imports = {}  # {name: line_number}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports[name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports[name] = node.lineno

        if not imports:
            return issues

        used_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                used_names.add(node.attr)

        for name, lineno in imports.items():
            if name.startswith("_"):
                continue
            if name not in used_names:
                issues.append({
                    "type": "unused_import",
                    "severity": "low",
                    "line": lineno,
                    "code": "",
                    "description": f"导入了 '{name}' 但未使用",
                    "suggestion": f"移除 import {name} 或确认是否需要保留",
                })

        return issues

    @staticmethod
    def check_deep_nesting(code: str, max_depth: int = 4) -> list[dict]:
        """检测过深嵌套

        Args:
            code: 源代码字符串
            max_depth: 最大允许嵌套深度（默认 4）

        Returns:
            list[dict]: 过深嵌套问题列表

        Why:
            - 基于缩进级别检测，适用于 Python
            - 嵌套深度超过 4 层通常意味着需要重构
        """
        issues = []
        lines = code.split("\n")

        current_depth = 0
        depth_stack = []  # [(depth, line_number, keyword)]
        in_multiline = False

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            if not stripped or stripped.startswith("#") or stripped.startswith('"""'):
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    in_multiline = not in_multiline
                continue
            if in_multiline:
                continue

            indent = len(line) - len(line.lstrip())
            indent_level = indent // 4  # 假设 4 空格缩进

            nesting_keywords = [
                r"if\s", r"elif\s", r"else\s*:", r"for\s", r"while\s",
                r"def\s", r"class\s", r"try\s*:", r"except\s", r"finally\s*:",
                r"with\s", r"async\s+def\s", r"async\s+for\s", r"async\s+with\s",
            ]

            for kw in nesting_keywords:
                if re.match(rf"\s*{kw}", line):
                    depth_stack.append((indent_level, i, kw[:10]))
                    break

        for depth, line_num, keyword in depth_stack:
            if depth > max_depth:
                issues.append({
                    "type": "deep_nesting",
                    "severity": "medium",
                    "line": line_num,
                    "code": lines[line_num - 1].strip()[:60],
                    "description": f"嵌套深度 {depth} 层（{keyword}），超过建议的 {max_depth} 层",
                    "suggestion": "提取为独立函数、使用 guard clause 提前返回，或合并条件",
                })

        return issues

    @staticmethod
    def check_hardcoded_values(code: str) -> list[dict]:
        """检测硬编码值（魔数、硬编码 URL、硬编码路径）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 硬编码值问题列表
        """
        issues = []
        lines = code.split("\n")

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or "import " in stripped:
                continue

            magic_num = re.search(r"=\s*(\d{4,})\s*(?:#|$|\])", stripped)
            if magic_num:
                val = int(magic_num.group(1))
                if val not in (0, 1, -1, 80, 443, 8080, 8000, 3000, 5000, 6379, 3306, 5432):
                    issues.append({
                        "type": "hardcoded_value",
                        "severity": "low",
                        "line": i,
                        "code": stripped[:60],
                        "description": f"硬编码魔数 {val}",
                        "suggestion": "提取为命名常量：MAX_RETRIES = 3",
                    })

            url_match = re.search(r'["\'](https?://[^"\']+)["\']', stripped)
            if url_match:
                url = url_match.group(1)
                if "localhost" not in url and "127.0.0.1" not in url and not re.search(
                    r"example\.com|test|cdn\.",
                    url,
                ):
                    issues.append({
                        "type": "hardcoded_value",
                        "severity": "low",
                        "line": i,
                        "code": stripped[:60],
                        "description": f"硬编码 URL: {url[:40]}...",
                        "suggestion": "将 URL 移至配置文件或环境变量",
                    })

        return issues

    @staticmethod
    def check_missing_error_handling(code: str) -> list[dict]:
        """检测缺失异常处理

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 缺失异常处理问题列表
        """
        issues = []

        dangerous_calls = [
            (r"int\(|float\(|bool\(", "类型转换", "输入值非预期格式"),
            (r"open\(|\.read\(|\.write\(", "文件 I/O", "文件不存在或权限不足"),
            (r"json\.loads\(|json\.dump\(", "JSON 解析", "JSON 格式错误"),
            (r"requests\.(get|post|put|delete)\(", "HTTP 请求", "网络异常或超时"),
            (r"\.execute\(", "数据库查询", "SQL 执行错误"),
            (r"subprocess\.", "子进程", "子进程执行失败"),
        ]

        lines = code.split("\n")
        for pattern, op_name, reason in dangerous_calls:
            for m in re.finditer(pattern, code):
                line = code[: m.start()].count("\n") + 1
                try_nearby = False
                for j in range(max(0, line - 12), line - 1):
                    if re.match(r"\s*try\s*:", lines[j]) if j < len(lines) else False:
                        try_nearby = True
                        break

                if not try_nearby:
                    issues.append({
                        "type": "missing_error_handling",
                        "severity": "medium",
                        "line": line,
                        "code": code.split("\n")[line - 1].strip()[:60],
                        "description": f"检测到 {op_name} 调用但未在 try/except 中保护（可能原因：{reason}）",
                        "suggestion": f"用 try/except 包装 {op_name} 调用，处理可能的异常",
                    })

        return issues

    @staticmethod
    def check_long_functions(code: str, max_lines: int = 80) -> list[dict]:
        """检测过长函数

        Args:
            code: 源代码字符串
            max_lines: 函数最大建议行数（默认 80）

        Returns:
            list[dict]: 过长函数问题列表
        """
        issues = []
        lines = code.split("\n")

        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"\s*def\s+(\w+)\s*\(", line)
            if m:
                func_name = m.group(1)
                func_start = i
                func_indent = len(line) - len(line.lstrip())
                i += 1
                while i < len(lines):
                    l = lines[i]
                    indent = len(l) - len(l.lstrip())
                    if l.strip() and indent <= func_indent:
                        break
                    i += 1
                func_lines = i - func_start
                if func_lines > max_lines and not func_name.startswith("_"):
                    issues.append({
                        "type": "long_function",
                        "severity": "low",
                        "line": func_start + 1,
                        "code": f"def {func_name}()",
                        "description": f"函数 '{func_name}' 共 {func_lines} 行，超过建议的 {max_lines} 行",
                        "suggestion": "将函数拆分成多个小函数，每个函数只做一件事",
                    })
            else:
                i += 1

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合代码质量审查"""
        issues = []
        issues.extend(cls.check_unused_import(code))
        issues.extend(cls.check_deep_nesting(code))
        issues.extend(cls.check_hardcoded_values(code))
        issues.extend(cls.check_missing_error_handling(code))
        issues.extend(cls.check_long_functions(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


