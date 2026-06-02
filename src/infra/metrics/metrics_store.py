"""metrics_store.py — Swarm 自我进化循环的指标收集模块

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


