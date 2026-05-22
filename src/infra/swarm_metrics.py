#!/usr/bin/env python3
"""swarm_metrics.py — Swarm 自我进化循环的指标收集模块

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
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Union

from src.infra.swarm_utils import read_file_safe, write_file_safe, log_step
from src.infra.swarm_logger import SwarmLogger

# ── 默认日志记录器 ──────────────────────────────────────────────────
_log = SwarmLogger(name="swarm_metrics", level="INFO", json_mode=False)

# ── 严重级别排序权重 ────────────────────────────────────────────────
SEVERITY_ORDER: List[str] = ["critical", "error", "warning", "info", "debug"]
SEVERITY_WEIGHT: Dict[str, int] = {
    "critical": 50,
    "error": 40,
    "warning": 30,
    "info": 20,
    "debug": 10,
}


# ═══════════════════════════════════════════════════════════════════
# RoundTimer
# ═══════════════════════════════════════════════════════════════════
class RoundTimer:
    """RoundTimer — 轮次计时器。

    记录单轮或多轮的开始时间、结束时间和持续时长。
    支持主动调用 start/end 手动计时，或通过 context manager 自动计时。

    Attributes:
        rounds:   已完成的轮次记录列表，每项为 {round_num, start, end, duration_sec}。
        _current: 当前正在计时的轮次状态（dict），或 None。
    """

    def __init__(self) -> None:
        """RoundTimer — 初始化计时器。"""
        self.rounds: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None

    def start_round(self, round_num: int) -> None:
        """start_round — 开始一轮计时。

        Args:
            round_num: 轮次编号（正整数）。

        Raises:
            ValueError: 当已有未结束的轮次时抛出。
        """
        if self._current is not None:
            raise ValueError(
                f"轮次 Round {self._current['round_num']} 尚未结束，"
                "请先调用 end_round() 后再开始新轮次。"
            )
        if not isinstance(round_num, int) or round_num < 0:
            raise ValueError(f"round_num 必须为非负整数，收到 {round_num!r}。")

        self._current = {
            "round_num": round_num,
            "start": datetime.datetime.now().isoformat(),
            "end": None,
            "duration_sec": None,
        }
        _log.info("开始计时", round=round_num)

    def end_round(self) -> Dict[str, Any]:
        """end_round — 结束当前轮次计时。

        Returns:
            当前轮次的计时记录（包含 round_num, start, end, duration_sec）。

        Raises:
            RuntimeError: 当没有正在计时的轮次时抛出。
        """
        if self._current is None:
            raise RuntimeError("没有正在计时的轮次，请先调用 start_round()。")

        now = datetime.datetime.now()
        start = datetime.datetime.fromisoformat(self._current["start"])
        duration_sec = round((now - start).total_seconds(), 3)

        self._current["end"] = now.isoformat()
        self._current["duration_sec"] = duration_sec

        record = dict(self._current)
        self.rounds.append(record)
        self._current = None
        _log.info("结束计时", round=record["round_num"], duration_sec=duration_sec)
        return record

    def current_round(self) -> Optional[int]:
        """current_round — 获取当前正在计时的轮次编号。

        Returns:
            轮次编号，若无正在计时的轮次则返回 None。
        """
        if self._current is not None:
            return self._current["round_num"]
        return None

    def total_duration_sec(self) -> float:
        """total_duration_sec — 计算所有已完成轮次的总耗时。

        Returns:
            所有已记录轮次的持续时间之和（秒）。
        """
        return sum(
            r["duration_sec"] for r in self.rounds if r["duration_sec"] is not None
        )

    def average_duration_sec(self) -> Optional[float]:
        """average_duration_sec — 计算所有已完成轮次的平均耗时。

        Returns:
            平均持续时间（秒），若无数据则返回 None。
        """
        durations = [r["duration_sec"] for r in self.rounds if r["duration_sec"] is not None]
        if not durations:
            return None
        return round(statistics.mean(durations), 3)

    def last_round(self) -> Optional[Dict[str, Any]]:
        """last_round — 获取最后一轮的计时记录。

        Returns:
            最后一轮的记录字典，若无数据则返回 None。
        """
        return self.rounds[-1] if self.rounds else None

    def to_dict(self) -> Dict[str, Any]:
        """to_dict — 将计时器数据序列化为字典。

        Returns:
            包含 current 和 rounds 的字典。
        """
        return {
            "current": dict(self._current) if self._current else None,
            "rounds": list(self.rounds),
            "summary": {
                "total_rounds": len(self.rounds),
                "total_duration_sec": self.total_duration_sec(),
                "average_duration_sec": self.average_duration_sec(),
            },
        }

    def __enter__(self) -> "RoundTimer":
        """__enter__ — 支持 with 语句（需先调用 start_round）。"""
        return self

    def __exit__(self, *args: Any) -> None:
        """__exit__ — 退出 with 语句时自动结束当前轮次。"""
        if self._current is not None:
            self.end_round()


# ═══════════════════════════════════════════════════════════════════
# TaskTracker
# ═══════════════════════════════════════════════════════════════════
class TaskTracker:
    """TaskTracker — 任务完成率追踪器。

    记录每个 Agent 的任务完成数、失败数及通过率，
    支持按 Agent 聚合查询和全局统计。

    Attributes:
        tasks: 所有已记录的任务列表。
    """

    def __init__(self) -> None:
        """TaskTracker — 初始化任务追踪器。"""
        self.tasks: List[Dict[str, Any]] = []

    def record_task(
        self,
        agent: str,
        status: str,
        duration_sec: Optional[float] = None,
        task_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """record_task — 记录一个任务的执行结果。

        Args:
            agent:       Agent 标识符（如 "agent-1"）。
            status:      任务状态。"completed" / "failed" / "skipped" / "in_progress"。
            duration_sec: 任务耗时（秒，可选）。
            task_name:    任务名称（可选）。
            details:      附加详情字典（可选）。

        Returns:
            新创建的任务记录字典。

        Raises:
            ValueError: 当 status 不在合法范围内时抛出。
        """
        valid_statuses = {"completed", "failed", "skipped", "in_progress"}
        if status not in valid_statuses:
            raise ValueError(
                f"status 必须是 {valid_statuses} 之一，收到 {status!r}。"
            )

        record: Dict[str, Any] = {
            "agent": agent,
            "status": status,
            "duration_sec": duration_sec,
            "task_name": task_name,
            "details": details or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }
        self.tasks.append(record)
        _log.info("记录任务", agent=agent, status=status, task=task_name)
        return record

    def count_by_status(self, status: str) -> int:
        """count_by_status — 统计指定状态的任务数量。

        Args:
            status: 任务状态（"completed" / "failed" / "skipped" / "in_progress"）。

        Returns:
            匹配该状态的任务数量。
        """
        return sum(1 for t in self.tasks if t["status"] == status)

    def count_by_agent(self, agent: str, status: Optional[str] = None) -> int:
        """count_by_agent — 统计指定 Agent 的任务数量。

        Args:
            agent:  Agent 标识符。
            status: 可选的状态过滤条件。

        Returns:
            匹配的任务数量。
        """
        if status is not None:
            return sum(1 for t in self.tasks if t["agent"] == agent and t["status"] == status)
        return sum(1 for t in self.tasks if t["agent"] == agent)

    def total(self) -> int:
        """total — 获取任务总数。

        Returns:
            所有记录的任务数量。
        """
        return len(self.tasks)

    def completed(self) -> int:
        """completed — 获取已完成任务数。"""
        return self.count_by_status("completed")

    def failed(self) -> int:
        """failed — 获取失败任务数。"""
        return self.count_by_status("failed")

    def skipped(self) -> int:
        """skipped — 获取已跳过任务数。"""
        return self.count_by_status("skipped")

    def pass_rate(self) -> Optional[float]:
        """pass_rate — 计算任务通过率。

        通过率 = 已完成数 / (已完成数 + 失败数)。
        跳过的任务不计入分母。

        Returns:
            通过率（0.0 ~ 1.0），若无有效任务则返回 None。
        """
        completed = self.completed()
        failed = self.failed()
        denominator = completed + failed
        if denominator == 0:
            return None
        return round(completed / denominator, 4)

    def agent_summary(self) -> Dict[str, Dict[str, int]]:
        """agent_summary — 按 Agent 聚合统计。

        Returns:
            {agent: {completed, failed, skipped, total}, ...} 格式的字典。
        """
        agents: Dict[str, Dict[str, int]] = {}
        for t in self.tasks:
            agent = t["agent"]
            if agent not in agents:
                agents[agent] = {"completed": 0, "failed": 0, "skipped": 0, "in_progress": 0, "total": 0}
            agents[agent][t["status"]] += 1
            agents[agent]["total"] += 1
        return agents

    def to_dict(self) -> Dict[str, Any]:
        """to_dict — 将任务追踪器数据序列化为字典。

        Returns:
            包含 tasks 和 summary 的字典。
        """
        return {
            "tasks": list(self.tasks),
            "summary": {
                "total": self.total(),
                "completed": self.completed(),
                "failed": self.failed(),
                "skipped": self.skipped(),
                "pass_rate": self.pass_rate(),
                "agents": self.agent_summary(),
            },
        }


# ═══════════════════════════════════════════════════════════════════
# IssueTracker
# ═══════════════════════════════════════════════════════════════════
class IssueTracker:
    """IssueTracker — 问题频率统计器。

    按严重级别（critical / error / warning / info / debug）统计问题出现频率。
    支持按严重级别、类别、模块维度分析。

    Attributes:
        issues: 所有已记录的问题列表。
    """

    def __init__(self) -> None:
        """IssueTracker — 初始化问题统计器。"""
        self.issues: List[Dict[str, Any]] = []

    def record_issue(
        self,
        severity: str,
        category: str,
        module: Optional[str] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """record_issue — 记录一个问题。

        Args:
            severity: 严重级别。
                      "critical" / "error" / "warning" / "info" / "debug"。
            category: 问题类别（如 "logic_error", "import_error", "timeout"）。
            module:   相关模块名称（可选，如 "swarm_metrics"）。
            message:  问题描述消息（可选）。
            details:  附加详情字典（可选）。

        Returns:
            新创建的问题记录字典。

        Raises:
            ValueError: 当 severity 不在合法范围内时抛出。
        """
        if severity not in SEVERITY_ORDER:
            raise ValueError(
                f"severity 必须是 {SEVERITY_ORDER} 之一，收到 {severity!r}。"
            )

        record: Dict[str, Any] = {
            "severity": severity,
            "category": category,
            "module": module,
            "message": message,
            "details": details or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }
        self.issues.append(record)
        _log.info("记录问题", severity=severity, category=category, module=module)
        return record

    def frequency_by_severity(self) -> Dict[str, int]:
        """frequency_by_severity — 按严重级别统计问题频率。

        Returns:
            {severity: count, ...} 格式的字典。
        """
        freq: Dict[str, int] = {}
        for issue in self.issues:
            sev = issue["severity"]
            freq[sev] = freq.get(sev, 0) + 1
        # 补全未出现的级别为零值
        for sev in SEVERITY_ORDER:
            freq.setdefault(sev, 0)
        return freq

    def frequency_by_category(self) -> Dict[str, int]:
        """frequency_by_category — 按类别统计问题频率。

        Returns:
            {category: count, ...} 格式的字典。
        """
        freq: Dict[str, int] = {}
        for issue in self.issues:
            cat = issue["category"]
            freq[cat] = freq.get(cat, 0) + 1
        return freq

    def frequency_by_module(self) -> Dict[str, int]:
        """frequency_by_module — 按模块统计问题频率。

        Returns:
            {module: count, ...} 格式的字典。
        """
        freq: Dict[str, int] = {}
        for issue in self.issues:
            mod = issue["module"] or "unknown"
            freq[mod] = freq.get(mod, 0) + 1
        return freq

    def top_issues(self, n: int = 5) -> List[Dict[str, Any]]:
        """top_issues — 获取最严重的前 N 个问题。

        按严重级别权重降序排列，同级别按时间升序排列。

        Args:
            n: 返回条数（默认 5）。

        Returns:
            前 N 个最严重的问题记录列表。
        """
        sorted_issues = sorted(
            self.issues,
            key=lambda x: (
                -SEVERITY_WEIGHT.get(x["severity"], 0),
                x["timestamp"],
            ),
        )
        return sorted_issues[:n]

    def weighted_score(self) -> int:
        """weighted_score — 计算加权问题严重性得分。

        得分 = 每个问题的权重之和。得分越高表示问题越严重。

        Returns:
            加权得分（整数）。
        """
        return sum(SEVERITY_WEIGHT.get(issue["severity"], 0) for issue in self.issues)

    def total(self) -> int:
        """total — 获取问题总数。

        Returns:
            所有记录的问题数量。
        """
        return len(self.issues)

    def to_dict(self) -> Dict[str, Any]:
        """to_dict — 将问题统计器数据序列化为字典。

        Returns:
            包含 issues、frequency 和 summary 的字典。
        """
        return {
            "issues": list(self.issues),
            "frequency": {
                "by_severity": self.frequency_by_severity(),
                "by_category": self.frequency_by_category(),
                "by_module": self.frequency_by_module(),
            },
            "summary": {
                "total": self.total(),
                "weighted_score": self.weighted_score(),
                "top_issues": self.top_issues(5),
            },
        }


# ═══════════════════════════════════════════════════════════════════
# MetricsStore
# ═══════════════════════════════════════════════════════════════════
class MetricsStore:
    """MetricsStore — 指标数据持久化存储。

    负责将指标数据保存为 JSON 文件，以及从 JSON 文件加载恢复。

    用法示例
    --------
        store = MetricsStore()
        store.save(data, "tmp_agent/metrics/round-15.json")
        restored = store.load("tmp_agent/metrics/round-15.json")
    """

    @staticmethod
    def save(
        data: Dict[str, Any],
        path: Union[str, os.PathLike],
        indent: int = 2,
        ensure_ascii: bool = False,
    ) -> bool:
        """save — 将指标数据保存为 JSON 文件。

        自动创建父目录。写入成功返回 True，失败返回 False。

        Args:
            data:          要保存的字典数据。
            path:          目标文件路径。
            indent:        JSON 缩进空格数（默认 2）。
            ensure_ascii:  是否确保 ASCII 输出（默认 False，保留中文）。

        Returns:
            保存成功 True，失败 False。
        """
        try:
            json_str = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent, default=str)
            return write_file_safe(path, json_str)
        except (TypeError, ValueError, OverflowError) as exc:
            _log.error("JSON 序列化失败", path=str(path), error=str(exc))
            return False

    @staticmethod
    def load(path: Union[str, os.PathLike]) -> Optional[Dict[str, Any]]:
        """load — 从 JSON 文件加载指标数据。

        Args:
            path: JSON 文件路径。

        Returns:
            加载的字典数据，若文件不存在或解析失败则返回 None。
        """
        content = read_file_safe(path)
        if content is None:
            return None
        try:
            data: Dict[str, Any] = json.loads(content)
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            _log.error("JSON 解析失败", path=str(path), error=str(exc))
            return None

    @staticmethod
    def list_metrics(
        directory: Union[str, os.PathLike],
        pattern: str = "round-*.json",
    ) -> List[Dict[str, Any]]:
        """list_metrics — 列出指定目录下的指标文件概要。

        扫描匹配 pattern 的 JSON 文件，加载并返回其 round 摘要信息。

        Args:
            directory: 扫描目录。
            pattern:   文件名 glob pattern（默认 "round-*.json"）。

        Returns:
            每个文件 {path, round_num, timestamp, task_count} 的列表。
        """
        import glob

        dir_str = os.fspath(directory)
        results: List[Dict[str, Any]] = []
        for filepath in sorted(glob.glob(os.path.join(dir_str, pattern))):
            data = MetricsStore.load(filepath)
            if data is None:
                continue
            summary = data.get("summary", {})
            timer_data = data.get("timer", {})
            summary_data = timer_data.get("summary", {})
            results.append({
                "path": filepath,
                "round_num": timer_data.get("round_num", summary.get("last_round")),
                "timestamp": summary.get("generated_at"),
                "task_count": data.get("tasks", {}).get("summary", {}).get("total", 0),
            })
        return results


# ═══════════════════════════════════════════════════════════════════
# MetricsReporter
# ═══════════════════════════════════════════════════════════════════
class MetricsReporter:
    """MetricsReporter — 指标报告生成器。

    将 SwarmMetrics 的数据转换为可读的文本摘要或结构化 JSON 报告。
    """

    # ── 文本报告子方法 ──────────────────────────────────────────

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

        # 按严重级别
        by_severity = issue_freq.get("by_severity", {})
        if any(v > 0 for v in by_severity.values()):
            lines.append("  按严重级别:")
            for sev in SEVERITY_ORDER:
                count = by_severity.get(sev, 0)
                if count > 0:
                    lines.append(f"    {sev:<10s}: {count}")
            lines.append("")

        # 按类别
        by_category = issue_freq.get("by_category", {})
        if by_category:
            lines.append("  按类别（Top 5）:")
            sorted_cats = sorted(by_category.items(), key=lambda x: -x[1])[:5]
            for cat, count in sorted_cats:
                lines.append(f"    {cat:<20s}: {count}")
            lines.append("")

        # Top 问题
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

        # ── 尾部 ──
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


# ═══════════════════════════════════════════════════════════════════
# SwarmMetrics
# ═══════════════════════════════════════════════════════════════════
class SwarmMetrics:
    """SwarmMetrics — Swarm 自我进化循环指标收集器。

    整合 RoundTimer、TaskTracker、IssueTracker、MetricsStore、MetricsReporter，
    提供统一的指标收集、报告生成和持久化接口。

    用法示例
    --------
        metrics = SwarmMetrics()
        metrics.start_round(round_num=15)
        metrics.record_task(agent="agent-1", status="completed", duration_sec=120)
        metrics.record_issue(severity="error", category="logic_error", module="swarm_metrics")
        report = metrics.generate_report()
        metrics.save("tmp_agent/metrics/round-15.json")
    """

    def __init__(self) -> None:
        """SwarmMetrics — 初始化指标收集器。"""
        self.timer = RoundTimer()
        self.tasks = TaskTracker()
        self.issues = IssueTracker()
        self.store = MetricsStore()
        self.reporter = MetricsReporter()
        self._meta: Dict[str, Any] = {}

    def set_meta(self, key: str, value: Any) -> None:
        """set_meta — 设置元信息（如项目名称、版本号等）。

        Args:
            key:   元信息键名。
            value: 元信息值。
        """
        self._meta[key] = value

    # ── 轮次计时委托 ──────────────────────────────────────────

    def start_round(self, round_num: int) -> None:
        """start_round — 开始一轮计时。

        Args:
            round_num: 轮次编号。
        """
        self.timer.start_round(round_num)

    def end_round(self) -> Dict[str, Any]:
        """end_round — 结束当前轮次计时。

        Returns:
            当前轮次的计时记录。
        """
        return self.timer.end_round()

    def current_round(self) -> Optional[int]:
        """current_round — 获取当前正在计时的轮次编号。"""
        return self.timer.current_round()

    # ── 任务记录委托 ──────────────────────────────────────────

    def record_task(
        self,
        agent: str,
        status: str,
        duration_sec: Optional[float] = None,
        task_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """record_task — 记录一个任务的执行结果。

        Args:
            agent:       Agent 标识符。
            status:      任务状态。"completed" / "failed" / "skipped" / "in_progress"。
            duration_sec: 任务耗时（秒，可选）。
            task_name:    任务名称（可选）。
            details:      附加详情字典（可选）。

        Returns:
            新创建的任务记录字典。
        """
        return self.tasks.record_task(
            agent=agent,
            status=status,
            duration_sec=duration_sec,
            task_name=task_name,
            details=details,
        )

    # ── 问题记录委托 ──────────────────────────────────────────

    def record_issue(
        self,
        severity: str,
        category: str,
        module: Optional[str] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """record_issue — 记录一个问题。

        Args:
            severity: 严重级别。
                      "critical" / "error" / "warning" / "info" / "debug"。
            category: 问题类别（如 "logic_error", "import_error", "timeout"）。
            module:   相关模块名称（可选）。
            message:  问题描述消息（可选）。
            details:  附加详情字典（可选）。

        Returns:
            新创建的问题记录字典。
        """
        return self.issues.record_issue(
            severity=severity,
            category=category,
            module=module,
            message=message,
            details=details,
        )

    # ── 报告生成 ──────────────────────────────────────────────

    def generate_report(self, fmt: str = "text") -> str:
        """generate_report — 生成指标报告。

        Args:
            fmt: 报告格式。"text"（人类可读文本）或 "json"（结构化 JSON）。

        Returns:
            格式化的报告字符串。

        Raises:
            ValueError: 当 fmt 不是 "text" 或 "json" 时抛出。
        """
        data = self.to_dict()
        if fmt == "text":
            return self.reporter.generate_text_report(data)
        elif fmt == "json":
            return self.reporter.generate_json_report(data)
        else:
            raise ValueError(f"fmt 必须是 'text' 或 'json'，收到 {fmt!r}。")

    # ── 持久化存储 ──────────────────────────────────────────────

    def save(self, path: Union[str, os.PathLike]) -> bool:
        """save — 将完整指标数据保存为 JSON 文件。

        Args:
            path: 目标文件路径（如 "tmp_agent/metrics/round-15.json"）。

        Returns:
            保存成功 True，失败 False。
        """
        data = self.to_dict()
        log_step(f"保存指标数据到 {path}")
        return self.store.save(data, path)

    @classmethod
    def load(cls, path: Union[str, os.PathLike]) -> Optional["SwarmMetrics"]:
        """load — 从 JSON 文件加载指标数据并恢复 SwarmMetrics 实例。

        Args:
            path: JSON 文件路径。

        Returns:
            恢复的 SwarmMetrics 实例，若加载失败则返回 None。
        """
        data = MetricsStore.load(path)
        if data is None:
            return None

        instance = cls()
        instance._meta = data.get("meta", {})

        # 恢复轮次计时器
        timer_data = data.get("timer", {})
        if timer_data.get("rounds"):
            instance.timer.rounds = list(timer_data["rounds"])
        if timer_data.get("current"):
            instance.timer._current = dict(timer_data["current"])

        # 恢复任务追踪器
        tasks_data = data.get("tasks", {})
        if tasks_data.get("tasks"):
            instance.tasks.tasks = list(tasks_data["tasks"])

        # 恢复问题统计器
        issues_data = data.get("issues", {})
        if issues_data.get("issues"):
            instance.issues.issues = list(issues_data["issues"])

        return instance

    def to_dict(self) -> Dict[str, Any]:
        """to_dict — 将完整指标数据序列化为字典。

        Returns:
            包含 timer、tasks、issues、summary 的嵌套字典。
        """
        now = datetime.datetime.now().isoformat()
        return {
            "meta": dict(self._meta),
            "timer": self.timer.to_dict(),
            "tasks": self.tasks.to_dict(),
            "issues": self.issues.to_dict(),
            "summary": {
                "generated_at": now,
                "current_round": self.current_round(),
                "data_version": "1.0",
            },
        }


# ═══════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════
def main() -> None:
    """main — CLI 入口，演示 SwarmMetrics 的完整使用流程。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Swarm Metrics — 自我进化循环指标收集工具",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="运行演示示例（生成示例指标数据并输出报告）",
    )
    parser.add_argument(
        "--save",
        type=str,
        default="",
        help="将演示示例的指标数据保存到指定路径",
    )
    parser.add_argument(
        "--load",
        type=str,
        default="",
        help="从指定 JSON 文件加载并显示指标报告",
    )
    parser.add_argument(
        "--fmt",
        type=str,
        choices=["text", "json"],
        default="text",
        help="报告输出格式（默认 text）",
    )
    args = parser.parse_args()

    if args.load:
        metrics = SwarmMetrics.load(args.load)
        if metrics is None:
            print(f"错误：无法从 {args.load} 加载指标数据。", file=sys.stderr)
            sys.exit(1)
        report = metrics.generate_report(fmt=args.fmt)
        print(report)
        return

    if args.demo:
        # ── 构建演示数据 ──
        log_step("开始演示 SwarmMetrics")
        metrics = SwarmMetrics()

        # 模拟 3 轮循环
        for round_num in range(1, 4):
            metrics.start_round(round_num=round_num)

            # 模拟 3 个 Agent 的任务
            for agent_id in ["agent-1", "agent-2", "agent-3"]:
                import random
                status = random.choices(
                    ["completed", "completed", "failed", "skipped"],
                    weights=[6, 2, 1, 1],
                )[0]
                metrics.record_task(
                    agent=agent_id,
                    status=status,
                    duration_sec=random.uniform(10, 300),
                    task_name=f"实现 {agent_id} 任务",
                )

            # 模拟一些问题
            if round_num == 1:
                metrics.record_issue(
                    severity="error",
                    category="import_error",
                    module="swarm_metrics",
                    message="缺少依赖包 pyyaml",
                )
            if round_num == 2:
                metrics.record_issue(
                    severity="warning",
                    category="timeout",
                    module="agent-2",
                    message="任务执行超时",
                )
            metrics.record_issue(
                severity="info",
                category="retry",
                module="agent-1",
                message="重试成功",
            )

            metrics.end_round()

        # 输出报告
        report = metrics.generate_report(fmt=args.fmt)
        print(report)

        # 保存
        if args.save:
            metrics.save(args.save)
            log_step(f"指标数据已保存到 {args.save}")

        log_step("演示完成")
        return

    parser.print_help()



