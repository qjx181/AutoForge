#!/usr/bin/env python3
"""optimizer_core.py — 持续优化引擎核心编排器

作用：
  将 9 个维度的扫描器组织为一条完整的优化流水线：

    扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快

核心公式：
    Score(项目) = 100 - Σ(严重问题数 × 权重)
    每个维度独立评分后，取加权平均作为项目整体评分。

用法：
    from optimizer_core import run_full_pipeline, run_single_dimension
    result = run_full_pipeline("/path/to/project")

    # 或单维度调试：
    from optimizer_core import run_single_dimension
    result = run_single_dimension("/path/to/project", "security")
"""

import time
import os
import sys
from datetime import datetime
from typing import Optional

# ── 延迟导入避免循环依赖 ──────────────────────────────────────────────
PROJECT_ANALYZER = None
DIMS = None

# ── 维度名称映射（中英双语，供外部导入）────────────────────────────────
DIMENSION_NAMES = {
    "security": "安全",
    "performance": "性能",
    "asyncification": "异步化",
    "quality": "代码质量",
    "testing": "测试覆盖",
    "architecture": "架构",
    "documentation": "文档",
    "configuration": "配置",
    "deadcode": "死代码",
}


def _lazy_imports():
    global PROJECT_ANALYZER, DIMS
    if PROJECT_ANALYZER is None:
        from . import project_analyzer as _pa
        PROJECT_ANALYZER = _pa
        from . import dims as _d
        DIMS = _d


# ═══════════════════════════════════════════════════════════════════════
# 维度 → 扫描函数映射
# ═══════════════════════════════════════════════════════════════════════

DIMENSION_SCANNERS = {
    "security": "scan_security",
    "performance": "scan_performance",
    "asyncification": "scan_asyncification",
    "quality": "scan_quality",
    "testing": "scan_testing",
    "architecture": "scan_architecture",
    "documentation": "scan_documentation",
    "configuration": "scan_configuration",
    "deadcode": "scan_deadcode",
}

# 维度权重（影响整体评分）
DIMENSION_WEIGHTS = {
    "security": 2.0,        # 安全权重最高
    "performance": 1.5,
    "asyncification": 1.5,
    "quality": 1.0,
    "testing": 1.0,
    "architecture": 1.0,
    "documentation": 0.5,
    "configuration": 1.0,
    "deadcode": 0.5,
}


# ═══════════════════════════════════════════════════════════════════════
# 核心函数
# ═══════════════════════════════════════════════════════════════════════


def run_single_dimension(
    project_path: str,
    dimension: str,
    blueprint: Optional[object] = None,
) -> dict:
    """运行单个维度的扫描。

    Args:
        project_path: 项目路径
        dimension: 维度名称（如 "security", "quality"）
        blueprint: 已有蓝图（避免重复分析项目结构）

    Returns:
        dict: {
            "dimension": str,
            "score": int,
            "issues": [dict],
            "issue_count": int,
            "summary": str,
            "scan_time_ms": float,
        }
    """
    _lazy_imports()

    if blueprint is None:
        blueprint = PROJECT_ANALYZER.analyze_project(project_path)

    scanner_name = DIMENSION_SCANNERS.get(dimension)
    if scanner_name is None:
        return {
            "dimension": dimension,
            "score": 100,
            "issues": [],
            "issue_count": 0,
            "summary": f"未知维度: {dimension}",
            "scan_time_ms": 0,
            "error": f"Unknown dimension: {dimension}",
        }

    scanner = getattr(DIMS, scanner_name, None)
    if scanner is None:
        return {
            "dimension": dimension,
            "score": 100,
            "issues": [],
            "issue_count": 0,
            "summary": f"扫描器不可用: {scanner_name}",
            "scan_time_ms": 0,
            "error": f"Scanner not found: {scanner_name}",
        }

    t0 = time.perf_counter()
    result = scanner(blueprint)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    result["scan_time_ms"] = round(elapsed_ms, 1)
    return result


