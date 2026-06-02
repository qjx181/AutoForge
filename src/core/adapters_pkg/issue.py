"""adapters.py — Scanner/Fixer 标准化接口

设计动机（面试话术）：
  "项目三有 9 个维度扫描器和 6 种修复器，但它们的接口不统一——
   sec_scanner 用 scan(blueprint)，deep_enterprise_scanner 用 scan_deep(project_root)，
   enterprise_fixer 用 try_fix_deep(issue, project_root)。
   为了支持插拔式扩展（新增扫描器/修复器不改 orchestrator），我定义了标准 Adapter 接口。"

借鉴 HiveWard 的 RuntimeAdapter 模式：
  - 上游（pipeline）只依赖抽象接口，不依赖具体实现
  - 新增扫描器只需实现 ScannerAdapter + 注册到 registry
  - 新增修复器只需实现 FixerAdapter + 注册到 registry

核心类：
  Issue              — 标准化的问题描述（所有扫描器统一输出）
  FixResult          — 标准化的修复结果（所有修复器统一输出）
  ScannerAdapter     — 扫描器抽象基类
  FixerAdapter       — 修复器抽象基类
  ScannerRegistry    — 扫描器注册表（按名称查找）
  FixerRegistry      — 修复器注册表（按 issue_type 查找）
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    """标准化的问题描述。

    所有扫描器必须输出 Issue 列表，字段含义统一。
    这样 pipeline 不需要关心"这个问题来自哪个扫描器"。

    属性:
        type: 问题类型标识符（如 "sql_injection", "swallowed_exception"）
        severity: 严重等级（critical/high/medium/low）
        file: 相对于项目根目录的文件路径
        line: 问题所在行号（0 表示文件级问题）
        description: 人类可读的问题描述
        suggestion: 修复建议（给修复器或人工参考）
        scanner: 发现此问题的扫描器名称（自动填充）
        context: 额外上下文（如周围的代码片段、AST 节点信息）
    """
    type: str
    severity: str
    file: str
    line: int
    description: str
    suggestion: str = ""
    scanner: str = ""
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Issue":
        return Issue(
            type=d.get("type", "unknown"),
            severity=d.get("severity", "medium"),
            file=d.get("file", ""),
            line=d.get("line", 0),
            description=d.get("description", ""),
            suggestion=d.get("suggestion", ""),
            scanner=d.get("scanner", ""),
            context=d.get("context", {}),
        )


