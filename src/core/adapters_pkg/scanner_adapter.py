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

logger = logging.getLogger(__name__)




class ScannerAdapter(ABC):
    """扫描器标准接口。

    所有扫描器必须实现此接口。pipeline 通过 registry 获取扫描器实例，
    调用 scan() 获取 Issue 列表，不需要知道底层实现。

    设计决策（面试话术）：
      "为什么用 ABC 而不是鸭子类型？因为扫描器的输出格式必须严格统一——
       如果某个扫描器返回的 dict 缺了 'severity' 字段，下游的审批门控
       就会崩溃。ABC 强制编译期检查，比运行时报错更安全。"
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """扫描器唯一名称（如 'security', 'performance', 'enterprise'）。"""

    @property
    @abstractmethod
    def dimension(self) -> str:
        """扫描维度（如 'security', 'quality', 'performance'）。"""

    @abstractmethod
    def scan(self, project_root: Path) -> list[Issue]:
        """扫描项目，返回标准化 Issue 列表。

        Args:
            project_root: 项目根目录（绝对路径）

        Returns:
            Issue 列表，每个 Issue 代表一个发现的问题。
            空列表表示没有发现问题。
        """

    def is_available(self) -> bool:
        """检查此扫描器是否可用（依赖是否满足）。默认可用。"""
        return True


