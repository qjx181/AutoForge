"""triage.py — LLM 精判层。

数据流筛选出候选点后，本模块负责：
  1. 按文件分组候选，构建精简 prompt
  2. 发给 LLM 做最终判断（真问题 / 误报 / 需要更多上下文）
  3. 合并 LLM 结果与反馈抑制记录

核心原则：发给 LLM 的是"嫌疑犯档案"，不是整个项目代码。
每个候选的 prompt 成本约 200-400 tokens，远低于全量扫描。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .schemas import TaintCandidate
from .feedback import FeedbackStore

logger = logging.getLogger(__name__)

TRIAGE_PROMPT_TEMPLATE = """你是一个资深安全工程师，负责判断代码中的数据流是否构成真实漏洞。

以下是数据流追踪发现的候选问题点。每个候选展示了：
- 污点源（Source）：用户数据从哪里进入
- 传播路径（Hops）：数据经过了哪些变量赋值
- 危险汇（Sink）：数据最终流向了什么操作

请逐个判断：
1. 这是否是真实的漏洞？（考虑：数据是否真的来自不可信源？传播过程中是否有隐式消毒？汇操作是否真的危险？）
2. 给出判断：true_positive / false_positive / uncertain
3. 如果是误报，说明原因

## 候选列表

{candidates}

## 输出格式

