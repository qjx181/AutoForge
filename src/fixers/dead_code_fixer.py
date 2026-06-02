"""dead_code_fixer — 自动删除不可达代码

规则策略：检测 return/break/continue 之后的同缩进代码块并删除。
置信度 0.75（return 后代码确实不可达，但可能有意保留注释或调试代码）。
"""

import ast
import logging
from pathlib import Path

from src.core.adapters_pkg.fixer_adapter import FixerAdapter
from src.core.adapters_pkg.issue import Issue
from src.core.adapters_pkg.fix_result import FixResult

logger = logging.getLogger(__name__)

# 触发不可达的语句关键词
_DEAD_LINE_KEYWORDS = ("return ", "return\n", "return\t", "break\n", "break\t",
                       "continue\n", "continue\t", "raise ", "raise\n", "raise\t")


class DeadCodeFixer(FixerAdapter):
    """自动删除 return/break/continue 之后的不可达代码。"""

    @property
    def name(self) -> str:
        return "dead_code_fixer"

    @property
    def supported_types(self) -> list[str]:
        return ["dead_code"]

    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        target_file = project_root / issue.file
        if not target_file.exists():
            return self._fail(issue, f"文件不存在: {target_file}")

        try:
            content = target_file.read_text(encoding="utf-8")
        except Exception as e:
            return self._fail(issue, f"读取文件失败: {e}")

        lines = content.splitlines(keepends=True)
        dead_line = issue.line  # scanner 报告的不可达代码行号

        if dead_line < 1 or dead_line > len(lines):
            return self._fail(issue, f"行号越界: {dead_line}")

        # 分发：未调用函数 vs 不可达代码
        line_content = lines[dead_line - 1].strip()
        if line_content.startswith(("def ", "async def ")):
            return self._fix_uncalled_function(issue, lines, target_file, dead_line)

        # 找到死代码块的范围：从 dead_line 开始，到下一个非空且缩进 <= 的行
        dead_indent = self._get_indent(lines[dead_line - 1])
        end_line = dead_line
        for i in range(dead_line, len(lines)):
            line = lines[i]
            if line.strip() == "":
                end_line = i + 1  # 空行也算死代码的一部分（保留）
                continue
            indent = self._get_indent(line)
            if indent < dead_indent:
                break
            if indent == dead_indent and not self._is_continuation(line, lines, i):
                end_line = i + 1
            else:
                end_line = i + 1

        # 回退尾部空行
        while end_line > dead_line and lines[end_line - 1].strip() == "":
            end_line -= 1

        # 构建新内容
        removed_count = end_line - dead_line + 1
        removed_lines = lines[dead_line - 1:end_line]
        new_lines = lines[:dead_line - 1] + lines[end_line:]
        new_content = "".join(new_lines)

        # 验证语法
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return self._fail(issue, f"删除后语法错误: {e}")

        return FixResult(
            success=True,
            action=f"删除不可达代码: 行 {dead_line}-{end_line} ({removed_count} 行)",
            confidence=0.75,
            fixer=self.name,
            issue_type=issue.type,
            file=issue.file,
            line=issue.line,
            diff=self._make_diff(removed_lines, dead_line),
        )

    def _get_indent(self, line: str) -> int:
        return len(line) - len(line.lstrip())

    def _is_continuation(self, line: str, lines: list[str], idx: int) -> bool:
        """判断是否是多行语句的延续（如括号未闭合）。"""
        # 简单判断：如果前面有未闭合的括号，认为是延续
        open_parens = 0
        for i in range(max(0, idx - 5), idx + 1):
            for ch in lines[i]:
                if ch == "(":
                    open_parens += 1
                elif ch == ")":
                    open_parens -= 1
        return open_parens > 0

    def _fix_uncalled_function(self, issue: Issue, lines: list[str],
                               target_file: Path, dead_line: int) -> FixResult:
        """删除未被调用的函数定义（AST 精确定位，含装饰器）。

        两种场景触发此分支：
        1. quality_scanner 检测到函数定义后从未被同一文件调用
        2. 未来 scanner 报告"孤立函数"类 dead_code

        confidence=0.5：函数可能被外部模块 import 调用，
        此 fixer 只处理同一文件内确认无引用的情况。
        """
        try:
            tree = ast.parse("".join(lines))
        except SyntaxError:
            return self._fail(issue, "文件语法错误")

        # 在 AST 中定位目标函数
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno == dead_line:
                    target_func = node
                    break

        if not target_func:
            return self._fail(issue, f"行 {dead_line} 未找到函数定义")

        if target_func.end_lineno is None:
            return self._fail(issue, f"无法确定函数结束行")

        # 含装饰器时从装饰器首行开始
        start_line = (target_func.decorator_list[0].lineno
                      if target_func.decorator_list
                      else target_func.lineno)
        end_line = target_func.end_lineno

        # 删除函数体 + 尾部多余空行
        removed_lines = lines[start_line - 1:end_line]
        new_lines = lines[:start_line - 1] + lines[end_line:]
        new_content = "".join(new_lines)

        # 语法校验
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return self._fail(issue, f"删除后语法错误: {e}")

        return FixResult(
            success=True,
            action=f"删除未调用函数: 行 {start_line}-{end_line} ({end_line - start_line + 1} 行)",
            confidence=0.5,
            fixer=self.name,
            issue_type=issue.type,
            file=issue.file,
            line=issue.line,
            diff=self._make_diff(removed_lines, start_line),
        )

    def _make_diff(self, removed_lines: list[str], start_line: int) -> str:
        diff_lines = []
        for i, line in enumerate(removed_lines):
            diff_lines.append(f"--- {start_line + i}: {line.rstrip()}")
        diff_lines.append(f"+++ (deleted {len(removed_lines)} lines)")
        return "\n".join(diff_lines)

    def _fail(self, issue: Issue, error: str) -> FixResult:
        return FixResult(
            success=False,
            error=error,
            confidence=0.0,
            fixer=self.name,
            issue_type=issue.type,
            file=issue.file,
            line=issue.line,
        )
