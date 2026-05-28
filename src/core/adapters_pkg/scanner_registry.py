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
from src.core.adapters_pkg.scanner_adapter import ScannerAdapter
from src.core.adapters_pkg.issue import Issue

logger = logging.getLogger(__name__)




class ScannerRegistry:
    """扫描器注册表。

    集中管理所有扫描器实例，按名称查找。
    pipeline 启动时注册所有扫描器，运行时按需调用。

    使用方式：
        registry = ScannerRegistry()
        registry.register(SecurityScanner())
        registry.register(PerformanceScanner())
        # ...
        for scanner in registry.all():
            issues.extend(scanner.scan(project_root))
    """

    def __init__(self):
        self._scanners: dict[str, ScannerAdapter] = {}

    def register(self, scanner: ScannerAdapter) -> None:
        """注册一个扫描器。"""
        self._scanners[scanner.name] = scanner
        logger.info("Scanner registered: %s (dimension=%s)", scanner.name, scanner.dimension)

    def get(self, name: str) -> Optional[ScannerAdapter]:
        """按名称获取扫描器。"""
        return self._scanners.get(name)

    def all(self) -> list[ScannerAdapter]:
        """返回所有已注册且可用的扫描器。"""
        return [s for s in self._scanners.values() if s.is_available()]

    def by_dimension(self, dimension: str) -> list[ScannerAdapter]:
        """按维度筛选扫描器。"""
        return [s for s in self.all() if s.dimension == dimension]

    def scan_all(self, project_root: Path) -> list[Issue]:
        """运行所有扫描器，汇总 Issue 列表。"""
        all_issues: list[Issue] = []
        for scanner in self.all():
            try:
                issues = scanner.scan(project_root)
                for issue in issues:
                    issue.scanner = scanner.name
                all_issues.extend(issues)
                logger.info("Scanner %s: %d issues found", scanner.name, len(issues))
            except Exception as e:
                logger.error("Scanner %s failed: %s", scanner.name, e)
        return all_issues
