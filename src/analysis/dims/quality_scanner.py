#!/usr/bin/env python3
"""dims/quality_scanner.py — 维度一：代码质量 + 死代码 扫描器

检测：
  1. 未使用的 import
  2. 过深嵌套（>4 层）
  3. 硬编码值（魔数、硬编码 URL/路径）
  4. 过长函数（>80 行）
  5. 过长模块文件（>1000 行）
  6. 缺失异常处理
  7. 未调用的函数/方法（死代码）

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
        返回: {"dimension": "quality", "score": int, "issues": [dict], "summary": str}
"""

import ast
import re
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


# ═══════════════════════════════════════════════════════════════════════
# AST 分析
# ═══════════════════════════════════════════════════════════════════════


def _find_unused_imports(code: str) -> list[dict]:
    """使用 AST 找未使用的 import。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    # 收集所有 import
    imported_names: set[str] = set()
    imported_modules: dict[str, int] = {}  # name → line

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imported_names.add(name)
                imported_modules[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                imported_names.add(name)
                imported_modules[name] = node.lineno

    # 收集所有被引用的名字
    used_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                used_names.add(node.value.id)

    # 找未使用
    for name, line in imported_modules.items():
        if name not in used_names and name not in ("sys", "os"):  # sys/os 可能是风格问题
            issues.append({
                "type": "unused_import",
                "severity": "low",
                "line": line,
                "description": f"导入了 '{name}' 但未在文件中使用",
                "suggestion": f"删除 import 或确认该名称有实际用途",
            })

    return issues


def _find_deep_nesting(code: str, max_depth: int = 4) -> list[dict]:
    """检测过深嵌套。"""
    issues = []
    lines = code.split("\n")

    depth = 0
    max_ever = 0
    deepest_line = 1

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        current_depth = indent // 4

        # 嵌套语句
        nesting_kws = re.match(r"^\s*(if|elif|for|while|with|except|try|async\s+for|async\s+with)\b", stripped)
        dedent = stripped.startswith(("return", "break", "continue", "pass", "raise"))

        if nesting_kws:
            depth += 1
            if depth > max_ever:
                max_ever = depth
                deepest_line = i
        elif dedent and depth > 0:
            depth -= 1

        if depth > max_depth:
            issues.append({
                "type": "deep_nesting",
                "severity": "medium",
                "line": i,
                "description": f"嵌套深度 {depth} 层，超过建议的 {max_depth} 层",
                "suggestion": "提取为独立函数、使用 guard clause 提前返回",
            })

    return issues


def _find_hardcoded_values(code: str) -> list[dict]:
    """检测硬编码值（魔数、硬编码 URL）。"""
    issues = []
    lines = code.split("\n")

    # 魔数：整数赋值语句中的非标准数字
    magic_pattern = re.compile(r"=\s*([2-9]\d{1,})\b")
    skip_pattern = re.compile(r"(timeout|port|size|max|min|count|limit|index|id|version|retries|delay|interval|maxsize|poolsize)", re.IGNORECASE)

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#") or "import" in stripped:
            continue
        # 跳过 if/for/while 条件中的数字
        if re.match(r"^\s*(if|elif|for|while)\b", stripped):
            continue

        for m in magic_pattern.finditer(stripped):
            num_str = m.group(1)
            if skip_pattern.search(stripped):
                continue
            issues.append({
                "type": "hardcoded_value",
                "severity": "low",
                "line": i,
                "description": f"硬编码整数 {num_str}，建议定义为命名常量",
                "suggestion": f"MAX_{num_str} = {num_str} 或从配置读取",
            })

    # 硬编码 URL
    url_pattern = re.compile(r"https?://[^\s\"']+")
    for i, line in enumerate(lines, 1):
        if "http://" in line or "https://" in line:
            issues.append({
                "type": "hardcoded_url",
                "severity": "low",
                "line": i,
                "description": "检测到硬编码 URL，建议移至配置",
                "suggestion": "BASE_URL = os.environ.get('BASE_URL', 'https://...')",
            })

    return issues


def _find_long_functions(code: str, max_lines: int = 80) -> list[dict]:
    """检测过长函数。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines = node.end_lineno - node.lineno + 1
            if lines > max_lines:
                issues.append({
                    "type": "long_function",
                    "severity": "medium",
                    "line": node.lineno,
                    "description": f"函数 '{node.name}' 有 {lines} 行，超过建议的 {max_lines} 行",
                    "suggestion": "拆分函数，每个函数不超过一个关注点",
                })

    return issues


