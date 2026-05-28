#!/usr/bin/env python3
"""self_evolve_round.py — 项目三自进化后勤脚本

职责（每 30 分钟由 cronjob 触发）：
  1. PID 文件锁 + 冲突自愈
  2. 磁盘空间检查 + 日志轮转
  3. 成本熔断检查
  4. 项目一同步（git pull + commit）
  5. 项目三同步（git pull + commit）
  6. 🚀 持续优化引擎（九维全覆盖，任意目标项目）：
       扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快
  7. 分层委托诊断 + 强制委托检查
  8. ⬆️ 并行任务规划（微委托集成）
  9. 更新 state.json

注意：
  实际的任务执行（write_file / delegate_task）由 Hermes Agent cronjob 的 prompt 驱动。
  本脚本只做"后勤 + 规划"——打扫战场、生成执行计划。
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

# ─── 路径（自动计算，不依赖硬编码）─────────────────────────────────────
# self_evolve_round.py 现在位于 src/core/，需要向上两级回到项目根目录
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()

# ─── PROJECT1_DIR：从环境变量或配置读取，不硬编码路径 ──────────────────
# 用法：export PROJECT1_DIR=/path/to/project1
# 或在 config.yaml 中设置 project1_dir 字段

def _get_project1_dir() -> Path:
    env_path = os.environ.get("PROJECT1_DIR", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    # 回退：从 config.yaml 读取
    cfg = SWARM_DIR / "config.yaml"
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="utf-8")
            # 支持 project1_dir: "path" 或 project1_dir: path
            m = re.search(r"^\s*project1_dir:\s*(?:['\"]([^'\"]*)['\"]|(\S+))", text, re.MULTILINE)
            if m:
                path_val = m.group(1).strip() if m.group(1) else (m.group(2).strip() if m.group(2) else "")
                if path_val:
                    p = Path(path_val)
                    if p.exists():
                        return p
        except Exception:
            pass
    return None  # 不存在时同步步骤跳过

PROJECT1_DIR = _get_project1_dir()


def _finalize_list(result: dict, current_key: str, list_items: list, in_list: bool):
    """如果当前在处理列表，把收集到的列表项写入结果。"""
    if current_key and in_list:
        result[current_key] = list_items


def _process_top_level_key(line: str, result: dict) -> str:
    """处理 YAML 顶层 key: value 行，返回 key 名称。"""
    key = line.split(":")[0].strip()
    value = line.split(":", 1)[1].strip().strip("'\"").strip()
    if value:
        result[key] = value
    elif not line.rstrip().endswith(":"):
        result[key] = value
    else:
        result[key] = None
    return key


def _process_list_item(line: str, current_key: str, current_indent: int,
                       result: dict, list_items: list, in_list: bool):
    """处理列表项行（以 - 开头）。"""
    if current_key and current_key != current_key:  # never true, placeholder
        pass
    item = line[1:].strip().strip("'\"").strip()
    if item:
        list_items.append(item)
    return True


def _parse_yaml_top_level(text: str, result: dict) -> None:
    """解析 YAML 顶层 key: value 对。"""
    current_key = None
    current_indent = 0
    in_list = False
    list_items = []
    for raw_line in text.split("\n"):
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw_line) - len(line)
        if indent == 0 and ":" in line:
            _finalize_list(result, current_key, list_items, in_list)
            if in_list:
                list_items = []
                in_list = False
            current_key = _process_top_level_key(line, result)
            current_indent = indent
        elif current_key and indent > current_indent and ":" not in line:
            if line.startswith("- "):
                in_list = _process_list_item(line, current_key, current_indent,
                                             result, list_items, in_list)
    _finalize_list(result, current_key, list_items, in_list)
