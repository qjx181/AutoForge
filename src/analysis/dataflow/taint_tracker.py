"""taint_tracker.py — 轻量级污点追踪器。

核心思路（不依赖 AST，纯正则 + 变量赋值链追踪）：
  1. 逐行扫描，遇到 source 标记变量为 tainted
  2. 遇到赋值语句，追踪 RHS 中的 tainted 变量是否传播到 LHS
  3. 遇到 sink，检查参数中是否有 tainted 变量
  4. 遇到 sanitizer，清除对应变量的 taint

这是"廉价筛选层"——宁可多报不漏报，精确度靠后面的 LLM triage。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .registry import Registry, PYTHON_SOURCES, PYTHON_SINKS, PYTHON_SANITIZERS
from .schemas import (
    SinkKind,
    SourceKind,
    TaintCandidate,
    TaintHop,
)


# 每个文件最多输出多少候选，防止 LLM 成本爆炸
MAX_CANDIDATES_PER_FILE = 20


@dataclass
class _TaintedVar:
    """一个被标记为 tainted 的变量。"""
    name: str
    source_kind: SourceKind
    source_pattern: str
    source_line: int
    hops: list[TaintHop] = field(default_factory=list)


class TaintTracker:
    """逐行扫描函数体，追踪变量从 source 到 sink 的传播路径。

    工作流程：
      1. 初始化时加载 source/sink/sanitizer 注册表
      2. analyze(code, filepath) 对代码逐行扫描
      3. 返回 TaintCandidate 列表（已过滤掉被消毒的路径）

    局限性（明确留给 LLM triage 处理）：
      - 只做函数内追踪，不跨函数
      - 不追踪容器写入 dict[key] = tainted
      - 不追踪全局变量
      - 不追踪隐式控制流依赖
    """

    def __init__(self, registry: Registry | None = None):
        self._reg = registry or Registry()

    def analyze(self, code: str, filepath: str = "") -> list[TaintCandidate]:
        """分析一段代码，返回污点候选列表。

        Args:
            code: 源代码文本（整个文件或单个函数体）
            filepath: 文件路径（用于候选元数据）

        Returns:
            TaintCandidate 列表，按 source_line 排序
        """
        lines = code.splitlines()
        tainted: dict[str, _TaintedVar] = {}  # var_name → TaintedVar
        candidates: list[TaintCandidate] = []

        for lineno, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith('"""'):
                continue

            # ── 1. 检查是否是新的 taint source ──
            sources = self._reg.match_source(line)
            for spec in sources:
                # 尝试提取赋值左侧的变量名
                var = self._extract_assign_lhs(line)
                if var:
                    tainted[var] = _TaintedVar(
                        name=var,
                        source_kind=spec.kind,
                        source_pattern=line[:200],
                        source_line=lineno,
                    )
                # 即使没提取到变量，也可能直接传入sink（同一行）
                # 这种情况在 sink 检查时处理

            # ── 2. 检查是否是消毒器 ──
            if self._reg.match_sanitizer(line):
                var = self._extract_assign_lhs(line)
                if var and var in tainted:
                    del tainted[var]

            # ── 3. 追踪赋值传播 ──
            self._track_assignment(line, lineno, tainted)

            # ── 4. 检查是否到达 sink ──
            sinks = self._reg.match_sink(line)
            for sink_spec in sinks:
                # 检查 sink 行中是否引用了任何 tainted 变量
                matched_vars = self._find_tainted_in_line(line, tainted)
                for var_name in matched_vars:
                    tv = tainted[var_name]
                    # 构建代码片段（source 到 sink 之间的上下文）
                    snippet = self._build_snippet(lines, tv.source_line, lineno)

                    candidate = TaintCandidate(
                        source_kind=tv.source_kind,
                        source_pattern=tv.source_pattern,
                        source_line=tv.source_line,
                        sink_kind=sink_spec.kind,
                        sink_pattern=line[:200],
                        sink_line=lineno,
                        hops=list(tv.hops),
                        filepath=filepath,
                        snippet=snippet,
                        confidence=self._estimate_confidence(tv, sink_spec),
                    )
                    candidates.append(candidate)

                    if len(candidates) >= MAX_CANDIDATES_PER_FILE:
                        return candidates

        return candidates

    def _extract_assign_lhs(self, line: str) -> Optional[str]:
        """提取赋值语句左侧的变量名。

        支持：
          x = ...
          x: str = ...
          x, y = ...
          self.x = ...
        """
        # 普通赋值
        m = re.match(r'^(\w+)\s*[:=]', line)
        if m:
            return m.group(1)

        # self.x = ...
        m = re.match(r'^self\.(\w+)\s*=', line)
        if m:
            return f"self.{m.group(1)}"

        # 解构赋值（取第一个）
        m = re.match(r'^(\w+)\s*,', line)
        if m:
            return m.group(1)

        return None

    def _track_assignment(
        self,
        line: str,
        lineno: int,
        tainted: dict[str, _TaintedVar],
    ) -> None:
        """追踪赋值语句中的 taint 传播。

        如果 RHS 中包含 tainted 变量，则 LHS 也被标记为 tainted。
        """
        # 提取赋值 RHS
        m = re.match(r'^(?:self\.)?(\w+)\s*=\s*(.+)$', line)
        if not m:
            return

        lhs = m.group(1)
        rhs = m.group(2)

        # 检查 RHS 中是否引用了任何 tainted 变量
        rhs_vars = self._find_tainted_in_line(rhs, tainted)
        if not rhs_vars:
            return

        # 从第一个 tainted 源传播
        source_var = rhs_vars[0]
        tv = tainted[source_var]

        # 检查 RHS 是否经过消毒
        if self._reg.match_sanitizer(rhs):
            return

        # 构建传播链
        new_hops = list(tv.hops)
        new_hops.append(TaintHop(
            variable=lhs,
            lineno=lineno,
            code_line=line[:200],
            hop_type=self._classify_hop(rhs),
        ))

        tainted[lhs] = _TaintedVar(
            name=lhs,
            source_kind=tv.source_kind,
            source_pattern=tv.source_pattern,
            source_line=tv.source_line,
            hops=new_hops,
        )

    def _classify_hop(self, rhs: str) -> str:
        """判断传播类型。"""
        if re.search(r'f["\']', rhs):
            return "fstring"
        if re.search(r'\+', rhs):
            return "concat"
        if re.search(r'%\s*[(\[]', rhs) or '%s' in rhs or '%d' in rhs:
            return "format"
        if re.search(r'\.format\(', rhs):
            return "format"
        return "assign"

    def _find_tainted_in_line(
        self,
        line: str,
        tainted: dict[str, _TaintedVar],
    ) -> list[str]:
        """找出一行代码中引用了哪些 tainted 变量。"""
        matched = []
        for var_name in tainted:
            # 用 word boundary 匹配，避免部分匹配
            # self.x 需要特殊处理
            if var_name.startswith("self."):
                attr = var_name.split(".", 1)[1]
                if re.search(r'\b' + re.escape(attr) + r'\b', line):
                    matched.append(var_name)
            else:
                if re.search(r'\b' + re.escape(var_name) + r'\b', line):
                    matched.append(var_name)
        return matched

    def _build_snippet(self, lines: list[str], start: int, end: int, context: int = 3) -> str:
        """构建 source 到 sink 之间的代码片段，附带上下文。"""
        s = max(0, start - 1 - context)
        e = min(len(lines), end + context)
        result = []
        for i in range(s, e):
            prefix = ">> " if start - 1 <= i <= end - 1 else "   "
            result.append(f"{prefix}{i+1:4d} | {lines[i]}")
        return "\n".join(result)

    def _estimate_confidence(self, tv: _TaintedVar, sink_spec) -> float:
        """估算这个候选是真问题的置信度（0-1）。

        规则：
          - source 直接到 sink（hop=0）→ 0.8
          - 经过 1 跳 → 0.7
          - 经过 2+ 跳 → 0.5
          - sink 是 critical 级别 → +0.1
          - source 是 user_input/HTTP → +0.1
        """
        base = 0.8 if len(tv.hops) == 0 else (0.7 if len(tv.hops) == 1 else 0.5)

        if sink_spec.severity == "critical":
            base += 0.1
        if tv.source_kind in (SourceKind.USER_INPUT, SourceKind.HTTP_PARAM, SourceKind.HTTP_BODY):
            base += 0.1

        return min(1.0, base)
