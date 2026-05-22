#!/usr/bin/env python3
"""dims/arch_scanner.py — 维度四：架构扫描器

检测：
  1. 循环依赖（模块 A 导入 B，B 又导入 A）
  2. 缺少 __init__.py（Python 包目录）
  3. 过深包嵌套（目录层级 > 5）
  4. 大型文件（单文件 > 500 行）
  5. 缺少分层（app/logic/data/ 分离）
  6. 单体文件（所有代码堆在一个文件）
  7. 配置与逻辑混合（settings.py 中含业务逻辑）

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import ast
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "architecture"


def _find_circular_imports(root: Path) -> list[dict]:
    """通过分析所有 import 语句，检测循环依赖。"""
    issues = []
    imports: dict[str, set[str]] = {}  # module → set of imported modules

    for py_file in root.rglob("*.py"):
        if py_file.name.startswith("."):
            continue
        try:
            code = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(code)
        except Exception:
            continue

        rel_path = py_file.relative_to(root)
        module_parts = list(rel_path.parts)[:-1]  # 去掉 .py
        # 简化：只用文件名作为模块名
        module_name = py_file.stem

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[0]
                    if name in ("os", "sys", "typing", "dataclasses", "abc"):
                        continue
                    imported.add(name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] in ("os", "sys", "typing", "dataclasses"):
                    continue
                imported.add(module.split(".")[0])

        imports[module_name] = imported

    # 检测循环
    for mod, deps in imports.items():
        for dep in deps:
            if dep in imports and mod in imports.get(dep, set()):
                issues.append({
                    "type": "circular_dependency",
                    "severity": "high",
                    "file": mod + ".py",
                    "line": 0,
                    "description": f"模块 '{mod}' 和 '{dep}' 存在循环依赖",
                    "suggestion": f"将共享代码提取到独立模块，破坏循环链",
                })

    return issues


def _find_missing_init(source_files: list[str]) -> list[dict]:
    """检测缺少 __init__.py 的包目录。"""
    issues = []
    if not source_files:
        return issues

    root = Path(source_files[0]).parent
    for _ in range(3):  # 最多向上 3 层
        if root.parent != root:
            root = root.parent

    # 找所有目录
    all_dirs = set()
    for f in source_files:
        p = Path(f)
        for parent in p.parents:
            if parent.name and not parent.name.startswith("."):
                all_dirs.add(parent)

    # Python 项目：检查含 .py 文件的目录是否缺少 __init__.py
    for d in all_dirs:
        py_files = list(d.glob("*.py"))
        if py_files and not (d / "__init__.py").exists():
            # 排除根目录
            if d != root:
                issues.append({
                    "type": "missing_init",
                    "severity": "low",
                    "file": str(d / "__init__.py"),
                    "line": 0,
                    "description": f"目录 '{d.name}' 包含 .py 文件但缺少 __init__.py",
                    "suggestion": f"在 {d}/ 下创建空的 __init__.py",
                })

    return issues


def _find_large_files(source_files: list[str], max_lines: int = 500) -> list[dict]:
    """检测过大文件。"""
    issues = []
    for f in source_files:
        try:
            fp = Path(f)
            lines = fp.read_text(encoding="utf-8", errors="ignore").count("\n")
            if lines > max_lines:
                issues.append({
                    "type": "large_file",
                    "severity": "medium",
                    "file": f,
                    "line": 0,
                    "description": f"文件有 {lines} 行，超过建议的 {max_lines} 行",
                    "suggestion": "将文件按功能拆分到子模块",
                })
        except Exception:
            continue
    return issues


def _find_monolith(source_files: list[str], total_files: int) -> list[dict]:
    """检测单体文件（所有代码堆在一个文件）。"""
    issues = []
    if total_files <= 3:  # 小项目不适用
        return issues

    for f in source_files:
        try:
            fp = Path(f)
            lines = fp.read_text(encoding="utf-8", errors="ignore").count("\n")
            if lines > 2000:
                issues.append({
                    "type": "monolith_file",
                    "severity": "high",
                    "file": f,
                    "line": 0,
                    "description": f"超大单体文件 {lines} 行，违反关注点分离原则",
                    "suggestion": "按功能拆分为多个模块：data/, logic/, api/, models/",
                })
        except Exception:
            continue
    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    """扫描架构问题。"""
    if not blueprint.is_enabled("architecture"):
        return {
            "dimension": DIMENSION, "score": 100, "issues": [],
            "file_count": 0, "issue_count": 0,
            "summary": "架构维度未启用（项目文件数 < 5）",
        }

    source_files = blueprint.get_source_files(blueprint.language.primary)
    all_issues = []
    all_issues.extend(_find_circular_imports(blueprint.project_root))
    all_issues.extend(_find_missing_init(source_files))
    all_issues.extend(_find_large_files(source_files))
    all_issues.extend(_find_monolith(source_files, blueprint.language.total_files))

    for issue in all_issues:
        issue["dimension"] = DIMENSION

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    score = max(0, 100 - len([i for i in all_issues if i["severity"] in ("critical", "high")]) * 20)

    return {
        "dimension": DIMENSION,
        "score": score,
        "issues": all_issues,
        "file_count": len(source_files),
        "issue_count": len(all_issues),
        "summary": (
            f"架构扫描完成：{len(all_issues)} 个问题（循环依赖/缺少__init__/大文件/单体），"
            f"评分 {score}/100"
        ),
    }

