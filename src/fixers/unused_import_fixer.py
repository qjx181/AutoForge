"""unused_import_fixer — 自动删除未使用的 import 语句

规则策略：解析 issue 描述提取未使用的 import 名，用 AST 定位并删除对应行。
置信度 0.85（纯规则，误删概率低，但可能有动态使用场景如 __all__）。
"""

import ast
import logging
import re
from pathlib import Path

from src.core.adapters_pkg.fixer_adapter import FixerAdapter
from src.core.adapters_pkg.issue import Issue
from src.core.adapters_pkg.fix_result import FixResult

logger = logging.getLogger(__name__)


class UnusedImportFixer(FixerAdapter):
    """自动删除未使用的 import 语句。"""

    @property
    def name(self) -> str:
        return "unused_import_fixer"

    @property
    def supported_types(self) -> list[str]:
        return ["unused_import"]

    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        target_file = project_root / issue.file
        if not target_file.exists():
            return self._fail(issue, f"文件不存在: {target_file}")

        try:
            content = target_file.read_text(encoding="utf-8")
        except Exception as e:
            return self._fail(issue, f"读取文件失败: {e}")

        # 从 description 提取未使用的 import 名
        unused_name = self._extract_unused_name(issue.description)
        if not unused_name:
            return self._fail(issue, f"无法从描述中提取未使用模块名: {issue.description[:100]}")

        lines = content.splitlines(keepends=True)
        if issue.line < 1 or issue.line > len(lines):
            return self._fail(issue, f"行号越界: {issue.line}")

        target_line = lines[issue.line - 1]
        new_line = self._remove_unused_from_line(target_line, unused_name)

        if new_line is None:
            return self._fail(issue, f"无法从行中移除 {unused_name}: {target_line.strip()}")

        if new_line == target_line:
            return self._fail(issue, "行内容未变化")

        # 构建新内容
        new_lines = lines[:issue.line - 1] + ([new_line] if new_line else []) + lines[issue.line:]
        new_content = "".join(new_lines)

        # 验证语法
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return self._fail(issue, f"修改后语法错误: {e}")

        return FixResult(
            success=True,
            action=f"删除未使用的 import: {unused_name} (行 {issue.line})",
            confidence=0.85,
            fixer=self.name,
            issue_type=issue.type,
            file=issue.file,
            line=issue.line,
            diff=self._make_diff(target_line, new_line, issue.line),
            error=None,
        )

    def _extract_unused_name(self, description: str) -> str:
        """从 issue 描述中提取未使用的模块名。

        支持中英文格式：
          - "导入了 'Optional' 但未在文件中使用"
          - "Unused import: os"
          - "unused import 'os'"
          - "Import 'os' is not used"
          - "Module 'os' imported but unused"
        """
        patterns = [
            r"导入了\s*['\"](\w+)['\"]",           # 中文: 导入了 'Optional'
            r"[Uu]nused\s+import[:\s]+['\"]?(\w+)",  # 英文: Unused import: os
            r"[Ii]mport\s+['\"](\w+)['\"]?\s+.*not\s+used",
            r"[Mm]odule\s+['\"](\w+)['\"]?\s+imported\s+but\s+unused",
            r"'(\w+)'\s+imported\s+but\s+unused",
        ]
        for p in patterns:
            m = re.search(p, description)
            if m:
                return m.group(1)
        # fallback: 取最后一个单词
        words = description.split()
        if words:
            return words[-1].strip("'\"")
        return ""

    def _remove_unused_from_line(self, line: str, unused_name: str) -> str | None:
        """从 import 行中移除未使用的名称。

        处理场景：
          - import os  → 整行删除
          - import os, sys  → 只删 os
          - from os import path, getcwd  → 只删 path
          - from os import (path, getcwd)  → 只删 path，保留括号
        """
        stripped = line.strip()

        # import X 或 import X, Y, Z
        if stripped.startswith("import "):
            names = [n.strip() for n in stripped[7:].split(",")]
            remaining = [n for n in names if n.split()[0].split(".")[0] != unused_name]
            if not remaining:
                return ""  # 整行删除
            return "import " + ", ".join(remaining) + "\n"

        # from X import Y, Z
        if stripped.startswith("from "):
            m = re.match(r"from\s+\S+\s+import\s+(.+)", stripped)
            if m:
                import_part = m.group(1).strip().strip("()")
                names = [n.strip() for n in import_part.split(",")]
                remaining = [n for n in names if n.strip() != unused_name]
                if not remaining:
                    return ""  # 整行删除
                # 保留原格式（是否有括号）
                if "(" in import_part or "(" in stripped:
                    # 多行 from import 的情况比较复杂，保守处理
                    new_import = ", ".join(remaining)
                    prefix = stripped[:stripped.index("import") + 7]
                    return f"{prefix}{new_import})\n"
                new_import = ", ".join(remaining)
                prefix = stripped[:stripped.index("import") + 7]
                return f"{prefix}{new_import}\n"

        return None

    def _make_diff(self, old_line: str, new_line: str, line_num: int) -> str:
        if not new_line:
            return f"--- {line_num}: {old_line.rstrip()}\n+++ {line_num}: (deleted)"
        return f"--- {line_num}: {old_line.rstrip()}\n+++ {line_num}: {new_line.rstrip()}"

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
