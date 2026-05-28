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




class FixResult:
    """标准化的修复结果。

    所有修复器必须输出 FixResult，字段含义统一。
    新增 confidence 字段用于审批门控决策。

    属性:
        success: 修复是否成功应用
        action: 执行了什么操作（如 "空except → logging.exception()"）
        confidence: 修复置信度（0.0~1.0）
            > 0.8  → 自动应用
            0.5~0.8 → 进入人工审批队列
            < 0.5  → 拒绝，只记录
        error: 失败原因（success=False 时）
        diff: 修改前后的差异（供审批时参考）
        rollback_info: 回滚信息（修改前的文件内容 hash）
        fixer: 执行修复的修复器名称
        issue_type: 对应的 Issue 类型
        file: 修改的文件路径
        line: 修改的行号
    """
    success: bool
    action: str = ""
    confidence: float = 0.0
    error: str = ""
    diff: str = ""
    rollback_info: str = ""
    fixer: str = ""
    issue_type: str = ""
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


