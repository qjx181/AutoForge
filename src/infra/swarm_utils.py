"""
swarm_utils.py — 基础工具函数集

提供安全的文件读写和步骤日志辅助函数。
"""

import os
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import sys
import datetime
from typing import Union, Optional
import logging
def read_file_safe(path: Union[str, os.PathLike]) -> Optional[str]:
    """安全读取文件内容。

    若文件不存在、路径为目录或读取过程中发生 I/O 错误，返回 None。
    成功时返回文件内容的字符串表示。

    注意：
        - 返回空字符串 "" 表示文件存在且为空，而非文件不存在。
        - 文件不存在时返回 None（区别于空文件）。

    Args:
        path: 文件路径（支持 str、bytes 或 os.PathLike）。

    Returns:
        文件内容的字符串，或 None（当文件不可读或出错时）。
        空文件时返回空字符串 ""。
    """
    try:
        p = os.fspath(path)  # 接受 str / bytes / os.PathLike
        if not os.path.isfile(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def write_file_safe(path: Union[str, os.PathLike], content: str) -> bool:
    """安全写入文件内容。

    自动创建父目录；写入成功返回 True，失败返回 False。

    Args:
        path: 目标文件路径（支持 str、bytes 或 os.PathLike）。
        content: 要写入的字符串内容。

    Returns:
        写入成功 True，失败 False。
    """
    try:
        p = os.fspath(path)  # 接受 str / bytes / os.PathLike
        parent = os.path.dirname(p)
        if parent and not os.path.isdir(parent):          # 空路径或根路径跳过
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except (OSError, NotADirectoryError, TypeError, ValueError):
        return False


def log_step(step_name: str) -> None:
    """打印步骤日志到 stderr。

    格式：[TIMESTAMP] ▶ STEP_NAME

    Args:
        step_name: 当前步骤名称（例如 "初始化配置"、"读取数据"）。
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{timestamp}] ▶ {step_name}", file=sys.stderr, flush=True)