def record_sqlite_metric(operation: str, sqlite_path: str = "") -> None:
    """兼容包装 — 记录 SQLite 操作指标。

    Args:
        operation: 操作类型（如 insert, select, vacuum）
        sqlite_path: SQLite 文件路径（可选）
    """
    print(f"[swarm_metrics] sqlite:{operation} {sqlite_path}")

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════
# ContainerPoolMonitor — Docker 容器预热池自动扩缩容监控（项1）
# ═══════════════════════════════════════════════════════════════════
# 监控等待队列长度，当队列 > 2 且持续 > 10 秒时自动 docker run
# 扩容一个新容器（上限 10 个）。每 5 分钟扫描一次池状态，当
# 空闲容器 > 3 时，停止最旧的几个容器进行缩容。
# ═══════════════════════════════════════════════════════════════════

class ContainerPoolMonitor:
    """ContainerPoolMonitor — Docker 容器预热池监控器。

    负责自动扩缩容 Docker 容器池，确保系统有足够的容器资源
    处理任务队列。扩容驱动：等待队列长度 + 等待时长联合触发。
    缩容驱动：定时扫描空闲容器数。

    Attributes:
        max_pool_size:   容器池最大数量（上限）。
        min_pool_size:   容器池最小保留数量。
        queue_scale_up_threshold: 扩容队列长度阈值。
        queue_wait_seconds:        扩容等待时间阈值（防抖动）。
        idle_cleanup_threshold:    空闲容器清理阈值。
        docker_image:   预热容器使用的 Docker 镜像。
        container_workdir: 容器内工作目录。
        pool_size:      当前容器池大小。
        queue_length:   当前等待队列长度。
        queue_start_time: 当前队列长度首次达到扩容阈值的时间。
        last_scan_time:  上次池状态扫描时间（时间戳）。
    """

    def __init__(
        self,
        max_pool_size: int = 10,
        min_pool_size: int = 2,
        queue_scale_up_threshold: int = 2,
        queue_wait_seconds: int = 10,
        idle_cleanup_threshold: int = 3,
        docker_image: str = "python:3.11-slim",
        container_workdir: str = "/workspace",
    ) -> None:
        """ContainerPoolMonitor — 初始化容器池监控器。

        Args:
            max_pool_size:   容器池最大数量（默认 10）。
            min_pool_size:   容器池最小保留数量（默认 2）。
            queue_scale_up_threshold: 扩容队列长度阈值（默认 2）。
            queue_wait_seconds:        扩容等待时间阈值秒（默认 10）。
            idle_cleanup_threshold:    空闲容器清理阈值（默认 3）。
            docker_image:   预热容器使用的 Docker 镜像（默认 python:3.11-slim）。
            container_workdir: 容器内工作目录（默认 /workspace）。

        为什么这么设计：
        - 队列长度 + 等待时长联合触发：防止短暂流量尖峰导致频繁扩缩容。
        - 定时扫描空闲容器：避免容器长时间闲置浪费资源。
        - 与 config.yaml 的 container_pool 节参数结构一致。
        """
        self.max_pool_size = max_pool_size
        self.min_pool_size = min_pool_size
        self.queue_scale_up_threshold = queue_scale_up_threshold
        self.queue_wait_seconds = queue_wait_seconds
        self.idle_cleanup_threshold = idle_cleanup_threshold
        self.docker_image = docker_image
        self.container_workdir = container_workdir
        self.pool_size = 0
        self.queue_length = 0
        self.queue_start_time: Optional[float] = None
        self.last_scan_time: float = time.time()

    def record_queue_length(self, length: int) -> None:
        """record_queue_length — 记录当前等待队列长度，必要时触发扩容。

        Args:
            length: 当前等待队列的长度。

        作用：更新队列长度数据。
        原理：队列长度 + 等待时长联合触发扩容，避免误判。
        逻辑：
        - 如果 length > queue_scale_up_threshold：
            - 首次进入阈值记录当前时间为 queue_start_time
            - 如果已持续 >= queue_wait_seconds，调用 scale_up()
        - 如果 length <= queue_scale_up_threshold：
            - 重置 queue_start_time 为 None
            - 更新 last_scan_time

        面试追问：
        - 为什么不直接用队列长度作为唯一指标？答：防止流量瞬变的抖动。
        - 等待时长如何保证准确性？答：使用 time.time() 差值，精度毫秒级。
        """
        now = time.time()
        self.queue_length = length

        if length > self.queue_scale_up_threshold:
            if self.queue_start_time is None:
                self.queue_start_time = now
                _log.info("队列达到扩容阈值",
                          length=length, threshold=self.queue_scale_up_threshold)
            elif now - self.queue_start_time >= self.queue_wait_seconds:
                self.scale_up()
                # 扩容后重置计时器，防止触发多次
                self.queue_start_time = now
        else:
            self.queue_start_time = None

        self.last_scan_time = now

    def check_pool_health(self) -> dict:
        """check_pool_health — 检查容器池健康状态，必要时缩容。

        Returns:
            包含当前池状态的字典：
            {
                "pool_size": int,
                "idle_count": int,
                "action": "scale_down" | "noop",
                "action_count": int,
            }

        作用：每 5 分钟扫描一次池状态。
        原理：空闲容器过多浪费资源，定期缩容到合理水平。
        逻辑：
        - 调用 _count_pool_containers() 获取当前容器数和空闲数
        - 如果空闲数 > idle_cleanup_threshold：
            - 计算需要停止的容器数 = 空闲数 - idle_cleanup_threshold
            - 调用 scale_down(需要停止的容器数)
        - 否则不做操作

        面试追问：
        - 如何判定容器"空闲"？答：容器正在运行但没有被 mark_in_use() 标记。
        - 缩容策略为什么停最旧的？答：FIFO 策略，越早创建的容器被复用的概率越低。
        """
        now = time.time()
        elapsed = now - self.last_scan_time

        pool_info = self._count_pool_containers()
        idle_count = pool_info.get("idle_count", 0)
        self.pool_size = pool_info.get("total_count", 0)

        result = {
            "pool_size": self.pool_size,
            "idle_count": idle_count,
            "action": "noop",
            "action_count": 0,
        }

        if idle_count > self.idle_cleanup_threshold:
            to_stop = idle_count - self.idle_cleanup_threshold
            _log.info("空闲容器过多，触发缩容",
                      idle=idle_count, threshold=self.idle_cleanup_threshold,
                      to_stop=to_stop)
            self.scale_down(to_stop)
            result["action"] = "scale_down"
            result["action_count"] = to_stop

        self.last_scan_time = now
        return result

    def scale_up(self) -> bool:
        """scale_up — 扩容：启动一个新 Docker 容器（在不超过上限的前提下）。

        Returns:
            True: 扩容成功（或已满无需扩容）。
            False: 扩容失败（docker run 报错或超时）。

        作用：增加容器池容量以应对任务积压。
        原理：上限 max_pool_size 防资源耗尽，下限 min_pool_size 保常驻能力。
        逻辑：
        - 先统计当前容器数
        - 如果 pool_size >= max_pool_size，直接返回 True（已满）
        - 生成容器名 sandbox-pool-{pool_size}
        - 执行 docker run -d --name {name} {image} sleep infinity
        - 成功后 pool_size += 1
        """
        pool_info = self._count_pool_containers()
        current_size = pool_info.get("total_count", 0)

        if current_size >= self.max_pool_size:
            _log.info("容器池已满，无需扩容",
                      size=current_size, max_size=self.max_pool_size)
            return True

        container_name = f"sandbox-pool-{current_size}"
        try:
            result = subprocess.run(
                ["docker", "run", "-d",
                 "--name", container_name,
                 self.docker_image,
                 "sleep", "infinity"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.pool_size = current_size + 1
                _log.info("扩容成功", name=container_name,
                          new_size=self.pool_size)
                return True
            else:
                _log.error("扩容失败", name=container_name,
                           error=result.stderr.strip())
                return False
        except subprocess.TimeoutExpired:
            _log.error("扩容超时（30s）", name=container_name)
            return False
        except FileNotFoundError:
            _log.error("Docker 命令未找到，请检查 Docker 是否安装")
            return False

    def scale_down(self, count: int) -> bool:
        """scale_down — 缩容：停止最旧的 N 个容器。

        Args:
            count: 要停止的容器数量。

        Returns:
            True: 缩容完成。False: 部分或全部容器停止失败。

        作用：释放空闲容器占用的资源。
        原理：按容器创建时间排序，FIFO 策略，最旧的优先停止。
        逻辑：
        - 调用 _list_sorted_containers() 获取按创建时间排序的容器列表
        - 取前 count 个容器
        - 对每个容器执行 docker stop + docker rm
        - 如果停止后 pool_size < min_pool_size，停止缩容
        """
        containers = self._list_sorted_containers()
        if not containers:
            return True

        to_stop = containers[:count]
        all_success = True

        for c in to_stop:
            name = c.get("name", "")
            try:
                stop_result = subprocess.run(
                    ["docker", "stop", name],
                    capture_output=True, text=True, timeout=15,
                )
                if stop_result.returncode == 0:
                    subprocess.run(
                        ["docker", "rm", name],
                        capture_output=True, text=True, timeout=10,
                    )
                    _log.info("缩容成功", name=name)
                else:
                    _log.warning("缩容失败", name=name,
                                 error=stop_result.stderr.strip())
                    all_success = False
            except subprocess.TimeoutExpired:
                _log.warning("缩容超时", name=name)
                all_success = False

        pool_info = self._count_pool_containers()
        self.pool_size = pool_info.get("total_count", 0)

        # 确保不低于最小池大小
        if self.pool_size < self.min_pool_size:
            _log.info("容器池低于最小值，回补到 %d", self.min_pool_size)
            for _ in range(self.min_pool_size - self.pool_size):
                self.scale_up()

        return all_success

    def get_pool_status(self) -> dict:
        """get_pool_status — 获取当前容器池状态。

        Returns:
            包含完整池状态的字典：
            {
                "pool_size": int,
                "queue_length": int,
                "max_pool_size": int,
                "min_pool_size": int,
                "queue_start_time": float or None,
                "last_scan_time": float,
                "idle_count": int,
            }

        作用：提供容器池的快照，供 metrics 收集和外部查询。
        原理：综合内部状态和外部 Docker 容器列表。
        逻辑：
        - 调用 _count_pool_containers() 获取实际 Docker 容器数据
        - 合并内部记录的队列长度和配置信息
        """
        pool_info = self._count_pool_containers()
        return {
            "pool_size": pool_info.get("total_count", 0),
            "queue_length": self.queue_length,
            "max_pool_size": self.max_pool_size,
            "min_pool_size": self.min_pool_size,
            "queue_start_time": self.queue_start_time,
            "last_scan_time": self.last_scan_time,
            "idle_count": pool_info.get("idle_count", 0),
        }

    # ── 内部辅助方法 ──────────────────────────────────────────────

    def _count_pool_containers(self) -> dict:
        """_count_pool_containers — 统计当前容器池中容器数量和空闲数量。

        Returns:
            {"total_count": int, "idle_count": int}

        作用：通过 docker ps 获取实际的容器状态。
        原理：按容器名前缀 sandbox-pool- 过滤。
        逻辑：
        - docker ps --filter name=sandbox-pool- --format json
        - 解析 JSON 输出，统计总数
        - 目前没有复杂的心跳机制，所有运行中的容器都视为"活跃"
        - idle_count 需要额外信息（待 future 实现 mark_in_use/mark_idle）
        """
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=sandbox-pool-",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"total_count": 0, "idle_count": 0}

            names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
            total = len(names)
            # 简化版：所有容器都视为空闲（真实场景需集成 mark_in_use/mark_idle）
            return {"total_count": total, "idle_count": total}
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"total_count": 0, "idle_count": 0}

    def _list_sorted_containers(self) -> list:
        """_list_sorted_containers — 获取按创建时间排序的容器列表（旧→新）。

        Returns:
            容器信息列表，每个元素包含 name 和 created_at。

        作用：为缩容提供 FIFO 顺序。
        原理：docker ps --sort=created 返回按创建时间排序的列表。
        逻辑：
        - 按创建时间升序排列，最旧的在前
        """
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=sandbox-pool-",
                 "--format", "{{.Names}}\t{{.CreatedAt}}",
                 "--sort", "created"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return []

            containers = []
            for line in result.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    containers.append({"name": parts[0], "created_at": parts[1]})
                elif len(parts) == 1:
                    containers.append({"name": parts[0], "created_at": ""})
            return containers
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
