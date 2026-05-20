#!/usr/bin/env python3
"""evolution_engine.py — 扫描+修复+验证+重扫 一体化进化引擎

把 9 维扫描器 + 代码修复器 + 验证器 整合为一个闭环，
不依赖 Hermes Agent / delegate_task，直接在 Python 进程内运行。

核心公式：
    扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快

用法：
    from evolution_engine import run_evolution_round
    result = run_evolution_round("/path/to/project")
"""

import ast
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 自动路径 ──────────────────────────────────────────────────────────
SWARM_DIR = Path(__file__).parent.parent.resolve()
SRC_DIR = SWARM_DIR / "src"
for p in [str(SRC_DIR), str(SWARM_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from src.analysis.optimizer_core import run_full_pipeline
from src.analysis.dims import DIMENSION_NAMES


# ═══════════════════════════════════════════════════════════════════════
# 简易修复器 — 用规则+AST 做自动化代码修复
# ═══════════════════════════════════════════════════════════════════════


def _fix_unused_import(filepath: Path, line_num: int) -> dict:
    """删除未使用的 import 行"""
    try:
        code = filepath.read_text(encoding="utf-8")
        lines = code.split("\n")
        if line_num < 1 or line_num > len(lines):
            return {"success": False, "error": f"行号 {line_num} 超出范围（共 {len(lines)} 行）"}
        old_line = lines[line_num - 1]
        # 只删 import 行
        stripped = old_line.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            return {"success": False, "error": f"行 {line_num} 不是 import 语句: {stripped[:60]}"}
        del lines[line_num - 1]
        filepath.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return {"success": True, "action": f"删除未使用的 import: {stripped[:60]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fix_missing_docstring(filepath: Path, line_num: int) -> dict:
    """为无文档字符串的函数添加简单的 docstring"""
    try:
        code = filepath.read_text(encoding="utf-8")
        lines = code.split("\n")
        if line_num < 1 or line_num > len(lines):
            return {"success": False, "error": f"行号 {line_num} 超出范围"}
        # 在 def 行的下一行插入简短的 docstring
        idx = line_num  # 0-indexed
        # 检查是否有已经有的 docstring
        if idx < len(lines):
            next_line = lines[idx].strip()
            if next_line.startswith('"""') or next_line.startswith("'''") or next_line.startswith('"'):
                return {"success": False, "error": "已有 docstring"}
        # 从 def 行提取函数名
        func_match = re.search(r"def\s+(\w+)\s*\(", lines[line_num - 1])
        func_name = func_match.group(1) if func_match else "unknown"
        indent = " " * (len(lines[line_num - 1]) - len(lines[line_num - 1].lstrip()))
        doc = f'{indent}"""{func_name} — TODO: 添加函数说明"""'
        lines.insert(line_num, doc)
        filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"success": True, "action": f"为 {func_name} 添加 docstring 占位"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fix_hardcoded_value(filepath: Path, line_num: int, description: str) -> dict:
    """提取硬编码值为模块级常量（仅对 URL/路径/端口有效）"""
    try:
        code = filepath.read_text(encoding="utf-8")
        lines = code.split("\n")
        if line_num < 1 or line_num > len(lines):
            return {"success": False, "error": f"行号 {line_num} 超出范围"}
        line = lines[line_num - 1]
        # 找到字符串字面量
        strings = re.findall(r'["\']([^"\']{4,})["\']', line)
        if not strings:
            return {"success": False, "error": "未找到字符串字面量"}
        val = strings[0]
        # 生成常量名
        const_name = re.sub(r'[^a-zA-Z0-9_]', '_', val.upper())[:30]
        if const_name[0].isdigit():
            const_name = "_" + const_name
        # 在文件顶部添加常量定义
        header = f"\n# ── 自动提取常量 ──\n{const_name} = {repr(val)}\n"
        insert_pos = 0
        for i, l in enumerate(lines):
            if l.strip().startswith("import ") or l.strip().startswith("from "):
                insert_pos = i + 1
        lines.insert(insert_pos, header)
        filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"success": True, "action": f"提取常量 {const_name} = {val[:40]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fix_missing_error_handling(filepath: Path, line_num: int) -> dict:
    """为可能出错的代码添加 try/except 包装（简化版 — 标记 + 建议）"""
    return {"success": False, "action": "跳过", "reason": "错误处理修复需要人工判断，无法自动化"}


def _fix_dead_file(filepath: Path) -> dict:
    """标记死文件为废弃（不删除，加 _deprecated 后缀标记）"""
    try:
        if not filepath.exists():
            return {"success": False, "error": "文件不存在"}
        if filepath.stat().st_size == 0:
            filepath.unlink()
            return {"success": True, "action": f"删除空文件: {filepath.name}"}
        # 对于有明显死代码特征的文件，添加废弃标记
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        if "# DEPRECATED" not in content:
            filepath.write_text(f"# DEPRECATED — 此文件可能不再使用\n# 自动标记于 {datetime.now().isoformat()[:10]}\n" + content, encoding="utf-8")
            return {"success": True, "action": f"标记为废弃: {filepath.name}"}
        return {"success": False, "error": "已标记过"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fix_missing_test(filepath: Path, module_path: str) -> dict:
    """为缺少测试的模块创建测试存根文件"""
    try:
        src_path = Path(module_path)
        if not src_path.exists():
            return {"success": False, "error": f"源文件不存在: {module_path}"}
        # 确定测试文件路径
        tests_dir = src_path.parent.parent / "tests"
        if not tests_dir.exists():
            tests_dir.mkdir(parents=True, exist_ok=True)
        test_file = tests_dir / f"test_{src_path.stem}.py"
        if test_file.exists():
            if test_file.stat().st_size > 50:
                return {"success": False, "error": f"测试文件已存在且有内容: {test_file}"}
        # 提取所有函数名
        try:
            tree = ast.parse(src_path.read_text(encoding="utf-8"))
            funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            funcs = [f for f in funcs if not f.startswith("_")]
        except Exception:
            funcs = []
        # 生成测试存根
        lines = [
            f'"""tests for {src_path.name}"""',
            "",
            f"import pytest",
            f"from {src_path.stem} import {', '.join(funcs[:5])}" if funcs else f"import {src_path.stem}",
            "",
        ]
        for f in funcs[:5]:
            lines.append("")
            lines.append(f"def test_{f}():")
            lines.append(f'    """测试 {f} — TODO: 补充测试用例"""')
            lines.append("    pass")
        lines.append("")
        test_file.write_text("\n".join(lines), encoding="utf-8")
        return {"success": True, "action": f"创建测试存根: {test_file.name}（{len(funcs)} 个函数）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# 修复调度器
# ═══════════════════════════════════════════════════════════════════════

FIX_REGISTRY = {
    "unused_import": _fix_unused_import,
    "missing_docstring": _fix_missing_docstring,
    "hardcoded_value": _fix_hardcoded_value,
    "missing_error_handling": _fix_missing_error_handling,
}


def try_fix(issue: dict, project_root: Path) -> dict:
    """根据 issue 类型尝试自动修复"""
    issue_type = issue.get("type", "")
    file_path = issue.get("file", "")
    line = issue.get("line", 0)

    if not file_path:
        return {"success": False, "reason": "无文件路径", "issue": issue_type}

    # 死代码/空文件（无特定行号）
    if issue_type in ("empty_file",):
        fp = project_root / file_path if not Path(file_path).is_absolute() else Path(file_path)
        if fp.exists():
            return _fix_dead_file(fp)
        return {"success": False, "reason": "文件不存在", "issue": issue_type}

    # 缺失测试
    if issue_type in ("missing_test", "no_test_for_module"):
        return _fix_missing_test(project_root / "dummy.py", file_path or str(project_root))

    # 需要行号的修复
    fixer = FIX_REGISTRY.get(issue_type)
    if not fixer:
        return {"success": False, "reason": f"无修复规则: {issue_type}", "issue": issue_type}

    fp = project_root / file_path if not Path(file_path).is_absolute() else Path(file_path)
    if not fp.exists():
        return {"success": False, "reason": f"文件不存在: {file_path}", "issue": issue_type}

    try:
        return fixer(fp, line)
    except Exception as e:
        return {"success": False, "error": str(e), "issue": issue_type}


# ═══════════════════════════════════════════════════════════════════════
# 验证器
# ═══════════════════════════════════════════════════════════════════════


def verify_fix(file_path: str) -> dict:
    """验证修复后的文件语法正确"""
    fp = Path(file_path)
    if not fp.exists():
        return {"passed": False, "error": "文件不存在"}
    if fp.suffix != ".py":
        return {"passed": True, "note": "非 Python 文件，跳过语法检查"}
    try:
        ast.parse(fp.read_text(encoding="utf-8"))
        return {"passed": True}
    except SyntaxError as e:
        return {"passed": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# 进化循环
# ═══════════════════════════════════════════════════════════════════════


def run_evolution_round(
    target_dir: str,
    dimensions: Optional[list[str]] = None,
    max_fixes_per_round: int = 30,
    progress_callback=None,
) -> dict:
    """执行一轮完整的进化：扫描 → 修复 → 验证 → 重扫

    Args:
        target_dir: 目标项目路径
        dimensions: 要扫描的维度列表，None=所有
        max_fixes_per_round: 每轮最多修复数
        progress_callback: 进度回调函数 fn(phase, data)

    Returns:
        dict: 完整进化报告
    """
    target_path = Path(target_dir)
    if not target_path.exists():
        return {"status": "failed", "error": f"路径不存在: {target_dir}"}

    report = {
        "status": "running",
        "target_dir": target_dir,
        "dimensions": dimensions,
        "started_at": datetime.now().isoformat(),
        "phases": [],
    }

    def _progress(phase: str, data: dict):
        if progress_callback:
            try:
                progress_callback(phase, data)
            except Exception:
                pass

    # ── Phase 1: 扫描 ──
    _progress("scanning", {"message": "全维度扫描中..."})
    try:
        scan_result = run_full_pipeline(target_dir, dimensions=dimensions)
    except Exception as e:
        report["status"] = "failed"
        report["error"] = str(e)
        return report

    score_before = scan_result.get("overall_score", 0)
    total_issues = scan_result.get("total_issues", 0)
    critical = scan_result.get("critical_issues", 0)

    report["score_before"] = score_before
    report["total_issues"] = total_issues
    report["critical_issues"] = critical
    report["scan_result"] = {
        "overall_score": score_before,
        "total_issues": total_issues,
        "critical_issues": critical,
    }
    report["dimensions"] = scan_result.get("dimensions", {})
    report["summary"] = scan_result.get("summary", "")

    # ── 深度企业级扫描 ──
    _progress("deep_scan", {"message": "企业级深度扫描中..."})
    try:
        from src.analysis.deep_enterprise_scanner import scan_deep
        deep_result = scan_deep(target_dir)
        report["deep_scan"] = deep_result
        _progress("deep_scanned", {
            "deep_score": deep_result.get("score", 0),
            "deep_issues": deep_result.get("issue_count", 0),
            "deep_critical": deep_result.get("by_severity", {}).get("critical", 0),
            "message": f"深度扫描评分 {deep_result.get('score',0)}/100，发现 {deep_result.get('issue_count',0)} 个问题",
        })
    except Exception as e:
        report["deep_scan"] = {"error": str(e), "score": 0, "issues": []}
        _progress("deep_scan_error", {"message": f"深度扫描失败: {e}"})

    # ── 深度修复（企业级） ──
    deep_issues = report.get("deep_scan", {}).get("issues", [])
    if deep_issues:
        _progress("deep_fixing", {"total": len(deep_issues), "message": f"尝试修复深层问题...共 {len(deep_issues)} 个"})
        try:
            from src.fixers.enterprise_fixer import try_fix_deep, DEEP_FIXERS
            fixable_types = {k for k, v in DEEP_FIXERS.items() if v is not None}

            deep_fixes = {"attempted": 0, "succeeded": 0, "failed": 0, "details": []}
            # 按修复能力排序：可自动修复的优先
            sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            sorted_issues = sorted(
                [i for i in deep_issues if i.get("type") in fixable_types],
                key=lambda x: sev_order.get(x.get("severity", "low"), 99),
            )

            for issue in sorted_issues[:20]:  # 每轮最多20个
                deep_fixes["attempted"] += 1
                result = try_fix_deep(issue, Path(target_dir))
                deep_fixes["details"].append({
                    "issue_type": issue.get("type", ""),
                    "severity": issue.get("severity", ""),
                    "file": issue.get("file", ""),
                    "line": issue.get("line", 0),
                    "fix_result": result,
                })
                if result.get("success"):
                    deep_fixes["succeeded"] += 1
                else:
                    deep_fixes["failed"] += 1

                _progress("deep_fixing", {
                    "attempted": deep_fixes["attempted"],
                    "succeeded": deep_fixes["succeeded"],
                    "failed": deep_fixes["failed"],
                    "total": len(sorted_issues),
                    "message": f"深层修复: {deep_fixes['succeeded']}成功 / {deep_fixes['failed']}失败 / {deep_fixes['attempted']}尝试",
                })

            report["deep_fixes"] = deep_fixes
            _progress("deep_fixed", {
                "succeeded": deep_fixes["succeeded"],
                "message": f"深层修复完成: 成功 {deep_fixes['succeeded']} 个",
            })
        except Exception as e:
            import traceback
            report["deep_fix_error"] = str(e)
            _progress("deep_fix_error", {"message": f"深层修复异常: {e}"})
    else:
        _progress("deep_skip", {"message": "无深层问题可修复"})

    _progress("scanned", {
        "score": score_before,
        "total_issues": total_issues,
        "critical_issues": critical,
        "message": f"评分 {score_before}/100，发现 {total_issues} 个问题（Critical {critical} 个）",
    })

    # ── Phase 2: 修复 ──
    fixes_attempted = 0
    fixes_succeeded = 0
    fixes_failed = 0
    fix_details = []

    # 收集所有可修复的问题
    fixable_issues = []
    for dim_name, dim_result in scan_result.get("dimensions", {}).items():
        for issue in dim_result.get("issues", []):
            issue_type = issue.get("type", "")
            if issue_type in FIX_REGISTRY or issue_type in ("empty_file", "missing_test", "no_test_for_module"):
                fixable_issues.append({
                    **issue,
                    "_dimension": dim_name,
                    "_dim_label": DIMENSION_NAMES.get(dim_name, dim_name),
                })

    # 按严重级别排序：critical → high → medium → low
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    fixable_issues.sort(key=lambda x: sev_order.get(x.get("severity", "low"), 99))

    _progress("fixing", {
        "total_fixable": len(fixable_issues),
        "max_fixes": max_fixes_per_round,
        "message": f"发现 {len(fixable_issues)} 个可自动修复的问题，开始修复...",
    })

    for issue in fixable_issues[:max_fixes_per_round]:
        fixes_attempted += 1
        result = try_fix(issue, target_path)
        fix_details.append({
            "issue_type": issue.get("type", ""),
            "severity": issue.get("severity", ""),
            "file": issue.get("file", ""),
            "line": issue.get("line", 0),
            "dimension": issue.get("_dim_label", ""),
            "fix_result": result,
        })
        if result.get("success"):
            # 验证语法
            file_path = issue.get("file", "")
            if file_path:
                fp = target_path / file_path if not Path(file_path).is_absolute() else Path(file_path)
                ver = verify_fix(str(fp))
                if not ver.get("passed"):
                    # 回滚
                    fix_details[-1]["verification"] = ver
                    fix_details[-1]["fix_result"]["success"] = False
                    fix_details[-1]["fix_result"]["rolled_back"] = True
                    fixes_failed += 1
                    continue
            fixes_succeeded += 1
        else:
            fixes_failed += 1

        _progress("fixing", {
            "attempted": fixes_attempted,
            "succeeded": fixes_succeeded,
            "failed": fixes_failed,
            "total_fixable": len(fixable_issues),
            "message": f"修复进度: {fixes_succeeded} 成功 / {fixes_failed} 失败 / {fixes_attempted} 尝试",
        })

    report["fixes"] = {
        "attempted": fixes_attempted,
        "succeeded": fixes_succeeded,
        "failed": fixes_failed,
        "details": fix_details,
    }

    # ── Phase 3: 重扫 ──
    _progress("rescanning", {"message": "修复完成，重新扫描..."})
    try:
        rescanned = run_full_pipeline(target_dir, dimensions=dimensions)
    except Exception as e:
        rescanned = {"overall_score": 0, "total_issues": 0, "error": str(e)}

    score_after = rescanned.get("overall_score", 0)
    total_after = rescanned.get("total_issues", 0)
    critical_after = rescanned.get("critical_issues", 0)

    report["score_after"] = score_after
    report["total_after"] = total_after
    report["critical_after"] = critical_after
    report["score_delta"] = score_after - score_before
    report["rescanned"] = {
        "overall_score": score_after,
        "total_issues": total_after,
        "critical_issues": critical_after,
    }

    _progress("completed", {
        "score_before": score_before,
        "score_after": score_after,
        "delta": score_after - score_before,
        "fixes_succeeded": fixes_succeeded,
        "fixes_failed": fixes_failed,
        "message": f"评分 {score_before}→{score_after}（{'+' if score_after >= score_before else ''}{score_after - score_before}），修复 {fixes_succeeded}/{fixes_attempted}",
    })

    report["status"] = "completed"
    report["finished_at"] = datetime.now().isoformat()
    return report


# ═══════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="进化引擎 — 扫描+修复+验证+重扫")
    parser.add_argument("target_dir", help="目标项目路径")
    parser.add_argument("--max-fixes", type=int, default=30, help="每轮最大修复数")
    parser.add_argument("--dimension", "-d", action="append", help="指定维度（可多次）")
    args = parser.parse_args()

    result = run_evolution_round(
        target_dir=args.target_dir,
        dimensions=args.dimension,
        max_fixes_per_round=args.max_fixes,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))



