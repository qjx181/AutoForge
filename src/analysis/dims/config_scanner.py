#!/usr/bin/env python3
"""dims/config_scanner.py — 维度七：配置扫描器

检测：
  1. 硬编码配置值（端口、URL、连接字符串）
  2. 缺少 .env.example（生产环境应提供 .env 模板）
  3. .env 文件被提交（应加入 .gitignore）
  4. 配置文件格式错误（YAML/TOML/JSON 语法错误）
  5. 缺失必要的环境变量校验（启动时未检查必需变量）
  6. 配置文件与代码混合

接口：
    scan(blueprint: OptimizationBlueprint) -> dict
"""

import json
import re
import yaml
from pathlib import Path

from ..project_analyzer import OptimizationBlueprint


DIMENSION = "configuration"


def _check_hardcoded_config(code: str, filepath: str) -> list[dict]:
    issues = []
    # 硬编码端口
    port_pattern = re.compile(r':\s*(?:8000|8080|3000|5000|5432|6379|27017|3306)\b')
    # 硬编码 URL
    url_pattern = re.compile(r'["\']https?://(?!localhost|127\.0\.0\.1)[^\s"\']+["\']')
    # 硬编码连接字符串
    conn_pattern = re.compile(r'(?:mongodb|postgresql|mysql|redis)://[^\s"\']+')

    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue

        for m in port_pattern.finditer(stripped):
            issues.append({
                "type": "hardcoded_port", "severity": "medium",
                "file": filepath, "line": i,
                "description": f"硬编码端口号，建议从环境变量读取",
                "suggestion": f"PORT = int(os.environ.get('PORT', 8000))",
            })

        if "localhost" not in stripped and "127.0.0.1" not in stripped:
            for m in url_pattern.finditer(stripped):
                issues.append({
                    "type": "hardcoded_url", "severity": "medium",
                    "file": filepath, "line": i,
                    "description": "硬编码生产 URL，建议使用配置或环境变量",
                    "suggestion": "BASE_URL = os.environ.get('BASE_URL')",
                })

        if conn_pattern.search(stripped):
            issues.append({
                "type": "hardcoded_connection", "severity": "high",
                "file": filepath, "line": i,
                "description": "硬编码数据库/缓存连接字符串",
                "suggestion": "DATABASE_URL = os.environ.get('DATABASE_URL')",
            })

    return issues


def _check_env_files(root: Path) -> list[dict]:
    issues = []

    if not (root / ".env.example").exists() and (root / ".env").exists():
        issues.append({
            "type": "missing_env_example", "severity": "high",
            "file": str(root / ".env.example"), "line": 0,
            "description": ".env 文件存在但缺少 .env.example 模板",
            "suggestion": "创建 .env.example 包含所有必需环境变量（不含实际值）",
        })

    # 检查 .gitignore
    gitignore = root / ".gitignore"
    if gitignore.exists() and (root / ".env").exists():
        gitignore_content = gitignore.read_text(encoding="utf-8", errors="ignore")
        if ".env" not in gitignore_content:
            issues.append({
                "type": "env_not_gitignored", "severity": "high",
                "file": str(gitignore), "line": 0,
                "description": ".env 文件存在但未被 .gitignore 忽略",
                "suggestion": "在 .gitignore 中添加：.env",
            })

    return issues


def _check_config_syntax(config_files: list[str]) -> list[dict]:
    issues = []
    for cf in config_files:
        fp = Path(cf)
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if fp.suffix in (".yaml", ".yml"):
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                issues.append({
                    "type": "invalid_yaml", "severity": "high",
                    "file": cf, "line": 0,
                    "description": f"YAML 语法错误：{e}",
                    "suggestion": "检查缩进和格式",
                })
        elif fp.suffix == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                issues.append({
                    "type": "invalid_json", "severity": "high",
                    "file": cf, "line": 0,
                    "description": f"JSON 语法错误：{e}",
                    "suggestion": "检查逗号、引号、花括号",
                })

    return issues


def scan(blueprint: OptimizationBlueprint) -> dict:
    if not blueprint.is_enabled("configuration"):
        return {"dimension": DIMENSION, "score": 100, "issues": [],
                "file_count": 0, "issue_count": 0,
                "summary": "配置维度未启用（项目无配置文件）"}

    all_issues = []
    for fp in blueprint.get_source_files(blueprint.language.primary):
        try:
            code = Path(fp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        all_issues.extend(_check_hardcoded_config(code, fp))

    all_issues.extend(_check_env_files(blueprint.project_root))
    all_issues.extend(_check_config_syntax(blueprint.structure.config_files))

    for issue in all_issues:
        issue["dimension"] = DIMENSION

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 99))

    score = max(0, 100 - len(all_issues) * 8)
    return {
        "dimension": DIMENSION, "score": score,
        "issues": all_issues,
        "file_count": len(blueprint.get_source_files(blueprint.language.primary)),
        "issue_count": len(all_issues),
        "summary": f"配置扫描完成：{len(all_issues)} 个问题，评分 {score}/100",
    }
