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
from src.core.adapters_pkg.fixer_adapter import FixerAdapter
from src.core.adapters_pkg.fix_result import FixResult

logger = logging.getLogger(__name__)




class FixerRegistry:
    """修复器注册表。

    集中管理所有修复器实例，按 Issue 类型查找合适的修复器。
    一个 Issue 类型只能有一个修复器（避免冲突）。

    使用方式：
        registry = FixerRegistry()
        registry.register(SwallowedExceptionFixer())
        registry.register(BareExceptFixer())
        # ...
        fixer = registry.get_fixer(issue.type)
        if fixer:
            result = fixer.fix(issue, project_root)
    """

    def __init__(self):
        self._fixers: dict[str, FixerAdapter] = {}  # issue_type → fixer
        self._type_fixers: dict[str, list[FixerAdapter]] = {}  # issue_type → [fixer1, fixer2, ...]
        self._fixer_instances: list[FixerAdapter] = []

    def register(self, fixer: FixerAdapter) -> None:
        """注册一个修复器。按 supported_types 索引。"""
        self._fixer_instances.append(fixer)
        for issue_type in fixer.supported_types:
            if issue_type in self._fixers and issue_type != "*":
                logger.warning(
                    "Issue type '%s' already has a fixer (%s), overriding with %s",
                    issue_type, self._fixers[issue_type].name, fixer.name,
                )
            self._fixers[issue_type] = fixer
            if issue_type not in self._type_fixers:
                self._type_fixers[issue_type] = []
            self._type_fixers[issue_type].append(fixer)
        logger.info("Fixer registered: %s (types=%s)", fixer.name, fixer.supported_types)

    def get_fixer(self, issue_type: str) -> Optional[FixerAdapter]:
        """按 Issue 类型获取对应的修复器。"""
        return self._fixers.get(issue_type)

    def get_fixers_for_type(self, issue_type: str) -> list[FixerAdapter]:
        """获取所有能处理该 issue_type 的修复器列表。

        用于 fallback chain 和策略选择器。
        返回注册顺序的 fixer 列表。
        """
        # 精确匹配 + 通配符匹配
        result = []
        if issue_type in self._type_fixers:
            result.extend(self._type_fixers[issue_type])
        if "*" in self._type_fixers:
            for f in self._type_fixers["*"]:
                if f not in result:
                    result.append(f)
        return result

    def can_fix(self, issue_type: str) -> bool:
        """检查是否有修复器能处理此类型。"""
        return issue_type in self._fixers

    def supported_types(self) -> list[str]:
        """返回所有可修复的 Issue 类型。"""
        return list(self._fixers.keys())

    def all(self) -> list[FixerAdapter]:
        """返回所有已注册的修复器实例。"""
        return self._fixer_instances


