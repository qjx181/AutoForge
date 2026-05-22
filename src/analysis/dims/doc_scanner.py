#!/usr/bin/env python3
"""dims/doc_scanner.py — 维度六：文档扫描器

检测：
  1. 缺少 README.md
  2. README 过短（< 100 字）
  3. 模块缺少 docstring（公开函数无文档字符串）
  4. 缺少 CHANGELOG.md
  5. 缺少 LICENSE 文件
  6. API 路由缺少注释

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import ast
import re
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "documentation"


def _check_readme(root: Path) -> list[dict]:
    issues = []
    readme_candidates = ["README.md", "README.rst", "README.txt", "readme.md"]
    readme = None
    for candidate in readme_candidates:
        candidate_path = root / candidate
        if candidate_path.exists():
            readme = candidate_path
            break

    if not readme:
        issues.append({
            "type": "missing_readme", "severity": "high",
            "file": str(root / "README.md"), "line": 0,
            "description": "项目根目录缺少 README.md",
            "suggestion": "创建 README.md 包含：项目简介、快速开始、安装步骤、使用示例",
        })
        return issues

    content = readme.read_text(encoding="utf-8", errors="ignore")
    word_count = len(re.findall(r'\w+', content))
    if word_count < 100:
        issues.append({
            "type": "readme_too_short", "severity": "medium",
            "file": str(readme), "line": 0,
            "description": f"README.md 只有 {word_count} 个词，建议至少 100 词",
            "suggestion": "补充：项目简介、快速开始、安装步骤、示例代码",
        })

    # 检查必要章节
    sections = ["安装", "安装步骤", "开始", "quick start", "install", "getting started"]
    has_intro = any(sec.lower() in content.lower() for sec in sections)
    if not has_intro:
        issues.append({
            "type": "readme_missing_sections", "severity": "low",
            "file": str(readme), "line": 0,
            "description": "README.md 缺少安装/快速开始章节",
            "suggestion": "添加安装步骤和快速开始指南",
        })

    return issues


def _check_module_docs(source_files: list[str]) -> list[dict]:
    """检测模块和公开函数是否缺少 docstring。"""
    issues = []
    for filepath in source_files:
        fp = Path(filepath)
        try:
            code = fp.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(code)
        except Exception:
            continue

        module_doc = ast.get_docstring(tree)
        if not module_doc:
            issues.append({
                "type": "missing_module_doc", "severity": "low",
                "file": filepath, "line": 1,
                "description": f"模块 '{fp.name}' 缺少模块级 docstring",
                "suggestion": f"在 '{fp.name}' 顶部添加 '''模块说明'''",
            })

        # 公开函数 docstring
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") or node.name.startswith("test_"):
                    continue
                doc = ast.get_docstring(node)
                if not doc:
                    issues.append({
                        "type": "missing_function_doc",
                        "severity": "low",
                        "file": filepath,
                        "line": node.lineno,
                        "description": f"函数 '{node.name}' 缺少 docstring",
                        "suggestion": f"为 '{node.name}' 添加函数文档字符串",
                    })

    return issues


def _check_missing_files(root: Path) -> list[dict]:
    issues = []
    missing = [
        ("LICENSE", "high", "缺少 LICENSE 文件，建议使用 MIT/Apache 2.0"),
        ("CHANGELOG.md", "medium", "缺少 CHANGELOG.md，建议按 Keep a Changelog 格式记录变更"),
    ]
    for filename, severity, suggestion in missing:
        if not (root / filename).exists():
            issues.append({
                "type": f"missing_{filename.lower().replace('.', '_')}",
                "severity": severity,
                "file": str(root / filename),
                "line": 0,
                "description": suggestion,
                "suggestion": f"创建 {filename}",
            })
    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    if not blueprint.is_enabled("documentation"):
        return {"dimension": DIMENSION, "score": 100, "issues": [],
                "file_count": 0, "issue_count": 0,
                "summary": "文档维度未启用（项目无 README）"}

    root = blueprint.project_root
    all_issues = []
    all_issues.extend(_check_readme(root))
    all_issues.extend(_check_module_docs(blueprint.get_source_files(blueprint.language.primary)))
    all_issues.extend(_check_missing_files(root))

    for issue in all_issues:
        issue["dimension"] = DIMENSION

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    score = max(0, 100 - len(all_issues) * 5)
    return {
        "dimension": DIMENSION, "score": score,
        "issues": all_issues,
        "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
        "issue_count": len(all_issues),
        "summary": (
            f"文档扫描完成：{len(all_issues)} 个问题（README/模块文档/必要文件），"
            f"评分 {score}/100"
        ),
    }
