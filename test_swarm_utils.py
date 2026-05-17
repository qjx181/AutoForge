"""test_swarm_utils.py — pytest 单元测试覆盖 swarm_utils.py

测试对象：
- read_file_safe()
- write_file_safe()
- log_step()
"""

import os
import sys
import time
import pytest
from pathlib import Path

# 将被测模块加入 sys.path（需要先添加当前目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm_utils import read_file_safe, write_file_safe, log_step


# ═══════════════════════════════════════════════════════════════
# read_file_safe
# ═══════════════════════════════════════════════════════════════

class TestReadFileSafe:
    """read_file_safe 测试"""

    def test_normal_file(self, tmp_path: Path) -> None:
        """正常读取一个存在的 UTF-8 文本文件"""
        f = tmp_path / "hello.txt"
        f.write_text("Hello, World!", encoding="utf-8")
        result = read_file_safe(str(f))
        assert result == "Hello, World!"

    def test_empty_file(self, tmp_path: Path) -> None:
        """空文件应返回空字符串而非 None"""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = read_file_safe(str(f))
        assert result == ""

    def test_file_not_exist(self, tmp_path: Path) -> None:
        """文件不存在应返回 None"""
        result = read_file_safe(str(tmp_path / "no_such_file.txt"))
        assert result is None

    def test_path_is_directory(self, tmp_path: Path) -> None:
        """路径为目录时应返回 None"""
        result = read_file_safe(str(tmp_path))
        assert result is None

    def test_encoding_error(self, tmp_path: Path) -> None:
        """非 UTF-8 编码文件（如二进制文件）应返回 None（UnicodeDecodeError）"""
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\xff\xfe\x00\x01\x02\xff")
        result = read_file_safe(str(f))
        assert result is None

    def test_pathlike_object(self, tmp_path: Path) -> None:
        """接受 os.PathLike 对象（如 pathlib.Path）"""
        f = tmp_path / "pathlike.txt"
        f.write_text("pathlib works", encoding="utf-8")
        result = read_file_safe(f)  # Path 对象
        assert result == "pathlib works"

    def test_bytes_path(self, tmp_path: Path) -> None:
        """接受 bytes 类型路径"""
        f = tmp_path / "bytes_path.txt"
        f.write_text("bytes path", encoding="utf-8")
        result = read_file_safe(bytes(f))
        assert result == "bytes path"

    def test_os_error_on_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """open() 抛出 OSError 时应返回 None"""
        # 创建一个有效文件，但 mock open 使其失败
        f = tmp_path / "will_fail.txt"
        f.write_text("content", encoding="utf-8")

        import builtins
        original_open = builtins.open

        def failing_open(*args: object, **kwargs: object) -> object:
            raise OSError("模拟 I/O 错误")

        monkeypatch.setattr(builtins, "open", failing_open)
        result = read_file_safe(str(f))
        assert result is None


# ═══════════════════════════════════════════════════════════════
# write_file_safe
# ═══════════════════════════════════════════════════════════════

class TestWriteFileSafe:
    """write_file_safe 测试"""

    def test_normal_write(self, tmp_path: Path) -> None:
        """正常写入一个文件"""
        f = tmp_path / "output.txt"
        ok = write_file_safe(str(f), "Hello, World!")
        assert ok is True
        assert f.read_text(encoding="utf-8") == "Hello, World!"

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        """覆盖已存在的文件"""
        f = tmp_path / "existing.txt"
        f.write_text("old content", encoding="utf-8")
        ok = write_file_safe(str(f), "new content")
        assert ok is True
        assert f.read_text(encoding="utf-8") == "new content"

    def test_auto_create_parent_dir(self, tmp_path: Path) -> None:
        """父目录不存时自动创建"""
        f = tmp_path / "sub" / "deep" / "nested.txt"
        ok = write_file_safe(str(f), "nested content")
        assert ok is True
        assert f.read_text(encoding="utf-8") == "nested content"
        assert f.parent.exists()

    def test_write_readonly_dir(self, tmp_path: Path) -> None:
        """向只读目录写入应返回 False"""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)
        try:
            f = readonly_dir / "cant_write.txt"
            ok = write_file_safe(str(f), "should fail")
            assert ok is False
        finally:
            readonly_dir.chmod(0o755)  # 恢复权限，方便清理

    def test_pathlike_object(self, tmp_path: Path) -> None:
        """接受 os.PathLike 对象"""
        f = tmp_path / "pathlike_out.txt"
        ok = write_file_safe(f, "pathlike output")
        assert ok is True
        assert f.read_text(encoding="utf-8") == "pathlike output"

    def test_empty_content(self, tmp_path: Path) -> None:
        """写入空字符串"""
        f = tmp_path / "empty_out.txt"
        ok = write_file_safe(str(f), "")
        assert ok is True
        assert f.read_text(encoding="utf-8") == ""


# ═══════════════════════════════════════════════════════════════
# log_step
# ═══════════════════════════════════════════════════════════════

class TestLogStep:
    """log_step 测试"""

    def test_log_step_output_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """log_step 应输出到 stderr，格式为 [TIMESTAMP] ▶ STEP_NAME"""
        log_step("初始化配置")
        captured = capsys.readouterr()
        assert captured.out == ""  # stdout 应为空
        assert "▶ 初始化配置" in captured.err

    def test_log_step_timestamp_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """时间戳格式应为 YYYY-MM-DD HH:MM:SS"""
        log_step("测试步骤")
        captured = capsys.readouterr()
        # 形如 [2026-05-17 20:20:00] ▶ 测试步骤
        import re
        assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ▶ 测试步骤", captured.err)
