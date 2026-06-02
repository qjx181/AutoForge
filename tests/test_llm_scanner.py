"""test_llm_scanner.py — LLM 扫描器的 JSON 解析 + 重试逻辑测试

测试目标：
  1. _try_parse_json: 各种 JSON 变体的容错解析
  2. _parse_llm_response: 多策略解析（markdown 包裹、截断恢复）
  3. _smart_dedup: 去重逻辑

这些测试 mock 了 LLM 调用，只测解析逻辑。
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# conftest.py 已自动设置 sys.path，无需手动添加


# ═══════════════════════════════════════════════════════════════════════
# _try_parse_json 测试
# ═══════════════════════════════════════════════════════════════════════


class TestTryParseJson:
    """测试 _try_parse_json 的各种 JSON 变体容错。"""

    def test_clean_json_array(self):
        """标准 JSON 数组直接解析。"""
        from src.analysis.llm_scanner import _try_parse_json
        data = [{"type": "bug", "file": "a.py", "line": 1}]
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert len(result) == 1
        assert result[0]["type"] == "bug"

    def test_json_object_with_issues_key(self):
        """如果返回 {"issues": [...]}，提取 issues 数组。"""
        from src.analysis.llm_scanner import _try_parse_json
        data = {"issues": [{"type": "x"}], "meta": "info"}
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert len(result) == 1
        assert result[0]["type"] == "x"

    def test_json_object_with_findings_key(self):
        """支持 findings/results/problems/data 等 key。"""
        from src.analysis.llm_scanner import _try_parse_json
        for key in ("results", "findings", "problems", "data"):
            data = {key: [{"id": 1}]}
            result = _try_parse_json(json.dumps(data))
            assert result is not None, f"key={key} 应该被识别"
            assert result[0]["id"] == 1

    def test_json_object_without_known_key(self):
        """没有已知 key 的单个 dict，包装为 list 返回。"""
        from src.analysis.llm_scanner import _try_parse_json
        data = {"type": "single_issue", "file": "b.py"}
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert len(result) == 1
        assert result[0]["type"] == "single_issue"

    def test_trailing_comma_in_array(self):
        """容错尾逗号 [a, b, ]。"""
        from src.analysis.llm_scanner import _try_parse_json
        raw = '[{"type": "a"}, {"type": "b"},]'
        result = _try_parse_json(raw)
        assert result is not None
        assert len(result) == 2

    def test_trailing_comma_in_object(self):
        """容错尾逗号 {"key": "value",}。"""
        from src.analysis.llm_scanner import _try_parse_json
        raw = '{"issues": [{"type": "x"}],}'
        result = _try_parse_json(raw)
        assert result is not None
        assert len(result) == 1

    def test_json_with_line_comments(self):
        """容错行注释（注释和括号在同一行时可清理）。"""
        from src.analysis.llm_scanner import _try_parse_json
        # 注释在值行尾部，清理后有效
        raw = '{"issues": [{"type": "a"}] // end}'
        result = _try_parse_json(raw)
        assert result is not None
        assert result[0]["type"] == "a"

    def test_json_inside_markdown_code_block(self):
        """从 ```json ... ``` 中提取 JSON。"""
        from src.analysis.llm_scanner import _try_parse_json
        raw = '''Here is the result:
```json
[{"type": "found"}]
```
Hope this helps!'''
        result = _try_parse_json(raw)
        assert result is not None
        assert result[0]["type"] == "found"

    def test_completely_invalid_text(self):
        """完全无法解析的文本返回 None。"""
        from src.analysis.llm_scanner import _try_parse_json
        result = _try_parse_json("this is not json at all, just random text")
        assert result is None

    def test_empty_string(self):
        """空字符串返回 None。"""
        from src.analysis.llm_scanner import _try_parse_json
        result = _try_parse_json("")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# _parse_llm_response 测试
# ═══════════════════════════════════════════════════════════════════════


class TestParseLlmResponse:
    """测试 _parse_llm_response 的多策略解析。"""

    def test_clean_json_response(self):
        """标准 JSON 响应。"""
        from src.analysis.llm_scanner import _parse_llm_response
        response = json.dumps([{"type": "swallowed_exception", "severity": "high"}])
        result = _parse_llm_response(response)
        assert len(result) == 1
        assert result[0]["severity"] == "high"

    def test_markdown_wrapped_response(self):
        """LLM 常见的 markdown 包裹格式。"""
        from src.analysis.llm_scanner import _parse_llm_response
        response = '''以下是发现的问题：
```json
[{"type": "bare_except", "severity": "medium", "file": "main.py", "line": 42}]
```
以上。'''
        result = _parse_llm_response(response)
        assert len(result) == 1
        assert result[0]["file"] == "main.py"

    def test_preamble_before_json(self):
        """LLM 在 JSON 前面加了解释文字。"""
        from src.analysis.llm_scanner import _parse_llm_response
        response = '''I analyzed the code and found the following issues:
[{"type": "missing_import", "severity": "low", "file": "x.py", "line": 1}]
These are the main findings.'''
        result = _parse_llm_response(response)
        assert len(result) == 1

    def test_truncated_array_recovery(self):
        """截断的 JSON 数组，尝试恢复完整对象。"""
        from src.analysis.llm_scanner import _parse_llm_response
        # 模拟截断：最后一个对象不完整
        response = '''[
            {"type": "a", "severity": "high", "file": "a.py", "line": 1},
            {"type": "b", "severity": "low", "file": "b.py", "line": 2},
            {"type": "c", "severity": "medium", "file": "c.py", "line": 3'''
        result = _parse_llm_response(response)
        # 应该至少恢复前两个完整对象
        assert len(result) >= 2

    def test_empty_response(self):
        """空响应返回空列表。"""
        from src.analysis.llm_scanner import _parse_llm_response
        result = _parse_llm_response("")
        assert result == []

    def test_non_json_response(self):
        """完全非 JSON 的响应返回空列表。"""
        from src.analysis.llm_scanner import _parse_llm_response
        result = _parse_llm_response("Sorry, I can't analyze this code.")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
# _smart_dedup 测试
# ═══════════════════════════════════════════════════════════════════════


class TestSmartDedup:
    """测试 _smart_dedup 去重逻辑。"""

    def test_no_duplicates(self):
        """没有重复时原样返回。"""
        from src.analysis.llm_scanner import _smart_dedup
        issues = [
            {"type": "a", "file": "x.py", "line": 1, "dimension": "quality"},
            {"type": "b", "file": "y.py", "line": 2, "dimension": "security"},
        ]
        result = _smart_dedup(issues)
        assert len(result) == 2

    def test_exact_duplicates(self):
        """完全相同的 issue 去重。"""
        from src.analysis.llm_scanner import _smart_dedup
        issue = {"type": "bare_except", "file": "a.py", "line": 10, "dimension": "quality", "severity": "medium"}
        issues = [issue, issue.copy(), issue.copy()]
        result = _smart_dedup(issues)
        assert len(result) == 1

    def test_same_file_adjacent_lines(self):
        """同文件相邻行的相同类型问题合并。"""
        from src.analysis.llm_scanner import _smart_dedup
        issues = [
            {"type": "swallowed_exception", "file": "a.py", "line": 10, "dimension": "quality", "severity": "high"},
            {"type": "swallowed_exception", "file": "a.py", "line": 11, "dimension": "quality", "severity": "high"},
        ]
        result = _smart_dedup(issues)
        # 相邻行同类型应合并为一个
        assert len(result) == 1

    def test_different_files_not_merged(self):
        """不同文件的相同类型不合并。"""
        from src.analysis.llm_scanner import _smart_dedup
        issues = [
            {"type": "bare_except", "file": "a.py", "line": 10, "dimension": "quality", "severity": "medium"},
            {"type": "bare_except", "file": "b.py", "line": 10, "dimension": "quality", "severity": "medium"},
        ]
        result = _smart_dedup(issues)
        assert len(result) == 2

    def test_empty_list(self):
        """空列表返回空列表。"""
        from src.analysis.llm_scanner import _smart_dedup
        result = _smart_dedup([])
        assert result == []
