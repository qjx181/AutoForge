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

        timer_data = data.get("timer", {})
        if timer_data.get("rounds"):
            instance.timer.rounds = list(timer_data["rounds"])
        if timer_data.get("current"):
            instance.timer._current = dict(timer_data["current"])

        tasks_data = data.get("tasks", {})
        if tasks_data.get("tasks"):
            instance.tasks.tasks = list(tasks_data["tasks"])

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


