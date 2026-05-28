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
from src.core.adapters_pkg.severity import Severity
from src.core.adapters_pkg.issue import Issue
from src.core.adapters_pkg.fix_result import FixResult

logger = logging.getLogger(__name__)




class FixerAdapter(ABC):
    """修复器标准接口。

    所有修复器必须实现此接口。pipeline 通过 registry 获取修复器实例，
    调用 fix() 获取 FixResult，再由 ConfidenceGate 决定是否应用。

    设计决策（面试话术）：
      "修复器和扫描器最大的区别是：修复器有副作用（修改文件）。
       所以 FixResult 里必须有 confidence 和 rollback_info——
       低置信度的修复不能直接应用，需要人工审批；应用后如果出问题，
       需要能回滚。这是扫描器不需要考虑的。"
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """修复器唯一名称（如 'swallowed_exception_fixer'）。"""

    @property
    @abstractmethod
    def supported_types(self) -> list[str]:
        """此修复器能处理的 Issue 类型列表。"""

    @abstractmethod
    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        """尝试修复一个问题，返回标准化 FixResult。

        注意：fix() 只生成补丁，不直接写文件。
        实际写入由 ConfidenceGate 根据置信度决定。

        Args:
            issue: 要修复的问题（来自扫描器）
            project_root: 项目根目录

        Returns:
            FixResult，包含修复动作、置信度、差异等信息。
        """

    def can_fix(self, issue_type: str) -> bool:
        """检查此修复器是否能处理指定类型的问题。"""
        return issue_type in self.supported_types


