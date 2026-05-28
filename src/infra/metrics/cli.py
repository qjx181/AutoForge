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
        log_step("开始演示 SwarmMetrics")
        metrics = SwarmMetrics()

        for round_num in range(1, 4):
            metrics.start_round(round_num=round_num)

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

        report = metrics.generate_report(fmt=args.fmt)
        print(report)

        if args.save:
            metrics.save(args.save)
            log_step(f"指标数据已保存到 {args.save}")

        log_step("演示完成")
        return

    parser.print_help()
