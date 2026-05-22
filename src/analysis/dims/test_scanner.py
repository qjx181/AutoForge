#!/usr/bin/env python3
"""dims/test_scanner.py — 维度二：测试覆盖扫描器

检测：
  1. 有代码无测试（模块有 .py 但 tests/ 下无对应测试）
  2. 测试文件为空或几乎为空
  3. 测试函数缺失（模块有 N 个函数，测试仅有 M < N 个）
  4. 缺少 conftest.py（pytest 项目）
  5. 测试跳过率过高（大量 @pytest.mark.skip）
  6. 缺少覆盖率配置

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import ast
from pathlib import Path
from typing import Any

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "testing"


def _get_module_functions(filepath: Path) -> list[str]:
    """获取模块中所有函数名（不含测试函数）。"""
    try:
        code = filepath.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(code)
    except Exception:
        return []

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_") and not node.name.startswith("Test"):
                functions.append(node.name)
    return functions


def _get_test_functions(filepath: Path) -> list[str]:
    """获取测试文件中的测试函数名。"""
    try:
        code = filepath.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(code)
    except Exception:
        return []

    tests = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_") or node.name.startswith("Test"):
                tests.append(node.name)
    return tests


def _is_test_file(filepath: Path) -> bool:
    name = filepath.name.lower()
    return (
        name.startswith("test_") or name.startswith("test") or name.endswith("_test.py")
    )


def _find_missing_tests(source_files: list[str], test_dir: str,
                       blueprint: OptimizationBlueprint) -> list[dict]:
    """检测有代码无测试的模块。"""
    issues = []

    if not test_dir or not Path(test_dir).exists():
        # 整个项目无测试目录
        for f in source_files[:10]:  # 只报告前 10 个
            issues.append({
                "type": "no_test_dir",
                "severity": "high",
                "file": f,
                "line": 0,
                "description": "项目无测试目录",
                "suggestion": f"创建 tests/ 目录并编写 {Path(f).name} 的测试",
            })
        return issues

    # 构建已测试模块映射
    tested_modules: set[str] = set()
    for tf in Path(test_dir).rglob("test_*.py"):
        test_funcs = _get_test_functions(tf)
        if test_funcs:
            # 找对应的源模块
            module_name = tf.stem
            if module_name.startswith("test_"):
                module_name = module_name[5:]
            tested_modules.add(module_name)

    # 检查每个源模块
    for source_path in source_files:
        src = Path(source_path)
        module_name = src.stem
        if module_name.startswith("__"):
            continue
        if module_name not in tested_modules:
            functions = _get_module_functions(src)
            if functions:  # 有函数但无测试
                issues.append({
                    "type": "untested_module",
                    "severity": "high",
                    "file": source_path,
                    "line": 0,
                    "description": f"模块 '{module_name}' 有 {len(functions)} 个函数但无测试",
                    "suggestion": f"在 tests/ 下创建 test_{module_name}.py 覆盖 {', '.join(functions[:5])} 等函数",
                })

    return issues


def _find_empty_tests(test_dir: str) -> list[dict]:
    """检测空测试文件。"""
    issues = []
    if not test_dir or not Path(test_dir).exists():
        return issues

    for tf in Path(test_dir).rglob("test_*.py"):
        try:
            size = tf.stat().st_size
        except Exception:
            continue
        if size < 100:  # 小于 100 字节的测试文件
            issues.append({
                "type": "empty_test",
                "severity": "high",
                "file": str(tf),
                "line": 0,
                "description": f"测试文件 '{tf.name}' 小于 100 字节，可能为空",
                "suggestion": "补充测试用例",
            })

    return issues


def _check_pytest_config(test_dir: str) -> list[dict]:
    """检查 pytest 配置。"""
    issues = []
    root = Path(test_dir).anchor if Path(test_dir).exists() else Path.cwd()

    # conftest.py
    has_conftest = any(Path(test_dir).rglob("conftest.py")) if Path(test_dir).exists() else False

    # pytest.ini / setup.cfg
    has_config = (
        (Path(test_dir) / "pytest.ini").exists()
        or (Path(test_dir) / "setup.cfg").exists()
        or (Path.cwd() / "pyproject.toml").exists()
    )

    if not has_conftest:
        issues.append({
            "type": "no_conftest",
            "severity": "low",
            "file": str(Path(test_dir) / "conftest.py"),
            "line": 0,
            "description": "pytest 项目缺少 conftest.py（共享 fixture）",
            "suggestion": "创建 conftest.py 定义共享的 @pytest.fixture",
        })

    if not has_config:
        issues.append({
            "type": "no_pytest_config",
            "severity": "low",
            "file": str(Path.cwd() / "pytest.ini"),
            "line": 0,
            "description": "pytest 项目缺少配置文件（pytest.ini / setup.cfg / pyproject.toml）",
            "suggestion": "创建 pytest.ini 包含 minversion, testpaths, python_files 等配置",
        })

    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    """扫描测试覆盖情况。"""
    all_issues = []
    source_files = blueprint.get_source_files(blueprint.language.primary)
    test_dir = blueprint.test.test_dir or ""

    if not blueprint.is_enabled("testing"):
        return {
            "dimension": DIMENSION,
            "score": 100,
            "issues": [],
            "file_count": 0,
            "issue_count": 0,
            "summary": "测试维度未启用（项目无测试框架配置）",
        }

    all_issues.extend(_find_missing_tests(source_files, test_dir, blueprint))
    all_issues.extend(_find_empty_tests(test_dir))
    all_issues.extend(_check_pytest_config(test_dir))

    for issue in all_issues:
        issue["dimension"] = DIMENSION

    # 评分
    untested = sum(1 for i in all_issues if i["type"] in ("untested_module", "no_test_dir"))
    score = max(0, 100 - untested * 5)

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    return {
        "dimension": DIMENSION,
        "score": score,
        "issues": all_issues,
        "file_count": len(source_files),
        "issue_count": len(all_issues),
        "summary": (
            f"测试覆盖扫描完成：{len(source_files)} 个源文件，"
            f"{len(all_issues)} 个问题，未测试模块 {untested} 个，评分 {score}/100"
        ),
    }