返回 JSON 数组，每个元素对应一个候选：
```json
[
  {{
    "index": 0,
    "judgment": "true_positive",
    "reason": "...",
    "severity": "critical/high/medium/low"
  }},
  ...
]
```"""


def build_candidate_prompt(candidates: list[TaintCandidate]) -> str:
    """将候选列表构建为 LLM 可理解的 prompt。"""
    parts = []
    for i, c in enumerate(candidates):
        hop_str = " → ".join(h.variable for h in c.hops) if c.hops else "(直接传递)"
        parts.append(
            f"### 候选 {i}\n"
            f"- 文件: {c.filepath}\n"
            f"- 函数: {c.function_name or '模块级'}\n"
            f"- Source ({c.source_kind.value}): `{c.source_pattern[:100]}` (行 {c.source_line})\n"
            f"- 传播: {hop_str} ({c.hop_count} 跳)\n"
            f"- Sink ({c.sink_kind.value}): `{c.sink_pattern[:100]}` (行 {c.sink_line})\n"
            f"- 数据流置信度: {c.confidence:.1%}\n"
            f"```\n{c.snippet[:400]}\n```\n"
        )
    return TRIAGE_PROMPT_TEMPLATE.format(candidates="\n".join(parts))


def triage_candidates(
    candidates: list[TaintCandidate],
    feedback: FeedbackStore | None = None,
    call_llm: bool = True,
) -> list[dict[str, Any]]:
    """对候选列表做 triage：先过滤已抑制的，再发给 LLM 精判。

    Args:
        candidates: 数据流层输出的候选列表
        feedback: 反馈存储（用于抑制已知误报）
        call_llm: 是否调用 LLM（False 时只做反馈过滤）

    Returns:
        list[dict]，每个元素包含：
          - candidate: 原始候选（TaintCandidate.to_dict()）
          - judgment: "true_positive" / "false_positive" / "suppressed" / "uncertain"
          - reason: 判断理由
          - severity: 严重级别
    """
    if feedback is None:
        feedback = FeedbackStore()

    results = []

    # ── Phase 1: 反馈抑制过滤 ──
    pending = []
    for c in candidates:
        if feedback.is_suppressed(
            c.filepath,
            c.source_kind.value,
            c.sink_kind.value,
            c.sink_pattern,
        ):
            results.append({
                "candidate": c.to_dict(),
                "judgment": "suppressed",
                "reason": "已被用户标记为误报（反馈循环抑制）",
                "severity": "none",
            })
        else:
            pending.append(c)

    if not pending:
        return results

    # ── Phase 2: LLM 精判 ──
    if call_llm:
        llm_results = _call_llm_triage(pending)
        results.extend(llm_results)
    else:
        # 不调 LLM 时，用数据流置信度做简单判断
        for c in pending:
            judgment = "uncertain"
            if c.confidence >= 0.7:
                judgment = "true_positive"
            elif c.confidence < 0.4:
                judgment = "false_positive"
            results.append({
                "candidate": c.to_dict(),
                "judgment": judgment,
                "reason": f"数据流置信度 {c.confidence:.1%}（未调用 LLM）",
                "severity": _estimate_severity(c),
            })

    return results


def _call_llm_triage(candidates: list[TaintCandidate]) -> list[dict]:
    """调用 LLM 对候选做精判。"""
    # 延迟导入避免循环依赖
    import importlib
    llm_scanner_mod = importlib.import_module("src.analysis.llm_scanner")

    prompt = build_candidate_prompt(candidates)
    logger.info("Triage: 发送 %d 个候选给 LLM（约 %d 字符）",
                len(candidates), len(prompt))

    try:
        response_text, cost = llm_scanner_mod._call_llm(prompt)
        logger.info("Triage: LLM 返回 %d 字符，成本 $%.4f", len(response_text), cost)
    except Exception as e:
        logger.error("Triage LLM 调用失败: %s，回退到置信度判断", e)
        # 回退：用数据流置信度
        return [
            {
                "candidate": c.to_dict(),
                "judgment": "uncertain",
                "reason": f"LLM 调用失败，回退到置信度 {c.confidence:.1%}",
                "severity": _estimate_severity(c),
            }
            for c in candidates
        ]

    # 解析 LLM 返回的 JSON
    try:
        parsed = _parse_llm_response(response_text, candidates)
        return parsed
    except Exception as e:
        logger.error("Triage: LLM 响应解析失败: %s", e)
        return [
            {
                "candidate": c.to_dict(),
                "judgment": "uncertain",
                "reason": f"LLM 响应解析失败",
                "severity": _estimate_severity(c),
            }
            for c in candidates
        ]


def _parse_llm_response(response: str, candidates: list[TaintCandidate]) -> list[dict]:
    """解析 LLM triage 的 JSON 响应。"""
    # 尝试提取 JSON 数组
    response = response.strip()

    # 如果被 markdown 包裹，提取出来
    if "```json" in response:
        start = response.index("```json") + 7
        end = response.index("```", start)
        response = response[start:end].strip()
    elif "```" in response:
        start = response.index("```") + 3
        end = response.index("```", start)
        response = response[start:end].strip()

    # 尝试解析
    if response.startswith("["):
        items = json.loads(response)
    elif response.startswith("{"):
        # 有时 LLM 返回 {"results": [...]}
        obj = json.loads(response)
        items = obj.get("results", obj.get("candidates", [obj]))
    else:
        raise ValueError(f"无法解析 LLM 响应: {response[:200]}")

    results = []
    for i, item in enumerate(items):
        idx = item.get("index", i)
        if idx >= len(candidates):
            continue
        c = candidates[idx]
        results.append({
            "candidate": c.to_dict(),
            "judgment": item.get("judgment", "uncertain"),
            "reason": item.get("reason", ""),
            "severity": item.get("severity", _estimate_severity(c)),
        })

    # 补充 LLM 没覆盖到的候选
    covered = {item.get("index", i) for i, item in enumerate(items)}
    for i, c in enumerate(candidates):
        if i not in covered:
            results.append({
                "candidate": c.to_dict(),
                "judgment": "uncertain",
                "reason": "LLM 未对此候选做出判断",
                "severity": _estimate_severity(c),
            })

    return results


def _estimate_severity(c: TaintCandidate) -> str:
    """根据 source/sink 类型估算严重级别。"""
    critical_sinks = {"code_exec", "shell_exec", "sql_query", "pickle"}
    high_sinks = {"xss_write", "path_traversal", "ssrf"}

    if c.sink_kind.value in critical_sinks:
        return "critical"
    elif c.sink_kind.value in high_sinks:
        return "high"
    return "medium"


def filter_by_feedback(
    candidates: list[TaintCandidate],
    feedback: FeedbackStore,
) -> tuple[list[TaintCandidate], int]:
    """预过滤已抑制的候选。

    Returns:
        (过滤后的候选列表, 被抑制的数量)
    """
    filtered = []
    suppressed = 0
    for c in candidates:
        if feedback.is_suppressed(
            c.filepath,
            c.source_kind.value,
            c.sink_kind.value,
            c.sink_pattern,
        ):
            suppressed += 1
        else:
            filtered.append(c)
    return filtered, suppressed
