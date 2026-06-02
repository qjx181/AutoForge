"""round_timer.py — Swarm 自我进化循环的指标收集模块

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


