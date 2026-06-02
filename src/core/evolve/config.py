"""evolve/config — 自进化系统核心配置

从环境变量和 config.yaml 读取项目配置：
  - PROJECT1_DIR: 项目一目录路径
  - _parse_yaml_top_level: 简易 YAML 解析（无需 PyYAML 依赖）
"""

import os
import re
from pathlib import Path
import logging

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()


def _get_project1_dir() -> Path:
    env_path = os.environ.get("PROJECT1_DIR", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    cfg = SWARM_DIR / "config.yaml"
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="utf-8")
            m = re.search(r"^\s*project1_dir:\s*(?:['\"]([^'\"]*)['\"]|(\S+))", text, re.MULTILINE)
            if m:
                path_val = m.group(1).strip() if m.group(1) else (m.group(2).strip() if m.group(2) else "")
                if path_val:
                    p = Path(path_val)
                    if p.exists():
                        return p
        except Exception:
                        logging.exception('异常捕获: ')
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
