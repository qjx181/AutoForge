"""swarm_health.py — 多 Agent 心跳检测机制

提供三个核心工具类：
  - HeartbeatPinger: 定期发送心跳（写入心跳文件）
  - HealthMonitor: 监控各 Agent 的心跳状态，超时检测
  - HealthReport: 生成结构化健康状态报告

用法示例
--------
    from swarm_health import HeartbeatPinger, HealthMonitor

    # Agent 1 启动心跳
    pinger = HeartbeatPinger(agent_id=1)
    pinger.ping()

    # 协调者检查所有 Agent 状态
    monitor = HealthMonitor(agent_ids=[1, 2, 3], timeout_seconds=30)
    report = monitor.generate_report()
    for r in report:
        print(f"Agent {r['agent_id']}: {r['status']}")
"""

import os
import json
import datetime
import time
from typing import Dict, List, Optional, Union
from pathlib import Path


# ── 默认心跳目录（可在实例化时覆盖） ──────────────────────────────
DEFAULT_HEARTBEAT_DIR = os.path.join(os.path.dirname(__file__), "heartbeats")


# ═══════════════════════════════════════════════════════════════
# HeartbeatPinger
# ═══════════════════════════════════════════════════════════════
class HeartbeatPinger:
    """周期性写入心跳文件，向系统广播此 Agent 仍存活。

    Agent 进程定期调用 ``ping()``，每次调用都会刷新心跳文件的时间戳。
    心跳文件格式为 JSON，包含 agent_id、时间戳和可选元数据。

    Attributes:
        agent_id:       Agent 编号（int）
        heartbeat_dir:  心跳文件存放目录（str）
        _heartbeat_path: 当前 Agent 专属心跳文件路径（str, 只读属性）
    """

    def __init__(
        self,
        agent_id: int,
        heartbeat_dir: str = DEFAULT_HEARTBEAT_DIR,
    ) -> None:
        """初始化心跳发送器。

        Args:
            agent_id:     Agent 编号（用于命名心跳文件）。
            heartbeat_dir: 心跳文件目录（默认项目根下的 heartbeats/）。
        """
        self.agent_id = agent_id
        self.heartbeat_dir = heartbeat_dir

    @property
    def _heartbeat_path(self) -> str:
        """返回此 Agent 专属心跳文件路径。"""
        return os.path.join(self.heartbeat_dir, f"agent_{self.agent_id}.json")

    def ping(self, metadata: Optional[Dict[str, object]] = None) -> bool:
        """发送一次心跳 —— 写入/刷新心跳文件。

        心跳文件内容（JSON）:
        {
            "agent_id":       <int>,
            "timestamp":      <iso-format str>,
            "unix_time":      <float>,
            "metadata":       <dict or null>
        }

        Args:
            metadata: 附加元数据（如当前任务名、步骤数等），可选。

        Returns:
            写入成功 True，失败 False。
        """
        heartbeat = {
            "agent_id": self.agent_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "unix_time": time.time(),
            "metadata": metadata or None,
        }
        os.makedirs(self.heartbeat_dir, exist_ok=True)
        try:
            with open(self._heartbeat_path, "w", encoding="utf-8") as f:
                json.dump(heartbeat, f, ensure_ascii=False, indent=2)
            return True
        except (OSError, IOError):
            return False

    def read_last_heartbeat(self) -> Optional[Dict[str, object]]:
        """读取自身最近一次心跳内容。

        Returns:
            心跳字典（含 agent_id、timestamp 等），文件不存在或损坏时返回 None。
        """
        try:
            if not os.path.isfile(self._heartbeat_path):
                return None
            with open(self._heartbeat_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None


# ═══════════════════════════════════════════════════════════════
# HealthMonitor
# ═══════════════════════════════════════════════════════════════
class HealthMonitor:
    """监控多个 Agent 的心跳状态，基于超时阈值判定存活状况。

    从 ``heartbeat_dir`` 中读取各 Agent 的心跳文件，比较 ``unix_time``
    与当前时间的差值，判定状态。

    Attributes:
        agent_ids:       被监控的 Agent 编号列表（List[int]）。
        timeout_seconds: 超时阈值（秒），超过此值则判定为 ``stale`` 或 ``dead``。
        heartbeat_dir:   心跳文件目录（str）。
    """

    STATUS_ALIVE = "alive"
    STATUS_STALE = "stale"
    STATUS_DEAD = "dead"

    def __init__(
        self,
        agent_ids: Optional[List[int]] = None,
        timeout_seconds: int = 30,
        heartbeat_dir: str = DEFAULT_HEARTBEAT_DIR,
    ) -> None:
        """初始化健康监控器。

        Args:
            agent_ids:       被监控的 Agent ID 列表。
                              若为 ``None``，则自动扫描 ``heartbeat_dir`` 发现所有 Agent。
            timeout_seconds: 心跳超时阈值（秒）。
                              默认 30 秒。超过此值未更新 => stale；
                              超过 2 倍阈值未更新 => dead。
            heartbeat_dir:   心跳文件目录。
        """
        self.agent_ids = agent_ids or []
        self.timeout_seconds = timeout_seconds
        self.heartbeat_dir = heartbeat_dir

    # ── 公共方法 ─────────────────────────────────────────────

    def scan_agents(self) -> List[int]:
        """扫描心跳目录，发现所有有心跳文件的 Agent 编号。

        Returns:
            检测到的 Agent ID 列表（按编号升序）。
        """
        if not os.path.isdir(self.heartbeat_dir):
            return []
        found: List[int] = []
        try:
            for fname in sorted(os.listdir(self.heartbeat_dir)):
                if fname.startswith("agent_") and fname.endswith(".json"):
                    agent_str = fname[len("agent_"):-len(".json")]
                    if agent_str.isdigit():
                        found.append(int(agent_str))
        except OSError:
            pass
        if self.agent_ids:
            # 合并显式指定的 + 扫描发现的
            all_ids = set(self.agent_ids) | set(found)
            return sorted(all_ids)
        return found

    def check_agent(self, agent_id: int) -> Dict[str, object]:
        """检查单个 Agent 的健康状态。

        Args:
            agent_id: Agent 编号。

        Returns:
            状态字典：
            {
                "agent_id":       <int>,
                "last_heartbeat": <iso timestamp str> or None,
                "unix_time":      <float> or None,
                "status":         "alive" | "stale" | "dead",
                "age_seconds":    <float> or None
            }
        """
        path = os.path.join(self.heartbeat_dir, f"agent_{agent_id}.json")

        # 文件不存在
        if not os.path.isfile(path):
            return {
                "agent_id": agent_id,
                "last_heartbeat": None,
                "unix_time": None,
                "status": self.STATUS_DEAD,
                "age_seconds": None,
            }

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {
                "agent_id": agent_id,
                "last_heartbeat": None,
                "unix_time": None,
                "status": self.STATUS_DEAD,
                "age_seconds": None,
            }

        last_time = data.get("unix_time")
        timestamp = data.get("timestamp")
        if last_time is None or not isinstance(last_time, (int, float)):
            return {
                "agent_id": agent_id,
                "last_heartbeat": timestamp,
                "unix_time": last_time,
                "status": self.STATUS_DEAD,
                "age_seconds": None,
            }

        age = time.time() - last_time
        # dead: 超过 2 倍超时阈值
        if age > 2 * self.timeout_seconds:
            status = self.STATUS_DEAD
        # stale: 超过单倍超时阈值但未达 2 倍
        elif age > self.timeout_seconds:
            status = self.STATUS_STALE
        else:
            status = self.STATUS_ALIVE

        return {
            "agent_id": agent_id,
            "last_heartbeat": timestamp,
            "unix_time": last_time,
            "status": status,
            "age_seconds": round(age, 3),
        }

    def generate_report(
        self,
        agent_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, object]]:
        """生成所有 Agent 的健康状态报告。

        若未指定 ``agent_ids``，则自动使用初始化时的 ``self.agent_ids``；
        若两者均为空，则自动扫描目录发现 Agent。

        Args:
            agent_ids: 可选，指定要检查的 Agent ID 列表
                       （覆盖实例化时传入的列表）。

        Returns:
            每个 Agent 一条状态记录的列表，按 agent_id 升序排列。
        """
        ids = agent_ids if agent_ids is not None else self.agent_ids
        if not ids:
            ids = self.scan_agents()
        results = [self.check_agent(aid) for aid in ids]
        # 排序保序
        results.sort(key=lambda r: r["agent_id"])
        return results

    def report_as_dict(
        self,
        agent_ids: Optional[List[int]] = None,
    ) -> Dict[str, object]:
        """生成以 agent_id 为键的扁平化健康报告字典。

        与 ``generate_report`` 返回列表不同，此方法返回 dict，
        便于按 Agent ID 快速查找状态。

        Args:
            agent_ids: 同 ``generate_report``。

        Returns:
            {agent_id: status_dict, ...} 格式的字典。
        """
        reports = self.generate_report(agent_ids=agent_ids)
        return {r["agent_id"]: r for r in reports}