def run_full_pipeline(
    project_path: str,
    dimensions: Optional[list[str]] = None,
    max_parallel: int = 4,
) -> dict:
    """运行完整的 9 维度优化流水线。

    核心公式：扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快

    Args:
        project_path: 项目根目录路径
        dimensions: 要扫描的维度列表（默认全部启用）
        max_parallel: 最大并发扫描数（Windows 上固定为 1）

    Returns:
        dict: {
            "project": str,
            "project_name": str,
            "language": str,
            "blueprint": OptimizationBlueprint,
            "dimensions": [每个维度的扫描结果],
            "overall_score": int,          # 加权平均分
            "total_issues": int,
            "critical_issues": int,
            "total_scan_time_ms": float,
            "at": str (ISO timestamp),
        }
    """
    _lazy_imports()

    t0 = time.perf_counter()
    timestamp = datetime.now().isoformat()

    # 1. 项目结构分析（一次，全维度共享）
    blueprint = PROJECT_ANALYZER.analyze_project(project_path)

    # 2. 确定要扫描的维度
    if dimensions is None:
        dimensions = [
            dim for dim, enabled in blueprint.enabled_dimensions.items()
            if enabled and dim in DIMENSION_SCANNERS
        ]

    # 3. 按顺序执行所有维度扫描（Windows 单线程）
    dimension_results = {}
    for dim in DIMS.DIMENSION_ORDER:
        if dim not in dimensions:
            continue
        result = run_single_dimension(project_path, dim, blueprint)
        dimension_results[dim] = result

    # 4. 计算加权整体评分
    total_weight = 0.0
    weighted_score = 0.0
    for dim, result in dimension_results.items():
        weight = DIMENSION_WEIGHTS.get(dim, 1.0)
        total_weight += weight
        weighted_score += result.get("score", 0) * weight

    overall_score = int(weighted_score / total_weight) if total_weight > 0 else 100

    total_issues = sum(r.get("issue_count", 0) for r in dimension_results.values())
    critical_issues = sum(
        sum(1 for i in r.get("issues", []) if i.get("severity") == "critical")
        for r in dimension_results.values()
    )
    total_time_ms = (time.perf_counter() - t0) * 1000

    # 5. 生成摘要
    summary_lines = [
        f"=== 持续优化引擎 · {blueprint.project_name} ===",
        f"语言: {blueprint.language.primary} | 框架: {', '.join(blueprint.language.frameworks) or '无'}",
        f"总文件: {blueprint.language.total_files} | 启用维度: {len(dimensions)}",
        f"整体评分: {overall_score}/100",
        f"发现问题: {total_issues} 个（其中 critical: {critical_issues}）",
        f"扫描耗时: {total_time_ms:.0f}ms",
        "",
        "各维度详情:",
    ]
    for dim in DIMS.DIMENSION_ORDER:
        if dim in dimension_results:
            r = dimension_results[dim]
            name_cn = DIMS.DIMENSION_NAMES.get(dim, dim)
            icon = "🔴" if r["score"] < 60 else "🟡" if r["score"] < 80 else "🟢"
            summary_lines.append(
                f"  {icon} {name_cn}({dim}): {r['score']}/100 "
                f"— {r.get('issue_count', 0)} 个问题 {r.get('scan_time_ms', 0):.0f}ms"
            )

    return {
        "project": str(project_path),
        "project_name": blueprint.project_name,
        "language": blueprint.language.primary,
        "frameworks": blueprint.language.frameworks,
        "blueprint": blueprint,
        "dimensions": dimension_results,
        "overall_score": overall_score,
        "total_issues": total_issues,
        "critical_issues": critical_issues,
        "total_scan_time_ms": round(total_time_ms, 1),
        "at": timestamp,
        "summary": "\n".join(summary_lines),
    }


# ═══════════════════════════════════════════════════════════════════════
# 兼容入口（供 self_evolve_round.py 原有调用）
# ═══════════════════════════════════════════════════════════════════════

def optimize_project(project_path: str) -> dict:
    """run_full_pipeline 的别名（向后兼容原有 bug_report.py 等调用）。"""
    return run_full_pipeline(project_path)


def get_project_report(project_path: str) -> str:
    """返回人类可读的优化报告。"""
    result = run_full_pipeline(project_path)
    return result.get("summary", "")


# ═══════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(description="持续优化引擎 — 9 维度项目扫描")
    parser.add_argument("project", help="项目目录路径")
    parser.add_argument("--dimension", "-d", action="append",
                        choices=list(DIMENSION_SCANNERS.keys()),
                        help="只扫描指定维度（可多次指定）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示所有问题详情")
    args = parser.parse_args()

    result = run_full_pipeline(args.project, dimensions=args.dimension)

    if args.json:
        # 序列化时去掉不可 JSON 序列化的 blueprint
        output = {k: v for k, v in result.items() if k != "blueprint"}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(result["summary"])
        if args.verbose:
            print("\n详细问题列表:")
            for dim, r in result["dimensions"].items():
                name_cn = DIMS.DIMENSION_NAMES.get(dim, dim)
                print(f"\n--- {name_cn}({dim}) ---")
                for issue in r.get("issues", []):
                    sev = issue.get("severity", "?").upper()
                    location = f"{issue.get('file', '')}:{issue.get('line', 0)}"
                    print(f"  [{sev:8s}] {location} | {issue.get('type', '')}")
                    print(f"    → {issue.get('description', '')}")
                    print(f"    建议: {issue.get('suggestion', '')}")

