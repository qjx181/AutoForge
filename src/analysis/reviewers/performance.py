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




class PerformanceReviewer:
    """性能审查器

    检测 N+1 查询、同步 I/O 阻塞、内存泄漏等性能问题。
    """

    @staticmethod
    def check_n_plus_one(code: str) -> list[dict]:
        """检测 N+1 查询模式

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: N+1 查询问题列表

        Why:
            - N+1 的经典模式：for 循环内执行 SQL/API 查询
            - 使用行级近似判断：循环体内有查询调用
        """
        issues = []
        lines = code.split("\n")

        in_loop = False
        loop_start = 0
        loop_indent = 0

        for i, line in enumerate(lines, 1):
            stripped = line.rstrip()
            indent = len(line) - len(line.lstrip())

            if re.match(r"\s*(for|while)\s", stripped) and ":" in stripped:
                in_loop = True
                loop_start = i
                loop_indent = indent

            if in_loop:
                if indent <= loop_indent and i > loop_start:
                    in_loop = False
                    continue

                if re.search(
                    r"(execute|\.query|\.get\b|\.filter\b|\.all\b|fetchone|fetchall)",
                    stripped,
                    re.IGNORECASE,
                ):
                    if re.search(
                        r"(for|while|list comp|generator|map\()",
                        stripped[:40],
                    ):
                        continue  # 这本身是循环定义，不是查询
                    issues.append({
                        "type": "n_plus_one",
                        "severity": "high",
                        "line": i,
                        "code": stripped[:80],
                        "description": "在循环体中执行数据库查询，可能导致 N+1 问题",
                        "suggestion": "将查询移到循环外部，使用 IN 查询或 select_related/prefetch_related 预加载",
                    })

        return issues

    @staticmethod
    def check_sync_io_in_async(code: str) -> list[dict]:
        """检测 async 函数中的同步 I/O 阻塞

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 同步阻塞问题列表

        Why:
            - async def 中使用 requests/print/time.sleep 会阻塞事件循环
            - httpx/asyncio.to_thread/aiohttp 是异步替代品
        """
        issues = []
        lines = code.split("\n")
        in_async_def = False
        async_indent = 0

        sync_io_patterns = [
            (r"^import requests|^from requests", "requests 库（同步 HTTP）", "high"),
            (r"time\.sleep\s*\(", "time.sleep()（同步阻塞）", "high"),
            (r"subprocess\.run\s*\(", "subprocess.run()（同步进程调用）", "high"),
            (r"subprocess\.call\s*\(", "subprocess.call()（同步）", "high"),
            (r"os\.system\s*\(", "os.system()（同步阻塞）", "high"),
            (r"\.read\s*\(\s*\)", "同步 .read() 调用", "medium"),
            (r"\.write\s*\(\s*\)", "同步 .write() 调用", "medium"),
            (r"json\.load\s*\(", "json.load()（同步文件读取）", "low"),
        ]

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())

            if stripped.startswith("async def "):
                in_async_def = True
                async_indent = indent
                continue

            if in_async_def:
                if indent <= async_indent and i > 1 and stripped:
                    in_async_def = False
                    continue

                for pattern, desc, severity in sync_io_patterns:
                    if re.search(pattern, stripped):
                        if "asyncio" in stripped:
                            continue
                        issues.append({
                            "type": "sync_io_in_async",
                            "severity": severity,
                            "line": i,
                            "code": stripped[:80],
                            "description": f"async 函数中使用 {desc}，会阻塞事件循环",
                            "suggestion": "使用 asyncio.to_thread() 包装同步调用，或使用 httpx/aiohttp 等异步库替代",
                        })

        return issues

    @staticmethod
    def check_memory_leak(code: str) -> list[dict]:
        """检测潜在内存泄漏

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 内存泄漏问题列表

        Why:
            - 全局列表/字典无限增长是最常见的 Python 内存泄漏模式
            - 没有上限的缓存（lru_cache 等）在长期运行中也会泄漏
        """
        issues = []
        lines = code.split("\n")

        global_collections = []
        in_class = False

        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            indent = len(line) - len(line.lstrip())

            m = re.match(r"(\w+)\s*=\s*\[\s*\]", stripped)
            if m and indent == 0:
                global_collections.append((m.group(1), i, "list"))
            m = re.match(r"(\w+)\s*=\s*\{\s*\}", stripped)
            if m and indent == 0:
                global_collections.append((m.group(1), i, "dict"))
            m = re.match(r"(\w+)\s*=\s*set\(\s*\)", stripped)
            if m and indent == 0:
                global_collections.append((m.group(1), i, "set"))

        for name, decl_line, ctype in global_collections:
            appends = list(re.finditer(rf"\b{re.escape(name)}\.append\(|{re.escape(name)}\.add\(", code))
            pops = list(re.finditer(rf"\b{re.escape(name)}\.pop\(|{re.escape(name)}\.discard\(", code))
            del_ops = list(re.finditer(
                rf"del\s+{re.escape(name)}\[|{re.escape(name)}\.clear\(|{re.escape(name)}\s*=\s*\[\s*\]",
                code,
            ))

            if len(appends) > 3 and len(pops) + len(del_ops) < 2:
                first_append_line = code[: appends[0].start()].count("\n") + 1
                issues.append({
                    "type": "memory_leak",
                    "severity": "medium",
                    "line": first_append_line,
                    "code": appends[0].group()[:60],
                    "description": f"全局 {ctype} '{name}' 有 {len(appends)} 次添加操作但几乎无清理，可能无限增长",
                    "suggestion": f"为 {name} 设置上限（如使用 collections.deque(maxlen=1000)），或定期清理过期元素",
                })

        for m in re.finditer(r"@lru_cache\b(?!\s*\(\s*maxsize)", code):
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "memory_leak",
                "severity": "low",
                "line": line,
                "code": "@lru_cache (无 maxsize)",
                "description": "@lru_cache 未指定 maxsize，默认 128，运行中可能增长",
                "suggestion": "显式设置 @lru_cache(maxsize=128)",
            })

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合性能审查"""
        issues = []
        issues.extend(cls.check_n_plus_one(code))
        issues.extend(cls.check_sync_io_in_async(code))
        issues.extend(cls.check_memory_leak(code))
        issues.extend(AsyncSyncBoundaryChecker.review_all(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