# ═══════════════════════════════════════════════════════════════
# HealthReport（便捷工厂）
# ═══════════════════════════════════════════════════════════════
class HealthReport:
    """生成全局健康状态报告的便捷入口。

    封装 ``HealthMonitor`` 的常见操作，提供一次性 ``from_heartbeats``
    类方法，直接读取目录生成报告。

    ……你也可以直接实例化 ``HealthMonitor`` 以获取更精细的控制能力。……
    """

    @staticmethod
    def from_heartbeats(
        agent_ids: Optional[List[int]] = None,
        timeout_seconds: int = 30,
        heartbeat_dir: str = DEFAULT_HEARTBEAT_DIR,
    ) -> List[Dict[str, object]]:
        """从心跳目录一次性生成健康报告。

        Args:
            agent_ids:       要检查的 Agent ID 列表（None=自动发现）。
            timeout_seconds: 超时阈值（秒），默认 30。
            heartbeat_dir:   心跳文件目录。

        Returns:
            同 ``HealthMonitor.generate_report()``。
        """
        monitor = HealthMonitor(
            agent_ids=agent_ids,
            timeout_seconds=timeout_seconds,
            heartbeat_dir=heartbeat_dir,
        )
        return monitor.generate_report()

    @staticmethod
    def print_report(
        agent_ids: Optional[List[int]] = None,
        timeout_seconds: int = 30,
        heartbeat_dir: str = DEFAULT_HEARTBEAT_DIR,
    ) -> None:
        """直接打印健康报告到标准输出。

        Args:
            agent_ids:       要检查的 Agent ID 列表（None=自动发现）。
            timeout_seconds: 超时阈值（秒）。
            heartbeat_dir:   心跳文件目录。
        """
        report = HealthReport.from_heartbeats(
            agent_ids=agent_ids,
            timeout_seconds=timeout_seconds,
            heartbeat_dir=heartbeat_dir,
        )
        print("=" * 60)
        print(f"  Swarm Health Report  |  {datetime.datetime.now().isoformat()}")
        print("=" * 60)
        for entry in report:
            age = entry.get("age_seconds")
            age_str = f"{age:.1f}s" if age is not None else "N/A"
            print(
                f"  Agent {entry['agent_id']:>3d}  |  "
                f"{entry['status'].upper():>5s}  |  "
                f"last: {entry['last_heartbeat'] or 'NEVER':>25s}  |  "
                f"age: {age_str}"
            )
        print("=" * 60)

    @staticmethod
    def alive_count(
        agent_ids: Optional[List[int]] = None,
        timeout_seconds: int = 30,
        heartbeat_dir: str = DEFAULT_HEARTBEAT_DIR,
    ) -> int:
        """统计当前存活的 Agent 数量（status == 'alive'）。

        Args:
            agent_ids:       要检查的 Agent ID 列表。
            timeout_seconds: 超时阈值（秒）。
            heartbeat_dir:   心跳文件目录。

        Returns:
            alive 状态的 Agent 数量。
        """
        report = HealthReport.from_heartbeats(
            agent_ids=agent_ids,
            timeout_seconds=timeout_seconds,
            heartbeat_dir=heartbeat_dir,
        )
        return sum(1 for r in report if r["status"] == "alive")


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    """CLI 入口：直接运行 ``python swarm_health.py`` 打印健康报告。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Swarm Health Monitor — 多 Agent 心跳检测",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="心跳超时阈值（秒），默认 30",
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=DEFAULT_HEARTBEAT_DIR,
        help=f"心跳文件目录（默认 {DEFAULT_HEARTBEAT_DIR}）",
    )
    parser.add_argument(
        "--agents",
        type=str,
        default="",
        help="要检查的 Agent ID，逗号分隔（如 '1,2,3'），默认自动发现",
    )
    parser.add_argument(
        "--ping",
        type=int,
        default=None,
        help="发送一次心跳（指定 Agent ID）",
    )
    parser.add_argument(
        "--meta",
        type=str,
        default="",
        help="与 --ping 配合使用的元数据 JSON 字符串",
    )
    args = parser.parse_args()

    # ── 发送心跳模式 ──
    if args.ping is not None:
        meta: Optional[Dict[str, object]] = None
        if args.meta:
            try:
                meta = json.loads(args.meta)
            except json.JSONDecodeError:
                print(f"Invalid metadata JSON: {args.meta}")
                return
        pinger = HeartbeatPinger(agent_id=args.ping, heartbeat_dir=args.dir)
        ok = pinger.ping(metadata=meta)
        print(f"Heartbeat for Agent {args.ping}: {'OK' if ok else 'FAILED'}")
        return

    # ── 报告模式 ──
    agent_ids: Optional[List[int]] = None
    if args.agents:
        agent_ids = [int(x.strip()) for x in args.agents.split(",") if x.strip()]

    HealthReport.print_report(
        agent_ids=agent_ids,
        timeout_seconds=args.timeout,
        heartbeat_dir=args.dir,
    )


if __name__ == "__main__":
    main()
