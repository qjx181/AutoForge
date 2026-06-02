"""metrics_reporter.py — Swarm 自我进化循环的指标收集模块

提供 Swarm 自我进化循环的完整指标收集能力，包含五个核心组件：
  - RoundTimer:   记录每轮开始/结束时间、持续时长
  - TaskTracker:  记录任务完成数、失败数、通过率
  - IssueTracker: 按严重级别统计问题出现频率
  - MetricsStore: 将指标数据持久化为 JSON 文件
  - MetricsReporter: 生成可读的文本/JSON 摘要报告

用法示例
--------
    from swarm_metrics import SwarmMetrics

    metrics = SwarmMetrics()
    metrics.start_round(round_num=15)
    metrics.record_task(agent="agent-1", status="completed", duration_sec=120)
    metrics.record_issue(severity="error", category="logic_error", module="swarm_metrics")
    report = metrics.generate_report()
    metrics.save("tmp_agent/metrics/round-15.json")
"""

import datetime
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Union

from src.infra.swarm_utils import read_file_safe, write_file_safe, log_step
from src.infra.swarm_logger import SwarmLogger

_log = SwarmLogger(name="swarm_metrics", level="INFO", json_mode=False)

SEVERITY_ORDER: List[str] = ["critical", "error", "warning", "info", "debug"]
SEVERITY_WEIGHT: Dict[str, int] = {
    "critical": 50,
    "error": 40,
    "warning": 30,
    "info": 20,
    "debug": 10,
}



