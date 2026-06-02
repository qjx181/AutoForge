"""issue_tracker.py — Swarm 自我进化循环的指标收集模块

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


