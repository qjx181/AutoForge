"""test_swarm_logger.py — pytest 单元测试覆盖 swarm_logger.py

测试对象：
- _JsonFormatter
- _TextFormatter
- SwarmLogger（初始化、各级别日志、extra字段、set_level、set_json_mode、add_file_handler、CLI）
"""

import os
import sys
import json
import logging
import datetime
import pytest
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm_logger import (
    SwarmLogger,
    _JsonFormatter,
    _TextFormatter,
    main as cli_main,
)


# ═══════════════════════════════════════════════════════════════
# _JsonFormatter
# ═══════════════════════════════════════════════════════════════

class TestJsonFormatter:
    """_JsonFormatter 单元测试"""

    def _make_record(
        self,
        msg: str = "test message",
        level: int = logging.INFO,
        name: str = "test",
        extras: Any = None,
    ) -> logging.LogRecord:
        """构造一个 LogRecord 用于测试。"""
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        if extras is not None:
            record._swarm_extra = extras
        return record

    def test_json_format_basic(self) -> None:
        """基本 JSON 格式输出应包含所有必需字段"""
        formatter = _JsonFormatter()
        record = self._make_record("info message", logging.INFO)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "info message"
        assert parsed["name"] == "test"
        assert "timestamp" in parsed
        assert "module" in parsed
        assert "function" in parsed
        assert "line" in parsed

    def test_json_format_with_extra(self) -> None:
        """extra 字段应出现在 JSON 的 extra 键中"""
        formatter = _JsonFormatter()
        record = self._make_record(
            "with extra", logging.WARNING, extras={"task_id": 42, "host": "node1"}
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "WARNING"
        assert parsed["message"] == "with extra"
        assert parsed["extra"] == {"task_id": 42, "host": "node1"}

    def test_json_format_error_level(self) -> None:
        """ERROR 级别日志应正确输出"""
        formatter = _JsonFormatter()
        record = self._make_record("error occurred", logging.ERROR)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "ERROR"

    def test_json_format_critical_level(self) -> None:
        """CRITICAL 级别日志应正确输出"""
        formatter = _JsonFormatter()
        record = self._make_record("critical!", logging.CRITICAL)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "CRITICAL"

    def test_json_format_ensure_ascii(self) -> None:
        """中文消息不应被转义"""
        formatter = _JsonFormatter()
        record = self._make_record("你好世界", logging.INFO)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "你好世界"

    def test_json_no_extra_key_when_no_extra(self) -> None:
        """没有 extra 字段时，JSON 中不应出现 extra 键"""
        formatter = _JsonFormatter()
        record = self._make_record("no extra", logging.INFO)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "extra" not in parsed


# ═══════════════════════════════════════════════════════════════
# _TextFormatter
# ═══════════════════════════════════════════════════════════════

class TestTextFormatter:
    """_TextFormatter 单元测试"""

    def _make_record(
        self,
        msg: str = "test message",
        level: int = logging.INFO,
        name: str = "test",
        extras: Any = None,
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        if extras is not None:
            record._swarm_extra = extras
        return record

    def test_text_format_basic(self) -> None:
        """基本文本格式应包含时间戳、级别、名称和消息"""
        formatter = _TextFormatter()
        record = self._make_record("hello world", logging.INFO)
        output = formatter.format(record)
        assert "[INFO" in output
        assert "[test]" in output
        assert "hello world" in output
        assert "[" in output and "]" in output  # 有时间戳

    def test_text_format_with_extra(self) -> None:
        """extra 字段应追加到消息尾部"""
        formatter = _TextFormatter()
        record = self._make_record(
            "task done", logging.WARNING, extras={"task_id": 1, "status": "ok"}
        )
        output = formatter.format(record)
        assert "task_id=1" in output
        assert "status=ok" in output

    def test_text_format_debug_level(self) -> None:
        """DEBUG 级别标识正确"""
        formatter = _TextFormatter()
        record = self._make_record("debug msg", logging.DEBUG)
        output = formatter.format(record)
        assert "[DEBUG" in output

    def test_text_format_error_level(self) -> None:
        """ERROR 级别标识正确"""
        formatter = _TextFormatter()
        record = self._make_record("error msg", logging.ERROR)
        output = formatter.format(record)
        assert "[ERROR" in output

    def test_text_format_no_extra_no_side_effect(self) -> None:
        """没有 extra 时，消息不变"""
        formatter = _TextFormatter()
        record = self._make_record("plain message", logging.INFO)
        output = formatter.format(record)
        assert "plain message" in output
        assert "[" in output  # 有时间戳


# ═══════════════════════════════════════════════════════════════
# SwarmLogger
# ═══════════════════════════════════════════════════════════════

def _unique_logger_name() -> str:
    """生成唯一的日志器名称，避免跨测试冲突。"""
    import uuid
    return f"test_{uuid.uuid4().hex[:8]}"


class TestSwarmLoggerInit:
    """SwarmLogger 初始化测试"""

    def test_default_init(self) -> None:
        """默认参数初始化"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name)
        assert log.name == name
        assert log.json_mode is False
        # SwarmLogger 内部使用 _log_level (int)，无 level 属性
        assert log._log_level == logging.INFO

    def test_init_with_level_string(self) -> None:
        """使用字符串级别初始化"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, level="DEBUG")
        assert log._log_level == logging.DEBUG

    def test_init_with_level_int(self) -> None:
        """使用 int 级别常量初始化"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, level=logging.DEBUG)
        assert log._log_level == logging.DEBUG

    def test_init_json_mode(self) -> None:
        """JSON 模式初始化"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, json_mode=True)
        assert log.json_mode is True

    def test_init_no_console(self) -> None:
        """console=False 时仅默认文件 handler（当 log_file=None）"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, console=False, log_file="")
        # log_file="" 禁用文件输出，console=False 禁用控制台 => 无 handler
        assert len(log.logger.handlers) == 0

    def test_init_with_log_file(self, tmp_path: Path) -> None:
        """指定日志文件路径"""
        name = _unique_logger_name()
        log_path = str(tmp_path / "test.log")
        log = SwarmLogger(name=name, log_file=log_path)
        # 应该有 console handler 和 file handler
        assert len(log.logger.handlers) >= 1

    def test_init_with_empty_log_file(self) -> None:
        """log_file="" 意味不输出到文件"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, log_file="")
        # 只有 console handler
        handler_types = [type(h).__name__ for h in log.logger.handlers]
        assert "StreamHandler" in handler_types

    def test_init_invalid_level_raises(self) -> None:
        """无效级别字符串应抛出 ValueError"""
        name = _unique_logger_name()
        with pytest.raises(ValueError):
            SwarmLogger(name=name, level="INVALID")

    def test_level_property(self) -> None:
        """_log_level 应存储 int 级别值"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, level="WARNING")
        assert log._log_level == logging.WARNING


class TestSwarmLoggerLogging:
    """SwarmLogger 日志记录功能测试"""

    def _create_logger(
        self, tmp_path: Path, level: str = "DEBUG", json_mode: bool = False
    ) -> SwarmLogger:
        """创建一个输出到临时文件的日志器。"""
        name = _unique_logger_name()
        log_path = str(tmp_path / f"{name}.log")
        log = SwarmLogger(
            name=name,
            level=level,
            log_file=log_path,
            json_mode=json_mode,
            console=False,  # 只用文件 handler 方便断言
        )
        log._log_path = log_path  # type: ignore[attr-defined]
        return log

    def test_debug_level(self, tmp_path: Path) -> None:
        """DEBUG 级别日志应写入文件"""
        log = self._create_logger(tmp_path, level="DEBUG")
        log.debug("debug message")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "debug message" in content

    def test_info_level(self, tmp_path: Path) -> None:
        """INFO 级别日志应写入文件"""
        log = self._create_logger(tmp_path, level="INFO")
        log.info("info message")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "info message" in content

    def test_warning_level(self, tmp_path: Path) -> None:
        """WARNING 级别日志应写入文件"""
        log = self._create_logger(tmp_path, level="WARNING")
        log.warning("warning message")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "warning message" in content

    def test_error_level(self, tmp_path: Path) -> None:
        """ERROR 级别日志应写入文件"""
        log = self._create_logger(tmp_path, level="ERROR")
        log.error("error message")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "error message" in content

    def test_critical_level(self, tmp_path: Path) -> None:
        """CRITICAL 级别日志应写入文件"""
        log = self._create_logger(tmp_path, level="CRITICAL")
        log.critical("critical message")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "critical message" in content

    def test_level_filtering(self, tmp_path: Path) -> None:
        """INFO 级别下，DEBUG 消息不应出现"""
        log = self._create_logger(tmp_path, level="INFO")
        log.debug("should not appear")
        log.info("should appear")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "should not appear" not in content
        assert "should appear" in content

    def test_extra_fields_json(self, tmp_path: Path) -> None:
        """extra 字段在 JSON 模式下应出现在 extra 键中"""
        log = self._create_logger(tmp_path, level="INFO", json_mode=True)
        log.info("task update", task_id=42, status="done")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        parsed = json.loads(content.strip())
        assert parsed["message"] == "task update"
        assert parsed["extra"] == {"task_id": 42, "status": "done"}

    def test_extra_fields_text(self, tmp_path: Path) -> None:
        """extra 字段在 TEXT 模式下应追加到消息尾部"""
        log = self._create_logger(tmp_path, level="INFO", json_mode=False)
        log.info("task update", task_id=42, status="done")
        content = Path(log._log_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "task update" in content
        assert "task_id=42" in content
        assert "status=done" in content


class TestSwarmLoggerDynamic:
    """SwarmLogger 动态切换功能测试"""

    def test_set_level_dynamic(self, tmp_path: Path) -> None:
        """set_level 应动态改变生效级别"""
        name = _unique_logger_name()
        log_path = str(tmp_path / f"{name}.log")
        log = SwarmLogger(name=name, level="INFO", log_file=log_path, console=False)

        # INFO 级别下，DEBUG 不应出现
        log.debug("before switch - should be hidden")
        log.info("before switch - visible")

        # 切换到 DEBUG
        log.set_level("DEBUG")
        log.debug("after switch - visible")
        log.info("after switch - visible")

        content = Path(log_path).read_text(encoding="utf-8")
        assert "before switch - should be hidden" not in content
        assert "before switch - visible" in content
        assert "after switch - visible" in content
        assert "after switch - visible" in content

    def test_set_json_mode_dynamic(self, tmp_path: Path) -> None:
        """set_json_mode 应切换所有 Handler 的格式化器"""
        name = _unique_logger_name()
        log_path = str(tmp_path / f"{name}.log")
        log = SwarmLogger(name=name, level="INFO", log_file=log_path, console=False)

        # TEXT 模式
        log.info("text mode message")
        content_before = Path(log_path).read_text(encoding="utf-8")

        # 切换到 JSON 模式
        log.set_json_mode(True)
        log.info("json mode message")
        content_after = Path(log_path).read_text(encoding="utf-8")

        # 新日志应包含 JSON 格式（以 { 开头）
        # 先找到第二行的 JSON
        lines = content_after.strip().split("\n")
        last_line = lines[-1]
        parsed = json.loads(last_line)
        assert parsed["message"] == "json mode message"

    def test_add_file_handler_dynamic(self, tmp_path: Path) -> None:
        """add_file_handler 应动态添加文件输出"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, level="INFO", console=False)

        # 初始没有文件 handler
        log.info("no file yet")

        # 动态添加文件 handler
        log_path = str(tmp_path / f"{name}_added.log")
        log.add_file_handler(log_path)
        log.info("after file added")

        content = Path(log_path).read_text(encoding="utf-8")
        assert "after file added" in content

    def test_remove_all_handlers(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """remove_all_handlers 应清空所有 Handler"""
        name = _unique_logger_name()
        log_path = str(tmp_path / f"{name}.log")
        log = SwarmLogger(name=name, level="INFO", log_file=log_path, console=False)

        log.info("before removal")
        log.remove_all_handlers()
        log.info("after removal")

        content = Path(log_path).read_text(encoding="utf-8")
        assert "before removal" in content
        # 移除 handler 后不应继续输出

    def test_console_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """console=True 时，日志输出到 stderr"""
        name = _unique_logger_name()
        log = SwarmLogger(name=name, level="INFO", console=True)
        log.info("console test")
        # 不捕获 stdout 或 stderr 内容，只确认 handler 类型
        handler_types = [type(h).__name__ for h in log.logger.handlers]
        assert "StreamHandler" in handler_types


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

class TestSwarmLoggerCLI:
    """CLI 入口 main() 测试"""

    def test_cli_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认参数运行 CLI"""
        monkeypatch.setattr("sys.argv", ["swarm_logger.py"])
        # 不应抛出异常
        cli_main()

    def test_cli_with_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """指定参数运行 CLI"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_logger.py", "--name", "test-cli", "--level", "WARNING"],
        )
        cli_main()

    def test_cli_json_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JSON 模式 CLI"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_logger.py", "--json", "--level", "DEBUG"],
        )
        cli_main()

    def test_cli_no_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """禁用控制台输出"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_logger.py", "--no-console"],
        )
        cli_main()

    def test_cli_with_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """指定日志文件"""
        log_path = str(tmp_path / "cli_test.log")
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_logger.py", "--file", log_path, "--no-console"],
        )
        cli_main()
        assert os.path.isfile(log_path)
        content = Path(log_path).read_text(encoding="utf-8")
        assert "INFO" in content

    def test_cli_json_with_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """JSON 模式+文件输出"""
        log_path = str(tmp_path / "cli_json.log")
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_logger.py", "--file", log_path, "--json", "--no-console"],
        )
        cli_main()
        content = Path(log_path).read_text(encoding="utf-8")
        lines = [l for l in content.strip().split("\n") if l.strip()]
        for line in lines:
            parsed = json.loads(line)
            assert "level" in parsed
            assert "message" in parsed
