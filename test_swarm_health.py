"""test_swarm_health.py — pytest 单元测试覆盖 swarm_health.py

测试对象：
- HeartbeatPinger (ping, read_last_heartbeat)
- HealthMonitor (check_agent, generate_report, report_as_dict, scan_agents)
- HealthReport (from_heartbeats, print_report, alive_count)
- CLI 入口
"""

import os
import sys
import json
import time
import pytest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm_health import (
    HeartbeatPinger,
    HealthMonitor,
    HealthReport,
    main as cli_main,
)


# ═══════════════════════════════════════════════════════════════
# HeartbeatPinger
# ═══════════════════════════════════════════════════════════════

class TestHeartbeatPinger:
    """HeartbeatPinger 单元测试"""

    def test_ping_creates_file(self, tmp_path: Path) -> None:
        """ping() 应创建心跳文件"""
        pinger = HeartbeatPinger(agent_id=1, heartbeat_dir=str(tmp_path))
        ok = pinger.ping()
        assert ok is True
        hb_file = tmp_path / "agent_1.json"
        assert hb_file.exists()

    def test_ping_content(self, tmp_path: Path) -> None:
        """ping() 生成的心跳文件应包含正确的字段"""
        pinger = HeartbeatPinger(agent_id=42, heartbeat_dir=str(tmp_path))
        pinger.ping()
        hb_file = tmp_path / "agent_42.json"
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        assert data["agent_id"] == 42
        assert "timestamp" in data
        assert "unix_time" in data
        assert isinstance(data["unix_time"], float)
        assert data["metadata"] is None

    def test_ping_with_metadata(self, tmp_path: Path) -> None:
        """ping() 可以携带 metadata"""
        pinger = HeartbeatPinger(agent_id=2, heartbeat_dir=str(tmp_path))
        meta = {"task": "training", "step": 5}
        ok = pinger.ping(metadata=meta)
        assert ok is True
        hb_file = tmp_path / "agent_2.json"
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        assert data["metadata"] == meta

    def test_ping_empty_metadata(self, tmp_path: Path) -> None:
        """传空 dict 作为 metadata 时，写入应为 None"""
        pinger = HeartbeatPinger(agent_id=3, heartbeat_dir=str(tmp_path))
        pinger.ping(metadata={})
        hb_file = tmp_path / "agent_3.json"
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        assert data["metadata"] is None  # 源码中：metadata or None

    def test_heartbeat_path_property(self, tmp_path: Path) -> None:
        """_heartbeat_path 应返回正确的路径"""
        pinger = HeartbeatPinger(agent_id=7, heartbeat_dir=str(tmp_path))
        expected = str(tmp_path / "agent_7.json")
        assert pinger._heartbeat_path == expected

    def test_read_last_heartbeat_normal(self, tmp_path: Path) -> None:
        """read_last_heartbeat 应返回最后写入的心跳数据"""
        pinger = HeartbeatPinger(agent_id=1, heartbeat_dir=str(tmp_path))
        pinger.ping(metadata={"status": "ok"})
        data = pinger.read_last_heartbeat()
        assert data is not None
        assert data["agent_id"] == 1
        assert data["metadata"] == {"status": "ok"}

    def test_read_last_heartbeat_not_found(self, tmp_path: Path) -> None:
        """心跳文件不存在时返回 None"""
        pinger = HeartbeatPinger(agent_id=999, heartbeat_dir=str(tmp_path))
        data = pinger.read_last_heartbeat()
        assert data is None

    def test_read_last_heartbeat_corrupted(self, tmp_path: Path) -> None:
        """损坏的心跳文件应返回 None"""
        pinger = HeartbeatPinger(agent_id=1, heartbeat_dir=str(tmp_path))
        hb_file = tmp_path / "agent_1.json"
        hb_file.write_text("{not valid json", encoding="utf-8")
        data = pinger.read_last_heartbeat()
        assert data is None

    def test_ping_fails_readonly_dir(self, tmp_path: Path) -> None:
        """向只读目录发心跳应返回 False"""
        ro_dir = tmp_path / "readonly_heartbeats"
        ro_dir.mkdir()
        ro_dir.chmod(0o444)
        try:
            pinger = HeartbeatPinger(agent_id=1, heartbeat_dir=str(ro_dir))
            ok = pinger.ping()
            assert ok is False
        finally:
            ro_dir.chmod(0o755)

    def test_ping_updates_timestamp(self, tmp_path: Path) -> None:
        """多次 ping 会更新时间戳"""
        pinger = HeartbeatPinger(agent_id=1, heartbeat_dir=str(tmp_path))
        pinger.ping()
        first_data = pinger.read_last_heartbeat()
        assert first_data is not None
        first_ts = first_data["unix_time"]

        time.sleep(0.01)  # 确保时间有变化
        pinger.ping()
        second_data = pinger.read_last_heartbeat()
        assert second_data is not None
        assert second_data["unix_time"] > first_ts


