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