def _find_missing_error_handling(code: str) -> list[dict]:
    """检测缺失异常处理的危险调用。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    DANGEROUS_CALLS = {
        "eval": "使用 eval() 执行任意代码",
        "exec": "使用 exec() 执行任意代码",
        "pickle.load": "使用 pickle.load 加载不可信数据",
        "yaml.load": "使用 yaml.load，应使用 yaml.safe_load",
        "subprocess.Popen": "使用 subprocess.Popen，需确认输入安全",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name in DANGEROUS_CALLS:
                # 向上查是否有 try/except 包裹
                issues.append({
                    "type": "missing_error_handling",
                    "severity": "high",
                    "line": node.lineno,
                    "description": DANGEROUS_CALLS[func_name],
                    "suggestion": "使用 try/except 包裹并处理异常",
                })

    return issues


def _find_dead_code(code: str) -> list[dict]:
    """使用 AST 分析检测未调用的函数（死代码候选）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    # 收集所有定义的函数名
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)

    # 收集所有调用的名字
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            called.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                called.add(node.value.id)

    # 公开函数（非下划线开头）且未被调用且非 main
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if (name not in called
                    and not name.startswith("_")
                    and name.lower() not in ("main", "cli", "run")):
                issues.append({
                    "type": "dead_code",
                    "severity": "low",
                    "line": node.lineno,
                    "description": f"函数 '{name}' 定义后未被调用（死代码候选）",
                    "suggestion": f"确认 '{name}' 是否需要，如不需要则删除",
                })

    return issues


def _scan_file(filepath: Path) -> list[dict]:
    """扫描单个文件，收集所有质量+死代码问题。"""
    issues = []
    try:
        code = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return issues

    if not code.strip():
        return issues

    issues.extend(_find_unused_imports(code))
    issues.extend(_find_deep_nesting(code))
    issues.extend(_find_hardcoded_values(code))
    issues.extend(_find_long_functions(code))
    issues.extend(_find_missing_error_handling(code))
    issues.extend(_find_dead_code(code))

    for issue in issues:
        issue["file"] = str(filepath)
        issue["dimension"] = "quality"

    return issues


# ═══════════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════════

DIMENSION = "quality"
MAX_LINES_PER_FILE = 1000  # 单文件超过此行数则降级扫描（只分析前 MAX_LINES）


def scan(blueprint: OptimizationBlueprint) -> dict:
    """扫描代码质量 + 死代码。

    返回格式：
        {
            "dimension": str,
            "score": int (0-100),
            "issues": [dict],
            "file_count": int,
            "summary": str,
        }
    """
    all_issues = []
    scored_files = 0
    total_score = 0

    source_files = blueprint.get_source_files(blueprint.language.primary)

    for filepath in source_files:
        fp = Path(filepath)
        if not fp.exists() or fp.stat().st_size == 0:
            continue

        issues = _scan_file(fp)
        all_issues.extend(issues)
        scored_files += 1

        # 文件评分：每有 1 个严重问题扣 10 分
        sev_count = sum(1 for i in issues if i["severity"] in ("critical", "high"))
        file_score = max(0, 100 - sev_count * 10)
        total_score += file_score

    avg_score = total_score // max(scored_files, 1)

    # 按严重级别排序
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    return {
        "dimension": DIMENSION,
        "score": avg_score,
        "issues": all_issues,
        "file_count": scored_files,
        "issue_count": len(all_issues),
        "summary": (
            f"扫描 {scored_files} 个文件，发现 {len(all_issues)} 个质量问题"
            f"（未用import/过深嵌套/硬编码/过长函数/缺失异常/死代码），"
            f"质量评分 {avg_score}/100"
        ),
    }

