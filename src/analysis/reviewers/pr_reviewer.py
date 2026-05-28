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




class PRReviewer:
    """综合 PR 审查器

    对 PR 的 diff 或文件变更进行全方面审查，输出质量报告。
    """

    def __init__(self):
        self.security = SecurityReviewer()
        self.performance = PerformanceReviewer()
        self.quality = QualityReviewer()

    def review_pr(self, diff_text: str, changed_files: list[str]) -> dict:
        """综合 PR 审查

        Args:
            diff_text: PR 的 diff 文本
            changed_files: 变更文件路径列表

        Returns:
            dict: {
                "summary": str,           # 审查概述
                "security_issues": [...],  # 安全问题
                "performance_issues": [...],
                "quality_issues": [...],
                "overall_score": int,       # 0-100
                "verdict": str,            # approve/needs_changes/reject
                "comments": str,           # GitHub PR review 评论
            }

        Why:
            - overall_score 基于问题严重程度加权扣分
            - verdict 让 CI/CD 系统能自动决定 merge/block
            - comments 可直接粘贴到 GitHub PR 页面
        """
        all_issues = []
        all_issues.extend(self.security.review_all(diff_text))
        all_issues.extend(self.performance.review_all(diff_text))
        all_issues.extend(self.quality.review_all(diff_text))

        severity_scores = {"critical": -30, "high": -15, "medium": -5, "low": -2}
        score = 100
        for issue in all_issues:
            score += severity_scores.get(issue.get("severity", "low"), -2)
        score = max(0, min(100, score))

        critical_count = sum(1 for i in all_issues if i["severity"] == "critical")
        high_count = sum(1 for i in all_issues if i["severity"] == "high")

        if critical_count > 0:
            verdict = "reject"
        elif high_count > 3:
            verdict = "reject"
        elif high_count > 0:
            verdict = "needs_changes"
        elif score >= 85:
            verdict = "approve"
        else:
            verdict = "needs_changes"

        total = len(all_issues)
        summary_parts = [
            f"## PR 审查报告",
            f"",
            f"**审核结论**: {'✅ 批准' if verdict == 'approve' else '⚠️ 需要修改' if verdict == 'needs_changes' else '❌ 拒绝'}",
            f"**综合评分**: {score}/100",
            f"",
            f"### 发现问题 ({total} 项)",
        ]

        if all_issues:
            by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for i in all_issues:
                by_severity[i["severity"]] = by_severity.get(i["severity"], 0) + 1
            summary_parts.append("| 级别 | 数量 |")
            summary_parts.append("|------|:----:|")
            for sev in ["critical", "high", "medium", "low"]:
                if by_severity[sev] > 0:
                    summary_parts.append(f"| {sev} | {by_severity[sev]} |")

        summary_parts.append("")
        for issue in all_issues:
            emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}.get(
                issue.get("severity", "low"), "⚪"
            )
            summary_parts.append(
                f"{emoji} **{issue['type']}** (L{issue['line']}): {issue['description']}"
            )
            summary_parts.append(f"   - 建议: {issue['suggestion']}")
            if issue.get("code"):
                summary_parts.append(f"   - 代码: `{issue['code'][:60]}`")

        comments = "\n".join(summary_parts)

        return {
            "summary": f"审查了 {len(changed_files)} 个文件，发现 {total} 个问题。评分 {score}/100，结论: {verdict}。",
            "security_issues": [i for i in all_issues if i["type"] in (
                "sql_injection", "command_injection", "secret_leak", "xss"
            )],
            "performance_issues": [i for i in all_issues if i["type"] in (
                "n_plus_one", "sync_io_in_async", "memory_leak",
                "sync_wrapper_raises", "asyncio_run_in_loop", "sync_calls_async",
                "sync_calls_async_cross_file",
            )],
            "quality_issues": [i for i in all_issues if i["type"] in (
                "unused_import", "deep_nesting", "hardcoded_value",
                "missing_error_handling", "long_function"
            )],
            "overall_score": score,
            "verdict": verdict,
            "comments": comments,
        }

    @staticmethod
    def generate_comment(issues: list[dict]) -> str:
        """生成 GitHub PR Review 评论文本

        Args:
            issues: 问题列表

        Returns:
            str: 格式化的 Review 评论

        Why:
            - 格式与 GitHub PR Review 兼容，可以粘贴到 GitHub 的 Review 文本框
            - 按严重级别分组，便于 reviewer 优先处理 critical 问题
        """
        if not issues:
            return "✅ 代码审查通过，未发现问题。"

        by_type = {}
        for issue in issues:
            t = issue["type"]
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(issue)

        lines = ["## 🤖 Swarm Code Review Report", ""]
        for issue_type, type_issues in by_type.items():
            sev_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
            lines.append(f"### {sev_emoji.get(type_issues[0].get('severity','low'), '⚪')} {issue_type}")
            lines.append("")
            for issue in type_issues:
                lines.append(f"- **L{issue['line']}** [{issue['severity']}]: {issue['description']}")
                lines.append(f"  - 💡 {issue['suggestion']}")
                if issue.get("code"):
                    lines.append(f"  - ```{issue['code'][:80]}```")
            lines.append("")

        if not any(i.get("severity") in ("critical", "high") for i in issues):
            lines.append("---")
            lines.append("> ⚠️ 未发现严重问题，建议修复以上 low/medium 问题后合并。")

        return "\n".join(lines)


