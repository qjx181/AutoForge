"""registry.py — 污点源/汇/消毒器的声明式注册表。

数据驱动：新增 pattern 只需往列表里加一行，不动追踪逻辑。
每个 pattern 是一个正则表达式，匹配到即认为触发了该 source/sink。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import SinkKind, SourceKind


@dataclass
class SourceSpec:
    """一个污点源的定义。"""
    kind: SourceKind
    patterns: list[str]          # 正则列表，命中任一即触发
    description: str = ""


@dataclass
class SinkSpec:
    """一个危险汇的定义。"""
    kind: SinkKind
    patterns: list[str]
    description: str = ""
    severity: str = "high"


@dataclass
class SanitizerSpec:
    """一个消毒器的定义：经过它的变量不再被视为 tainted。"""
    patterns: list[str]
    description: str = ""


# ──────────────────────────────────────────────────────────────
# Python Sources（污点入口）
# ──────────────────────────────────────────────────────────────

PYTHON_SOURCES: list[SourceSpec] = [
    # HTTP 参数
    SourceSpec(SourceKind.HTTP_PARAM, [
        r'request\.args\.get\(',
        r'request\.args\[',
        r'request\.values\.get\(',
        r'request\.GET\.get\(',
        r'request\.query_params',
        r'Query\(',
    ], "HTTP查询参数"),

    # HTTP Body
    SourceSpec(SourceKind.HTTP_BODY, [
        r'request\.form\.get\(',
        r'request\.form\[',
        r'request\.json',
        r'request\.data',
        r'request\.get_json\(',
        r'request\.POST\.get\(',
        r'request\.body',
        r'Body\(',
    ], "HTTP请求体"),

    # 用户输入
    SourceSpec(SourceKind.USER_INPUT, [
        r'\binput\(',
    ], "用户输入"),

    # 环境变量
    SourceSpec(SourceKind.ENV_VAR, [
        r'os\.environ\.get\(',
        r'os\.environ\[',
        r'os\.environ\.',
        r'os\.getenv\(',
    ], "环境变量"),

    # 文件读取
    SourceSpec(SourceKind.FILE_READ, [
        r'\bopen\(',
        r'\.read\(\)',
        r'\.readline\(\)',
        r'\.readlines\(\)',
    ], "文件读取"),

    # 命令行参数
    SourceSpec(SourceKind.ARGV, [
        r'sys\.argv',
    ], "命令行参数"),

    # 反序列化
    SourceSpec(SourceKind.DESERIALIZED, [
        r'pickle\.loads?\(',
        r'yaml\.load\(',
        r'yaml\.unsafe_load\(',
        r'json\.loads?\(',
    ], "反序列化"),

    # 配置读取
    SourceSpec(SourceKind.CONFIG, [
        r'config\[',
        r'config\.get\(',
        r'\.get\(["\']',
    ], "配置读取"),
]


# ──────────────────────────────────────────────────────────────
# Python Sinks（危险操作）
# ──────────────────────────────────────────────────────────────

PYTHON_SINKS: list[SinkSpec] = [
    # SQL 注入
    SinkSpec(SinkKind.SQL_QUERY, [
        r'execute\s*\(\s*f["\']',
        r'execute\s*\(\s*["\'].*%s.*["\']\s*%',
        r'execute\s*\(\s*[^"\']+\s*\+\s*',
        r'\.execute\s*\(',
        r'\.raw\s*\(',
        r'raw_sql\s*\(',
    ], "SQL查询", "critical"),

    # 命令注入
    SinkSpec(SinkKind.SHELL_EXEC, [
        r'os\.system\s*\(',
        r'os\.popen\s*\(',
        r'subprocess\.\w+\(.*shell\s*=\s*True',
        r'subprocess\.call\s*\(',
        r'subprocess\.Popen\s*\(',
    ], "Shell执行", "critical"),

    # 代码注入
    SinkSpec(SinkKind.CODE_EXEC, [
        r'\beval\s*\(',
        r'\bexec\s*\(',
        r'compile\s*\(',
    ], "代码执行", "critical"),

    # XSS
    SinkSpec(SinkKind.XSS_WRITE, [
        r'Markup\s*\(',
        r'\|safe',
        r'innerHTML',
        r'document\.write\s*\(',
        r'render_template_string\s*\(',
    ], "XSS输出", "high"),

    # 路径穿越
    SinkSpec(SinkKind.PATH_TRAVERSAL, [
        r'send_file\s*\(',
        r'os\.path\.join\s*\(.*request',
        r'pathlib.*\/.*request',
    ], "路径穿越", "high"),

    # 日志注入
    SinkSpec(SinkKind.LOG_INJECT, [
        r'logger\.\w+\(.*%',
        r'logging\.\w+\(.*%',
        r'print\s*\(.*request',
    ], "日志注入", "medium"),

    # SSRF
    SinkSpec(SinkKind.SSRF, [
        r'requests\.get\s*\(',
        r'requests\.post\s*\(',
        r'urllib\.request\.urlopen\s*\(',
        r'httpx\.\w+\s*\(',
        r'aiohttp\.\w+\.get\s*\(',
    ], "SSRF请求", "high"),

    # Pickle
    SinkSpec(SinkKind.PICKLE, [
        r'pickle\.loads?\s*\(',
    ], "反序列化", "critical"),
]


# ──────────────────────────────────────────────────────────────
# Python Sanitizers（消毒器：经过它变量不再 tainted）
# ──────────────────────────────────────────────────────────────

PYTHON_SANITIZERS: list[SanitizerSpec] = [
    SanitizerSpec([
        r'int\s*\(',           # 类型转换为 int
        r'float\s*\(',         # 类型转换为 float
    ], "类型转换"),

    SanitizerSpec([
        r'escape\s*\(',        # HTML 转义
        r'html\.escape\(',
        r'markupsafe\.escape\(',
        r'bleach\.clean\(',
    ], "HTML转义"),

    SanitizerSpec([
        r'parameterized',      # 参数化查询
        r'placeholder',
        r'\?\s*,',             # SQL 占位符 ?,...
    ], "SQL参数化"),

    SanitizerSpec([
        r'shlex\.quote\(',     # Shell 转义
        r'pipes\.quote\(',
    ], "Shell转义"),

    SanitizerSpec([
        r'os\.path\.realpath\(',  # 路径规范化
        r'os\.path\.normpath\(',
        r'Path\(.*\)\.resolve\(',
    ], "路径规范化"),
]


class Registry:
    """管理 source/sink/sanitizer 注册表，支持按行匹配。"""

    def __init__(
        self,
        sources: list[SourceSpec] | None = None,
        sinks: list[SinkSpec] | None = None,
        sanitizers: list[SanitizerSpec] | None = None,
    ):
        self._sources = sources or PYTHON_SOURCES
        self._sinks = sinks or PYTHON_SINKS
        self._sanitizers = sanitizers or PYTHON_SANITIZERS

        # 预编译正则
        self._source_patterns: list[tuple[re.Pattern, SourceSpec]] = []
        for spec in self._sources:
            for p in spec.patterns:
                self._source_patterns.append((re.compile(p), spec))

        self._sink_patterns: list[tuple[re.Pattern, SinkSpec]] = []
        for spec in self._sinks:
            for p in spec.patterns:
                self._sink_patterns.append((re.compile(p), spec))

        self._sanitizer_patterns: list[tuple[re.Pattern, SanitizerSpec]] = []
        for spec in self._sanitizers:
            for p in spec.patterns:
                self._sanitizer_patterns.append((re.compile(p), spec))

    def match_source(self, line: str) -> list[SourceSpec]:
        """检查一行代码是否匹配任何污点源。"""
        results = []
        for pat, spec in self._source_patterns:
            if pat.search(line):
                results.append(spec)
        return results

    def match_sink(self, line: str) -> list[SinkSpec]:
        """检查一行代码是否匹配任何危险汇。"""
        results = []
        for pat, spec in self._sink_patterns:
            if pat.search(line):
                results.append(spec)
        return results

    def match_sanitizer(self, line: str) -> bool:
        """检查一行代码是否是消毒器。"""
        for pat, _ in self._sanitizer_patterns:
            if pat.search(line):
                return True
        return False