class MetricsReporter:
    """MetricsReporter — 指标报告生成器。

    将 SwarmMetrics 的数据转换为可读的文本摘要或结构化 JSON 报告。
    """


    @staticmethod
    def _report_header(data: Dict[str, Any], lines: List[str]) -> None:
        """报告头部。"""
        separator = "=" * 60
        lines.append(separator)
        lines.append("  Swarm 自我进化循环 — 指标报告")
        lines.append(f"  生成时间: {data.get('summary', {}).get('generated_at', '未知')}")
        lines.append(separator)
        lines.append("")

    @staticmethod
    def _report_timer(data: Dict[str, Any], lines: List[str]) -> None:
        """轮次计时器部分。"""
        timer = data.get("timer", {})
        timer_summary = timer.get("summary", {})
        lines.append("─── 轮次计时 ───")
        current = timer.get("current")
        if current:
            lines.append(f"  当前轮次: Round {current.get('round_num')}（进行中）")
        lines.append(f"  已完成轮次: {timer_summary.get('total_rounds', 0)}")
        lines.append(f"  总耗时: {timer_summary.get('total_duration_sec', 0)} 秒")
        avg = timer_summary.get("average_duration_sec")
        if avg is not None:
            lines.append(f"  平均耗时: {avg} 秒")
        lines.append("")

    @staticmethod
    def _report_round_details(data: Dict[str, Any], lines: List[str]) -> None:
        """历史轮次详情部分。"""
        timer = data.get("timer", {})
        rounds = timer.get("rounds", [])
        if not rounds:
            return
        lines.append("─── 轮次详情 ───")
        for r in rounds[-5:]:  # 最近 5 轮
            dur = r.get("duration_sec")
            dur = f"{dur:>8.3f}" if dur is not None else "     None"
            start = r.get("start", "?")[:19]
            lines.append(f"  Round {r['round_num']:>3d} | {start} | {dur}s")
        if len(rounds) > 5:
            lines.append(f"  ... 还有 {len(rounds) - 5} 轮已省略")
        lines.append("")

    @staticmethod
    def _report_tasks(data: Dict[str, Any], lines: List[str]) -> None:
        """任务追踪部分。"""
        tasks = data.get("tasks", {})
        task_summary = tasks.get("summary", {})
        lines.append("─── 任务追踪 ───")
        lines.append(f"  总数: {task_summary.get('total', 0)}")
        lines.append(f"  完成: {task_summary.get('completed', 0)}")
        lines.append(f"  失败: {task_summary.get('failed', 0)}")
        lines.append(f"  跳过: {task_summary.get('skipped', 0)}")
        pass_rate = task_summary.get("pass_rate")
        if pass_rate is not None:
            lines.append(f"  通过率: {pass_rate * 100:.2f}%")
        lines.append("")

    @staticmethod
    def _report_agents(data: Dict[str, Any], lines: List[str]) -> None:
        """Agent 汇总部分。"""
        tasks = data.get("tasks", {})
        task_summary = tasks.get("summary", {})
        agents = task_summary.get("agents", {})
        if not agents:
            return
        lines.append("─── Agent 统计 ───")
        lines.append(f"  {'Agent':<15s} {'完成':>5s} {'失败':>5s} {'跳过':>5s} {'总数':>5s}")
        lines.append(f"  {'-'*15} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")
        for agent_name, stats in sorted(agents.items()):
            lines.append(
                f"  {agent_name:<15s} {stats['completed']:>5d} "
                f"{stats['failed']:>5d} {stats['skipped']:>5d} {stats['total']:>5d}"
            )
        lines.append("")

    @staticmethod
    def _report_issues(data: Dict[str, Any], lines: List[str]) -> None:
        """问题统计部分。"""
        issues = data.get("issues", {})
        issue_freq = issues.get("frequency", {})
        issue_summary = issues.get("summary", {})
        lines.append("─── 问题统计 ───")
        lines.append(f"  问题总数: {issue_summary.get('total', 0)}")
        lines.append(f"  加权得分: {issue_summary.get('weighted_score', 0)}")
        lines.append("")

        by_severity = issue_freq.get("by_severity", {})
        if any(v > 0 for v in by_severity.values()):
            lines.append("  按严重级别:")
            for sev in SEVERITY_ORDER:
                count = by_severity.get(sev, 0)
                if count > 0:
                    lines.append(f"    {sev:<10s}: {count}")
            lines.append("")

        by_category = issue_freq.get("by_category", {})
        if by_category:
            lines.append("  按类别（Top 5）:")
            sorted_cats = sorted(by_category.items(), key=lambda x: -x[1])[:5]
            for cat, count in sorted_cats:
                lines.append(f"    {cat:<20s}: {count}")
            lines.append("")

        top_issues = issue_summary.get("top_issues", [])
        if top_issues:
            lines.append("  最严重的问题:")
            for i, issue in enumerate(top_issues, 1):
                sev = issue.get("severity", "?")
                cat = issue.get("category", "?")
                mod = issue.get("module", "?")
                msg = issue.get("message", "")
                msg_suffix = f" — {msg}" if msg else ""
                lines.append(f"    {i}. [{sev.upper():>8s}] {cat} @ {mod}{msg_suffix}")
            lines.append("")

    @staticmethod
    def generate_text_report(data: Dict[str, Any]) -> str:
        """generate_text_report — 生成人类可读的文本摘要报告。

        Args:
            data: SwarmMetrics.to_dict() 返回的完整数据字典。

        Returns:
            格式化的文本报告字符串。
        """
        lines: List[str] = []
        separator = "=" * 60

        MetricsReporter._report_header(data, lines)
        MetricsReporter._report_timer(data, lines)
        MetricsReporter._report_round_details(data, lines)
        MetricsReporter._report_tasks(data, lines)
        MetricsReporter._report_agents(data, lines)
        MetricsReporter._report_issues(data, lines)

        lines.append(separator)
        return "\n".join(lines)

    @staticmethod
    def generate_json_report(data: Dict[str, Any]) -> str:
        """generate_json_report — 生成 JSON 格式的摘要报告。

        Args:
            data: SwarmMetrics.to_dict() 返回的完整数据字典。

        Returns:
            JSON 格式的摘要报告字符串。
        """
        report = {
            "generated_at": data.get("summary", {}).get("generated_at"),
            "timer": {
                "total_rounds": data.get("timer", {}).get("summary", {}).get("total_rounds"),
                "total_duration_sec": data.get("timer", {}).get("summary", {}).get("total_duration_sec"),
                "average_duration_sec": data.get("timer", {}).get("summary", {}).get("average_duration_sec"),
            },
            "tasks": {
                "total": data.get("tasks", {}).get("summary", {}).get("total"),
                "completed": data.get("tasks", {}).get("summary", {}).get("completed"),
                "failed": data.get("tasks", {}).get("summary", {}).get("failed"),
                "pass_rate": data.get("tasks", {}).get("summary", {}).get("pass_rate"),
                "agents": data.get("tasks", {}).get("summary", {}).get("agents"),
            },
            "issues": {
                "total": data.get("issues", {}).get("summary", {}).get("total"),
                "weighted_score": data.get("issues", {}).get("summary", {}).get("weighted_score"),
                "frequency_by_severity": data.get("issues", {}).get("frequency", {}).get("by_severity"),
                "frequency_by_category": data.get("issues", {}).get("frequency", {}).get("by_category"),
            },
        }
        try:
            return json.dumps(report, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            _log.error("JSON 报告生成失败", error=str(exc))
            return json.dumps({"error": str(exc)})


