"""evolve/pipeline — 持续优化引擎主入口（九维全覆盖）

核心公式：扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快
run_optimization_pipeline() 调用 optimizer_core 对每个目标目录执行九维扫描，
汇总结果并写入 state.json。
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.core.evolve.config_ext import OPT_DIMENSIONS
from src.core.evolve.logging import relog
from src.core.evolve.state import load_state, save_state

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


def run_optimization_pipeline(
    scan_targets: list[Path],
    timestamp: str,
    dimensions: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """run_optimization_pipeline — 持续优化引擎主入口（九维全覆盖）

    核心公式：扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快

    步骤：
      1. 对每个目标目录执行 optimizer_core.run_full_pipeline()
      2. 汇总各维度扫描结果
      3. 更新 state.json 记录本轮扫描结果

    Args:
        scan_targets: 要优化的目标目录列表（支持多项目同时优化）
        timestamp: 当前轮次时间戳
        dimensions: 要优化的维度列表，默认全部9个
        dry_run: True=只扫描不修改（预览模式）

    Returns:
        dict: {
            "targets": [str, ...],    # 扫描的目标目录
            "total_findings": int,    # 总发现数
            "total_fixes_applied": int, # 总修复数
            "total_verifications_passed": int,
            "total_verifications_failed": int,
            "score_delta": int,       # 评分变化
            "by_target": [dict, ...], # 每个目标的详细结果
            "at": str,
        }
    """
    if dimensions is None:
        dimensions = OPT_DIMENSIONS

    from src.analysis.optimizer_core import run_full_pipeline, DIMENSION_NAMES

    overall = {
        "targets": [],
        "total_findings": 0,
        "total_fixes_applied": 0,
        "total_verifications_passed": 0,
        "total_verifications_failed": 0,
        "score_delta": 0,
        "by_target": [],
        "at": timestamp,
    }

    for target in scan_targets:
        if not target or not target.exists():
            relog("ℹ️", "跳过不存在目录: %s", target)
            continue

        target_str = str(target)
        relog("🔍", "优化目标: %s（维度: %s）", target_str, ", ".join(dimensions))
        overall["targets"].append(target_str)

        try:
            pipeline_result = run_full_pipeline(target_str, dimensions=dimensions)
        except Exception as e:
            relog("⚠️", "optimizer_core 执行失败 [%s]: %s", target_str, e)
            overall["by_target"].append({
                "target": target_str,
                "error": str(e),
            })
            continue

        total_issues = pipeline_result.get("total_issues", 0)
        overall["total_findings"] += total_issues

        by_dimension = {}
        for dim_name, dim_result in pipeline_result.get("dimensions", {}).items():
            dim_label = DIMENSION_NAMES.get(dim_name, dim_name)
            by_dimension[dim_name] = {
                "label": dim_label,
                "score": dim_result.get("score", 0),
                "issues": dim_result.get("issue_count", 0),
                "scan_time_ms": dim_result.get("scan_time_ms", 0),
            }

        overall["by_target"].append({
            "target": target_str,
            "project_name": pipeline_result.get("project_name", target.name),
            "language": pipeline_result.get("language", "unknown"),
            "overall_score": pipeline_result.get("overall_score", 0),
            "total_issues": total_issues,
            "critical_issues": pipeline_result.get("critical_issues", 0),
            "scan_time_ms": pipeline_result.get("total_scan_time_ms", 0),
            "by_dimension": by_dimension,
            "summary": pipeline_result.get("summary", ""),
        })

        relog(
            "📊 [%s] 整体 %d/100 | 发现 %d（critical: %d）| 耗时 %.0fms",
            pipeline_result.get("project_name", target.name),
            pipeline_result.get("overall_score", 0),
            total_issues,
            pipeline_result.get("critical_issues", 0),
            pipeline_result.get("total_scan_time_ms", 0),
        )

    state = load_state()
    state["last_optimization"] = {
        "targets": overall["targets"],
        "dimensions": dimensions,
        "total_findings": overall["total_findings"],
        "total_fixes_applied": overall["total_fixes_applied"],
        "total_verifications_passed": overall["total_verifications_passed"],
        "total_verifications_failed": overall["total_verifications_failed"],
        "score_delta": overall["score_delta"],
        "dry_run": dry_run,
        "at": timestamp,
    }
    save_state(state)

    relog(
        "🏁 优化完成：%d 个目标，发现 %d，修复 %d，验证 %d/%d",
        len(overall["targets"]),
        overall["total_findings"],
        overall["total_fixes_applied"],
        overall["total_verifications_passed"],
        overall["total_verifications_passed"] + overall["total_verifications_failed"],
    )

    return overall
