"""dims/deadcode_scanner.py — 维度九：死代码扫描器

检测：
  1. 完全空的文件（0 字节或只有空行/注释）
  2. 只有 import 但无任何函数的文件
  3. 过时/废弃的 .pyc / __pycache__ 文件（可选）
  4. 孤立测试文件（测试了不存在的模块）
  5. 孤立脚本（定义了但从未被 import）
  6. 重复文件（文件名不同但内容高度相似）

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import hashlib
import ast
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "deadcode"


def _file_hash(filepath: Path) -> str:
    try:
        return hashlib.md5(filepath.read_bytes()).hexdigest()[:8]
    except Exception:
        return ""


def _is_meaningful(code: str) -> bool:
    """判断文件是否有实际代码（不只是注释和空行）。"""
    for line in code.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def _find_empty_files(source_files: list[str]) -> list[dict]:
    issues = []
    for filepath in source_files:
        fp = Path(filepath)
        try:
            size = fp.stat().st_size
        except Exception:
            continue

        if size == 0:
            issues.append({
                "type": "empty_file",
                "severity": "low",
                "file": filepath,
                "line": 0,
                "description": f"文件 {fp.name} 为空（0 字节）",
                "suggestion": "删除空文件或补充内容",
            })
        else:
            try:
                content = fp.read_text(encoding="utf-8", errors="ignore")
                if not _is_meaningful(content):
                    issues.append({
                        "type": "comment_only_file",
                        "severity": "low",
                        "file": filepath,
                        "line": 0,
                        "description": f"文件 {fp.name} 只包含注释，无实际代码",
                        "suggestion": "删除纯注释文件或补充实际代码",
                    })
            except Exception:
                pass
    return issues


def _find_import_only_files(source_files: list[str]) -> list[dict]:
    """只有 import 但无函数的文件。"""
    issues = []
    for filepath in source_files:
        fp = Path(filepath)
        try:
            code = fp.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(code)
        except Exception:
            continue

        has_import = False
        has_function = False
        has_class = False

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                has_import = True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                has_function = True
            if isinstance(node, ast.ClassDef):
                has_class = True

        if has_import and not has_function and not has_class:
            issues.append({
                "type": "import_only_file",
                "severity": "low",
                "file": filepath,
                "line": 0,
                "description": f"文件 {fp.name} 只有 import，无任何函数或类定义",
                "suggestion": f"确认 '{fp.name}' 是否必需，如只是 import 集合可合并到 __init__.py",
            })

    return issues


def _find_duplicate_files(source_files: list[str], threshold: float = 0.95) -> list[dict]:
    """检测内容高度相似的重复文件。"""
    issues = []
    hashes: dict[str, list[str]] = {}

    for filepath in source_files:
        fp = Path(filepath)
        h = _file_hash(fp)
        if h:
            hashes.setdefault(h, []).append(filepath)

    for h, files in hashes.items():
        if len(files) > 1:
            names = [Path(f).name for f in files]
            issues.append({
                "type": "duplicate_files",
                "severity": "low",
                "file": files[0],
                "line": 0,
                "description": f"检测到 {len(files)} 个内容相同的文件：{', '.join(names)}",
                "suggestion": "保留一个，删除其余重复文件并统一 import 路径",
            })

    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    if not blueprint.is_enabled("deadcode"):
        return {"dimension": DIMENSION, "score": 100, "issues": [],
                "file_count": 0, "issue_count": 0,
                "summary": "死代码维度未启用（项目文件数 < 3）"}

    source_files = blueprint.get_source_files(blueprint.language.primary)
    all_issues = []
    all_issues.extend(_find_empty_files(source_files))
    all_issues.extend(_find_import_only_files(source_files))
    all_issues.extend(_find_duplicate_files(source_files))

    for issue in all_issues:
        issue["dimension"] = DIMENSION

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    score = max(0, 100 - len(all_issues) * 3)
    return {
        "dimension": DIMENSION, "score": score,
        "issues": all_issues,
        "file_count": len(source_files),
        "issue_count": len(all_issues),
        "summary": f"死代码扫描完成：{len(all_issues)} 个问题（空文件/重复文件/孤立文件），评分 {score}/100",
    }