# ═══════════════════════════════════════════════════════════════
# HealthMonitor
# ═══════════════════════════════════════════════════════════════

class TestHealthMonitorCheckAgent:
    """HealthMonitor.check_agent 单元测试"""

    def _write_heartbeat(
        self, tmp_path: Path, agent_id: int, unix_time: float, timestamp: Optional[str] = None
    ) -> None:
        """辅助方法：写入一个指定时间的心跳文件。"""
        if timestamp is None:
            timestamp = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(unix_time)
            )
        data = {
            "agent_id": agent_id,
            "timestamp": timestamp,
            "unix_time": unix_time,
            "metadata": None,
        }
        hb_file = tmp_path / f"agent_{agent_id}.json"
        hb_file.write_text(json.dumps(data), encoding="utf-8")

    def test_check_agent_alive(self, tmp_path: Path) -> None:
        """最近有心跳（age ≤ timeout）→ alive"""
        self._write_heartbeat(tmp_path, 1, time.time() - 5)  # 5秒前
        monitor = HealthMonitor(
            agent_ids=[1], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(1)
        assert result["status"] == "alive"
        assert result["agent_id"] == 1
        assert result["unix_time"] is not None
        assert result["last_heartbeat"] is not None
        assert isinstance(result["age_seconds"], float)

    def test_check_agent_stale(self, tmp_path: Path) -> None:
        """心跳超时 but < 2x timeout → stale"""
        self._write_heartbeat(tmp_path, 2, time.time() - 45)  # 45秒前，timeout=30
        monitor = HealthMonitor(
            agent_ids=[2], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(2)
        assert result["status"] == "stale"

    def test_check_agent_dead_by_timeout(self, tmp_path: Path) -> None:
        """心跳远超 2x timeout → dead"""
        self._write_heartbeat(tmp_path, 3, time.time() - 120)  # 120秒前，timeout=30
        monitor = HealthMonitor(
            agent_ids=[3], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(3)
        assert result["status"] == "dead"

    def test_check_agent_file_not_found(self, tmp_path: Path) -> None:
        """心跳文件不存在 → dead"""
        monitor = HealthMonitor(
            agent_ids=[99], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(99)
        assert result["status"] == "dead"
        assert result["last_heartbeat"] is None
        assert result["unix_time"] is None
        assert result["age_seconds"] is None

    def test_check_agent_corrupted_file(self, tmp_path: Path) -> None:
        """损坏的心跳文件 → dead"""
        corrupted = tmp_path / "agent_5.json"
        corrupted.write_text("{bad json", encoding="utf-8")
        monitor = HealthMonitor(
            agent_ids=[5], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(5)
        assert result["status"] == "dead"

    def test_check_agent_missing_unix_time(self, tmp_path: Path) -> None:
        """心跳文件缺少 unix_time → dead"""
        hb_file = tmp_path / "agent_6.json"
        hb_file.write_text(
            json.dumps({"agent_id": 6, "timestamp": "2026-01-01T00:00:00"}),
            encoding="utf-8",
        )
        monitor = HealthMonitor(
            agent_ids=[6], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(6)
        assert result["status"] == "dead"


class TestHealthMonitorGenerateReport:
    """HealthMonitor.generate_report / report_as_dict / scan_agents 测试"""

    def _write_heartbeat(self, tmp_path: Path, agent_id: int, age_seconds: float = 5) -> None:
        """辅助：写入心跳文件，age_seconds 表示多久前。"""
        unix_time = time.time() - age_seconds
        data = {
            "agent_id": agent_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(unix_time)),
            "unix_time": unix_time,
            "metadata": None,
        }
        hb_file = tmp_path / f"agent_{agent_id}.json"
        hb_file.write_text(json.dumps(data), encoding="utf-8")

    def test_generate_report_multiple_agents(self, tmp_path: Path) -> None:
        """多 Agent 报告生成"""
        self._write_heartbeat(tmp_path, 1, 5)    # alive
        self._write_heartbeat(tmp_path, 2, 45)   # stale
        self._write_heartbeat(tmp_path, 3, 120)  # dead

        monitor = HealthMonitor(
            agent_ids=[1, 2, 3], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        report = monitor.generate_report()
        assert len(report) == 3
        assert report[0]["agent_id"] == 1
        assert report[0]["status"] == "alive"
        assert report[1]["agent_id"] == 2
        assert report[1]["status"] == "stale"
        assert report[2]["agent_id"] == 3
        assert report[2]["status"] == "dead"

    def test_generate_report_sorted(self, tmp_path: Path) -> None:
        """报告应按 agent_id 升序"""
        self._write_heartbeat(tmp_path, 3, 5)
        self._write_heartbeat(tmp_path, 1, 5)
        self._write_heartbeat(tmp_path, 2, 5)

        monitor = HealthMonitor(
            agent_ids=[3, 1, 2], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        report = monitor.generate_report()
        ids = [r["agent_id"] for r in report]
        assert ids == [1, 2, 3]

    def test_generate_report_with_custom_ids(self, tmp_path: Path) -> None:
        """generate_report 接受自定义 agent_ids 参数"""
        self._write_heartbeat(tmp_path, 1, 5)
        self._write_heartbeat(tmp_path, 2, 5)
        self._write_heartbeat(tmp_path, 3, 5)

        monitor = HealthMonitor(
            agent_ids=[1, 2, 3], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        # 只检查 agent_ids=[1, 3]
        report = monitor.generate_report(agent_ids=[1, 3])
        assert len(report) == 2
        assert report[0]["agent_id"] == 1
        assert report[1]["agent_id"] == 3

    def test_generate_report_empty_ids_without_dir(self, tmp_path: Path) -> None:
        """agent_ids 为空且目录不存在 → 空报告"""
        monitor = HealthMonitor(
            agent_ids=[], timeout_seconds=30, heartbeat_dir=str(tmp_path / "nonexistent")
        )
        report = monitor.generate_report()
        assert report == []

    def test_report_as_dict(self, tmp_path: Path) -> None:
        """report_as_dict 应以 agent_id 为键"""
        self._write_heartbeat(tmp_path, 1, 5)
        self._write_heartbeat(tmp_path, 2, 45)

        monitor = HealthMonitor(
            agent_ids=[1, 2], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        d = monitor.report_as_dict()
        assert isinstance(d, dict)
        assert 1 in d
        assert 2 in d
        assert d[1]["status"] == "alive"
        assert d[2]["status"] == "stale"

    def test_scan_agents_finds_files(self, tmp_path: Path) -> None:
        """scan_agents 应发现目录中的心跳文件"""
        for aid in [1, 3, 5]:
            (tmp_path / f"agent_{aid}.json").write_text("{}", encoding="utf-8")
        # 写入非心跳文件
        (tmp_path / "not_a_heartbeat.txt").write_text("xxx", encoding="utf-8")

        monitor = HealthMonitor(
            heartbeat_dir=str(tmp_path),
            agent_ids=[1],  # 合并显式 + 扫描
        )
        found = monitor.scan_agents()
        assert 1 in found
        assert 3 in found
        assert 5 in found

    def test_scan_agents_no_dir(self, tmp_path: Path) -> None:
        """目录不存在 → 空列表"""
        monitor = HealthMonitor(
            heartbeat_dir=str(tmp_path / "nonexistent"),
            agent_ids=[],
        )
        assert monitor.scan_agents() == []

    def test_generate_report_auto_scan(self, tmp_path: Path) -> None:
        """agent_ids 为空时自动扫描目录"""
        self._write_heartbeat(tmp_path, 1, 5)
        self._write_heartbeat(tmp_path, 2, 45)

        monitor = HealthMonitor(
            timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        # agent_ids 为空 → 应自动扫描
        report = monitor.generate_report()
        assert len(report) == 2

    def test_check_agent_boundary_alive_to_stale(self, tmp_path: Path) -> None:
        """age 略低于 timeout → alive（因为 > timeout 才是 stale）"""
        self._write_heartbeat(tmp_path, 1, 20)  # 20秒前，timeout=30，确保 alive
        monitor = HealthMonitor(
            agent_ids=[1], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(1)
        assert result["status"] == "alive"

    def test_check_agent_boundary_stale_to_dead(self, tmp_path: Path) -> None:
        """age 在 (timeout, 2*timeout) 之间 → stale"""
        self._write_heartbeat(tmp_path, 1, 50)  # 50秒前，timeout=30，30<50<60 → stale
        monitor = HealthMonitor(
            agent_ids=[1], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        result = monitor.check_agent(1)
        assert result["status"] == "stale"


# ═══════════════════════════════════════════════════════════════
# HealthReport
# ═══════════════════════════════════════════════════════════════

class TestHealthReport:
    """HealthReport 静态方法测试"""

    def _write_heartbeat(self, tmp_path: Path, agent_id: int, age_seconds: float = 5) -> None:
        unix_time = time.time() - age_seconds
        data = {
            "agent_id": agent_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(unix_time)),
            "unix_time": unix_time,
            "metadata": None,
        }
        (tmp_path / f"agent_{agent_id}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_from_heartbeats(self, tmp_path: Path) -> None:
        """from_heartbeats 应返回正确的报告"""
        self._write_heartbeat(tmp_path, 1, 5)
        self._write_heartbeat(tmp_path, 2, 45)

        report = HealthReport.from_heartbeats(
            agent_ids=[1, 2], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        assert len(report) == 2
        assert report[0]["status"] == "alive"
        assert report[1]["status"] == "stale"

    def test_alive_count(self, tmp_path: Path) -> None:
        """alive_count 应返回正确的存活数"""
        self._write_heartbeat(tmp_path, 1, 5)    # alive
        self._write_heartbeat(tmp_path, 2, 45)   # stale
        self._write_heartbeat(tmp_path, 3, 5)    # alive
        self._write_heartbeat(tmp_path, 4, 120)  # dead

        count = HealthReport.alive_count(
            agent_ids=[1, 2, 3, 4], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        assert count == 2

    def test_alive_count_zero(self, tmp_path: Path) -> None:
        """全部 dead 时 alive_count=0"""
        self._write_heartbeat(tmp_path, 1, 120)
        count = HealthReport.alive_count(
            agent_ids=[1], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        assert count == 0

    def test_print_report(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """print_report 应打印格式化的报告"""
        self._write_heartbeat(tmp_path, 1, 5)
        HealthReport.print_report(
            agent_ids=[1], timeout_seconds=30, heartbeat_dir=str(tmp_path)
        )
        captured = capsys.readouterr()
        assert "Swarm Health Report" in captured.out
        assert "Agent" in captured.out
        assert "ALIVE" in captured.out or "alive" in captured.out


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

class TestHealthCLI:
    """CLI main() 测试"""

    def test_cli_report_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认参数运行 CLI 报告模式"""
        monkeypatch.setattr("sys.argv", ["swarm_health.py"])
        cli_main()

    def test_cli_report_with_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """指定心跳目录"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_health.py", "--dir", str(tmp_path)],
        )
        cli_main()

    def test_cli_report_with_agents(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """指定 Agent ID 列表"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_health.py", "--agents", "1,2,3"],
        )
        cli_main()

    def test_cli_ping(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """发送心跳"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_health.py", "--ping", "1", "--dir", str(tmp_path)],
        )
        cli_main()
        hb_file = tmp_path / "agent_1.json"
        assert hb_file.exists()

    def test_cli_ping_with_meta(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """发送心跳并携带 metadata"""
        monkeypatch.setattr(
            "sys.argv",
            [
                "swarm_health.py",
                "--ping", "2",
                "--dir", str(tmp_path),
                "--meta", '{"task":"test"}',
            ],
        )
        cli_main()
        hb_file = tmp_path / "agent_2.json"
        assert hb_file.exists()
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        assert data["metadata"] == {"task": "test"}

    def test_cli_ping_invalid_meta(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """无效 metadata JSON 应打印错误"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_health.py", "--ping", "1", "--meta", "not-json"],
        )
        cli_main()
        captured = capsys.readouterr()
        assert "Invalid metadata" in captured.out

    def test_cli_report_with_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """指定超时阈值"""
        monkeypatch.setattr(
            "sys.argv",
            ["swarm_health.py", "--timeout", "60"],
        )
        cli_main()
