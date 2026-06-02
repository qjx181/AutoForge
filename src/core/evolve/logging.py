"""evolve/logging — 统一日志输出（控制台 + 文件）

提供 relog(tag, *args) 作为所有 evolve 模块的唯一日志接口。
支持 JSON 格式（CLI --json-logs 参数开启），日志同时写入 logs/self_evolve.log。
"""

import json
from datetime import datetime
import logging


def _format_log(level: str, msg: str) -> str:
    """格式化单条日志（纯文本或 JSON）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    if _cfg._JSON_MODE:
        return json.dumps(
            {"timestamp": ts, "level": level, "message": msg},
            ensure_ascii=False,
        )
    return f"[{ts}] {level} {msg}"


def relog(tag: str, *args) -> None:
    """简易日志输出（控制台 + 文件）。支持 JSON 模式。"""
    text = ("" if not args else " ".join(str(a) for a in args))
    msg = f"{tag}" + (f" {text}" if text else "")
    line = _format_log("INFO", msg)
    logging.info(line)
    _cfg.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _cfg.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


