"""swarm_logger.py — 日志记录工具模块

提供结构化的日志功能，同时输出到控制台（stderr）和文件（RotatingFileHandler）。
支持 DEBUG / INFO / WARNING / ERROR / CRITICAL 五个级别，
支持 TEXT 和 JSON 两种输出格式，支持按文件大小自动轮转。

用法示例
--------
    from swarm_logger import SwarmLogger

    # 默认 TEXT 格式，输出到 stderr
    log = SwarmLogger()
    log.info("系统启动完成")
    log.error("连接失败", host="db.example.com", port=5432)

    # JSON 格式，输出到文件和 stderr
    log2 = SwarmLogger(
        name="worker",
        level="DEBUG",
        log_file="logs/worker.log",
        json_mode=True,
    )
    log2.debug("处理任务", task_id=42, queue="high")
"""

import os
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import sys
import json
import logging
import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union
from logging.handlers import RotatingFileHandler


_ROOT = Path(__file__).parent.parent.parent
DEFAULT_LOG_DIR = _ROOT / "logs"


class _JsonFormatter(logging.Formatter):
    """JSON 格式日志格式化器 —— 每条日志输出为一行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """format - 将 LogRecord 格式化为 JSON 字符串。"""
        entry: Dict[str, Any] = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        extras = getattr(record, "_swarm_extra", None)
        if extras:
            entry["extra"] = extras
        try:
            return json.dumps(entry, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            entry["extra"] = str(entry.get("extra", {}))
            return json.dumps(entry, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    """TEXT 格式日志格式化器 —— 人类可读的彩色文本输出。"""

    def format(self, record: logging.LogRecord) -> str:
        """format - 将 LogRecord 格式化为文本字符串。"""
        extras = getattr(record, "_swarm_extra", None)
        if extras:
            extra_str = " | ".join(f"{k}={v}" for k, v in extras.items())
            record.msg = f"{record.msg}  [{extra_str}]"

        timestamp = datetime.datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return (
            f"[{timestamp}] "
            f"[{record.levelname:<8s}] "
            f"[{record.name}] "
            f"{record.getMessage()}"
        )


class SwarmLogger:
    """SwarmLogger - 结构化日志记录器。

    同时输出日志到控制台（stderr）和文件（RotatingFileHandler），
    支持 TEXT / JSON 两种格式，支持 DEBUG 到 CRITICAL 全部级别。

    Attributes:
        logger:    内部 logging.Logger 实例。
        name:      日志器名称。
        json_mode: 是否启用 JSON 格式输出（默认 False 使用 TEXT）。
        level:     当前生效的日志级别（字符串形式，如 "INFO"）。
    """

    LEVEL_MAP: Dict[str, int] = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    def __init__(
        self,
        name: str = "swarm",
        level: Union[str, int] = "INFO",
        log_file: Optional[str] = None,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        json_mode: bool = False,
        console: bool = True,
    ) -> None:
        """初始化 SwarmLogger 实例。

        Args:
            name:        日志器名称（也是内部 logging.Logger 的名称）。
            level:       日志级别，接受字符串（如 "INFO"）或 logging 常量。
            log_file:    日志文件路径。若为 None，则在 logs/ 下自动生成。
                         若为 ""，则不启用文件输出。
            max_bytes:   单个日志文件最大字节数（默认 10 MB），超出后自动轮转。
            backup_count: 保留的备份文件个数（默认 5）。
            json_mode:   是否使用 JSON 格式输出；否则使用 TEXT 格式。
            console:     是否同时输出到控制台（stderr）。
        """
        self.name = name
        self.json_mode = json_mode
        self._log_level: int = self._resolve_level(level)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(self._log_level)
        self.logger.handlers.clear()  # 避免重复添加
        self.logger.propagate = False

        self._json_formatter = _JsonFormatter()
        self._text_formatter = _TextFormatter()

        if console:
            self._setup_console_handler()

        resolved_path = self._resolve_log_path(log_file)
        if resolved_path:
            self._setup_file_handler(resolved_path, max_bytes, backup_count)


    def _resolve_level(self, level: Union[str, int]) -> int:
        """解析日志级别 —— 将字符串或 int 转换为 logging 常量。

        Args:
            level: "DEBUG" / "INFO" / "WARNING" / "ERROR" / "CRITICAL"
                   或 logging.DEBUG 等整数常量。

        Returns:
            logging 级别常量（int）。

        Raises:
            ValueError: 当字符串不在 LEVEL_MAP 中时抛出。
        """
        if isinstance(level, int):
            return level
        if isinstance(level, str):
            upper = level.upper()
            if upper in self.LEVEL_MAP:
                return self.LEVEL_MAP[upper]
            raise ValueError(
                f"不支持的日志级别：'{level}'。"
                f"可选：{', '.join(self.LEVEL_MAP.keys())}"
            )
        raise ValueError(f"level 参数必须为 str 或 int，收到 {type(level).__name__}")

    def _resolve_log_path(self, log_file: Optional[str]) -> Optional[str]:
        """解析日志文件路径 —— 若未指定则自动生成默认路径。

        Args:
            log_file: 用户传入的日志文件路径。

        Returns:
            解析后的绝对路径字符串；若不应输出到文件则返回 None。
        """
        if log_file is not None and log_file == "":
            return None
        if log_file is not None:
            return os.path.abspath(log_file)
        os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)
        return os.path.join(DEFAULT_LOG_DIR, f"{self.name}.log")

    def _get_formatter(self) -> logging.Formatter:
        """获取当前模式对应的格式化器。"""
        return self._json_formatter if self.json_mode else self._text_formatter

    def _setup_console_handler(self) -> None:
        """设置控制台（stderr）日志 Handler。"""
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setLevel(self._log_level)
        handler.setFormatter(self._get_formatter())
        self.logger.addHandler(handler)

    def _setup_file_handler(
        self,
        log_path: str,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        """设置 RotatingFileHandler —— 按文件大小自动轮转。

        Args:
            log_path:    日志文件路径。
            max_bytes:   单文件最大字节数。
            backup_count: 保留的备份文件数。
        """
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            filename=log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setLevel(self._log_level)
        handler.setFormatter(self._get_formatter())
        self.logger.addHandler(handler)


    def debug(self, msg: str, **extra: Any) -> None:
        """记录 DEBUG 级别日志。

        Args:
            msg: 日志消息文本。
            **extra: 额外结构化字段（在 JSON 模式下输出为 extra 对象）。
        """
        self.log(logging.DEBUG, msg, **extra)

    def info(self, msg: str, **extra: Any) -> None:
        """记录 INFO 级别日志。

        Args:
            msg: 日志消息文本。
            **extra: 额外结构化字段。
        """
        self.log(logging.INFO, msg, **extra)

    def warning(self, msg: str, **extra: Any) -> None:
        """记录 WARNING 级别日志。

        Args:
            msg: 日志消息文本。
            **extra: 额外结构化字段。
        """
        self.log(logging.WARNING, msg, **extra)

    def error(self, msg: str, **extra: Any) -> None:
        """记录 ERROR 级别日志。

        Args:
            msg: 日志消息文本。
            **extra: 额外结构化字段。
        """
        self.log(logging.ERROR, msg, **extra)

    def critical(self, msg: str, **extra: Any) -> None:
        """记录 CRITICAL 级别日志。

        Args:
            msg: 日志消息文本。
            **extra: 额外结构化字段。
        """
        self.log(logging.CRITICAL, msg, **extra)

    def log(self, level: Union[str, int], msg: str, **extra: Any) -> None:
        """通用日志记录 -- 所有日志方法的最终入口。"""
        resolved = self._resolve_level(level)
        if resolved < self._log_level:
            return

        try:
            if extra:
                record = self.logger.makeRecord(
                    self.logger.name, resolved, "(unknown file)", 0, msg, (), None)
                record._swarm_extra = extra
                for handler in list(self.logger.handlers):
                    if resolved >= handler.level:
                        handler.handle(record)
            else:
                self.logger.log(resolved, msg)
        except Exception:
            print(f"[swarm_logger] 日志记录失败: {msg}", file=sys.stderr, flush=True)

    def set_level(self, level: Union[str, int]) -> None:
        """设置日志级别 —— 动态调整生效级别。

        Args:
            level: 目标级别（字符串或 logging 常量）。
        """
        resolved = self._resolve_level(level)
        self._log_level = resolved
        self.logger.setLevel(resolved)
        for handler in list(self.logger.handlers):
            handler.setLevel(resolved)

    def set_json_mode(self, enabled: bool) -> None:
        """切换 JSON / TEXT 输出格式。

        切换后所有 Handler 的格式化器都会更新。

        Args:
            enabled: True 使用 JSON 格式，False 使用 TEXT 格式。
        """
        self.json_mode = enabled
        formatter = self._get_formatter()
        for handler in list(self.logger.handlers):
            handler.setFormatter(formatter)

    def add_file_handler(
        self,
        log_path: str,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        """动态添加文件 Handler —— 在初始化后可追加日志文件输出。

        Args:
            log_path:    日志文件路径。
            max_bytes:   单文件最大字节数（默认 10 MB）。
            backup_count: 保留的备份文件数（默认 5）。
        """
        self._setup_file_handler(log_path, max_bytes, backup_count)

    def remove_all_handlers(self) -> None:
        """移除所有日志 Handler —— 清空输出目标（谨慎使用）。"""
        self.logger.handlers.clear()


def main() -> None:
    """CLI 入口 —— 直接运行 ``python swarm_logger.py`` 测试各级别日志输出。"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Swarm Logger — 结构化日志记录工具",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="swarm",
        help="日志器名称（默认 swarm）",
    )
    parser.add_argument(
        "--level",
        type=str,
        default="INFO",
        help="日志级别：DEBUG / INFO / WARNING / ERROR / CRITICAL（默认 INFO）",
    )
    parser.add_argument(
        "--file",
        type=str,
        default="",
        help="日志文件路径（默认 logs/{name}.log）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="启用 JSON 格式输出",
    )
    parser.add_argument(
        "--no-console",
        action="store_true",
        help="关闭控制台输出",
    )
    args = parser.parse_args()

    log_file: Optional[str] = args.file if args.file else None

    logger = SwarmLogger(
        name=args.name,
        level=args.level.upper(),
        log_file=log_file,
        json_mode=args.json,
        console=not args.no_console,
    )

    logger.debug("这是一条 DEBUG 日志（仅当级别 ≤ DEBUG 时可见）")
    logger.info("这是一条 INFO 日志")
    logger.warning("这是一条 WARNING 日志", component="auth")
    logger.error("这是一条 ERROR 日志", service="db", host="10.0.0.1")
    logger.critical("这是一条 CRITICAL 日志", panic=True)

    print("日志测试完成。", file=sys.stderr)


if __name__ == "__main__":
    main()
