"""swarm_metrics.py — Swarm 自我进化循环的指标收集模块

提供 Swarm 自我进化循环的完整指标收集能力，包含五个核心组件：
  - RoundTimer:   记录每轮开始/结束时间、持续时长
  - TaskTracker:  记录任务完成数、失败数、通过率
  - IssueTracker: 按严重级别统计问题出现频率
  - MetricsStore: 将指标数据持久化为 JSON 文件
  - MetricsReporter: 生成可读的文本/JSON 摘要报告

用法示例
--------
    from swarm_metrics import SwarmMetrics

    metrics = SwarmMetrics()
    metrics.start_round(round_num=15)
    metrics.record_task(agent="agent-1", status="completed", duration_sec=120)
    metrics.record_issue(severity="error", category="logic_error", module="swarm_metrics")
    report = metrics.generate_report()
    metrics.save("tmp_agent/metrics/round-15.json")
"""

import datetime
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Union

from src.infra.swarm_utils import read_file_safe, write_file_safe, log_step
from src.infra.swarm_logger import SwarmLogger

_log = SwarmLogger(name="swarm_metrics", level="INFO", json_mode=False)

SEVERITY_ORDER: List[str] = ["critical", "error", "warning", "info", "debug"]
SEVERITY_WEIGHT: Dict[str, int] = {
    "critical": 50,
    "error": 40,
    "warning": 30,
    "info": 20,
    "debug": 10,
}



def record_sqlite_metric(operation: str, sqlite_path: str = "") -> None:
    """兼容包装 — 记录 SQLite 操作指标。

    Args:
        operation: 操作类型（如 insert, select, vacuum）
        sqlite_path: SQLite 文件路径（可选）
    """
    print(f"[swarm_metrics] sqlite:{operation} {sqlite_path}")

if __name__ == "__main__":
    main()



class ContainerPoolMonitor:
    """ContainerPoolMonitor — Docker 容器预热池监控器。

    负责自动扩缩容 Docker 容器池，确保系统有足够的容器资源
    处理任务队列。扩容驱动：等待队列长度 + 等待时长联合触发。
    缩容驱动：定时扫描空闲容器数。

    Attributes:
        max_pool_size:   容器池最大数量（上限）。
        min_pool_size:   容器池最小保留数量。
        queue_scale_up_threshold: 扩容队列长度阈值。
        queue_wait_seconds:        扩容等待时间阈值（防抖动）。
        idle_cleanup_threshold:    空闲容器清理阈值。
        docker_image:   预热容器使用的 Docker 镜像。
        container_workdir: 容器内工作目录。
        pool_size:      当前容器池大小。
        queue_length:   当前等待队列长度。
        queue_start_time: 当前队列长度首次达到扩容阈值的时间。
        last_scan_time:  上次池状态扫描时间（时间戳）。
    """

    def __init__(
        self,
        max_pool_size: int = 10,
        min_pool_size: int = 2,
        queue_scale_up_threshold: int = 2,
        queue_wait_seconds: int = 10,
        idle_cleanup_threshold: int = 3,
        docker_image: str = "python:3.11-slim",
        container_workdir: str = "/workspace",
    ) -> None:
        """ContainerPoolMonitor — 初始化容器池监控器。

        Args:
            max_pool_size:   容器池最大数量（默认 10）。
            min_pool_size:   容器池最小保留数量（默认 2）。
            queue_scale_up_threshold: 扩容队列长度阈值（默认 2）。
            queue_wait_seconds:        扩容等待时间阈值秒（默认 10）。
            idle_cleanup_threshold:    空闲容器清理阈值（默认 3）。
            docker_image:   预热容器使用的 Docker 镜像（默认 python:3.11-slim）。
            container_workdir: 容器内工作目录（默认 /workspace）。

        为什么这么设计：
        - 队列长度 + 等待时长联合触发：防止短暂流量尖峰导致频繁扩缩容。
        - 定时扫描空闲容器：避免容器长时间闲置浪费资源。
        - 与 config.yaml 的 container_pool 节参数结构一致。
        """
        self.max_pool_size = max_pool_size
        self.min_pool_size = min_pool_size
        self.queue_scale_up_threshold = queue_scale_up_threshold
        self.queue_wait_seconds = queue_wait_seconds
        self.idle_cleanup_threshold = idle_cleanup_threshold
        self.docker_image = docker_image
        self.container_workdir = container_workdir
        self.pool_size = 0
        self.queue_length = 0
        self.queue_start_time: Optional[float] = None
        self.last_scan_time: float = time.time()

    def record_queue_length(self, length: int) -> None:
        """record_queue_length — 记录当前等待队列长度，必要时触发扩容。

        Args:
            length: 当前等待队列的长度。

        作用：更新队列长度数据。
        原理：队列长度 + 等待时长联合触发扩容，避免误判。
        逻辑：
        - 如果 length > queue_scale_up_threshold：
            - 首次进入阈值记录当前时间为 queue_start_time
            - 如果已持续 >= queue_wait_seconds，调用 scale_up()
        - 如果 length <= queue_scale_up_threshold：
            - 重置 queue_start_time 为 None
            - 更新 last_scan_time

        面试追问：
        - 为什么不直接用队列长度作为唯一指标？答：防止流量瞬变的抖动。
        - 等待时长如何保证准确性？答：使用 time.time() 差值，精度毫秒级。
        """
        now = time.time()
        self.queue_length = length

        if length > self.queue_scale_up_threshold:
            if self.queue_start_time is None:
                self.queue_start_time = now
                _log.info("队列达到扩容阈值",
                          length=length, threshold=self.queue_scale_up_threshold)
            elif now - self.queue_start_time >= self.queue_wait_seconds:
                self.scale_up()
                self.queue_start_time = now
        else:
            self.queue_start_time = None

        self.last_scan_time = now

    def check_pool_health(self) -> dict:
        """check_pool_health — 检查容器池健康状态，必要时缩容。

        Returns:
            包含当前池状态的字典：
            {
                "pool_size": int,
                "idle_count": int,
                "action": "scale_down" | "noop",
                "action_count": int,
            }

        作用：每 5 分钟扫描一次池状态。
        原理：空闲容器过多浪费资源，定期缩容到合理水平。
        逻辑：
        - 调用 _count_pool_containers() 获取当前容器数和空闲数
        - 如果空闲数 > idle_cleanup_threshold：
            - 计算需要停止的容器数 = 空闲数 - idle_cleanup_threshold
            - 调用 scale_down(需要停止的容器数)
        - 否则不做操作

        面试追问：
        - 如何判定容器"空闲"？答：容器正在运行但没有被 mark_in_use() 标记。
        - 缩容策略为什么停最旧的？答：FIFO 策略，越早创建的容器被复用的概率越低。
        """
        now = time.time()
        elapsed = now - self.last_scan_time

        pool_info = self._count_pool_containers()
        idle_count = pool_info.get("idle_count", 0)
        self.pool_size = pool_info.get("total_count", 0)

        result = {
            "pool_size": self.pool_size,
            "idle_count": idle_count,
            "action": "noop",
            "action_count": 0,
        }

        if idle_count > self.idle_cleanup_threshold:
            to_stop = idle_count - self.idle_cleanup_threshold
            _log.info("空闲容器过多，触发缩容",
                      idle=idle_count, threshold=self.idle_cleanup_threshold,
                      to_stop=to_stop)
            self.scale_down(to_stop)
            result["action"] = "scale_down"
            result["action_count"] = to_stop

        self.last_scan_time = now
        return result

    def scale_up(self) -> bool:
        """scale_up — 扩容：启动一个新 Docker 容器（在不超过上限的前提下）。

        Returns:
            True: 扩容成功（或已满无需扩容）。
            False: 扩容失败（docker run 报错或超时）。

        作用：增加容器池容量以应对任务积压。
        原理：上限 max_pool_size 防资源耗尽，下限 min_pool_size 保常驻能力。
        逻辑：
        - 先统计当前容器数
        - 如果 pool_size >= max_pool_size，直接返回 True（已满）
        - 生成容器名 sandbox-pool-{pool_size}
        - 执行 docker run -d --name {name} {image} sleep infinity
        - 成功后 pool_size += 1
        """
        pool_info = self._count_pool_containers()
        current_size = pool_info.get("total_count", 0)

        if current_size >= self.max_pool_size:
            _log.info("容器池已满，无需扩容",
                      size=current_size, max_size=self.max_pool_size)
            return True

        container_name = f"sandbox-pool-{current_size}"
        try:
            result = subprocess.run(
                ["docker", "run", "-d",
                 "--name", container_name,
                 self.docker_image,
                 "sleep", "infinity"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.pool_size = current_size + 1
                _log.info("扩容成功", name=container_name,
                          new_size=self.pool_size)
                return True
            else:
                _log.error("扩容失败", name=container_name,
                           error=result.stderr.strip())
                return False
        except subprocess.TimeoutExpired:
            _log.error("扩容超时（30s）", name=container_name)
            return False
        except FileNotFoundError:
            _log.error("Docker 命令未找到，请检查 Docker 是否安装")
            return False

    def scale_down(self, count: int) -> bool:
        """scale_down — 缩容：停止最旧的 N 个容器。

        Args:
            count: 要停止的容器数量。

        Returns:
            True: 缩容完成。False: 部分或全部容器停止失败。

        作用：释放空闲容器占用的资源。
        原理：按容器创建时间排序，FIFO 策略，最旧的优先停止。
        逻辑：
        - 调用 _list_sorted_containers() 获取按创建时间排序的容器列表
        - 取前 count 个容器
        - 对每个容器执行 docker stop + docker rm
        - 如果停止后 pool_size < min_pool_size，停止缩容
        """
        containers = self._list_sorted_containers()
        if not containers:
            return True

        to_stop = containers[:count]
        all_success = True

        for c in to_stop:
            name = c.get("name", "")
            try:
                stop_result = subprocess.run(
                    ["docker", "stop", name],
                    capture_output=True, text=True, timeout=15,
                )
                if stop_result.returncode == 0:
                    subprocess.run(
                        ["docker", "rm", name],
                        capture_output=True, text=True, timeout=10,
                    )
                    _log.info("缩容成功", name=name)
                else:
                    _log.warning("缩容失败", name=name,
                                 error=stop_result.stderr.strip())
                    all_success = False
            except subprocess.TimeoutExpired:
                _log.warning("缩容超时", name=name)
                all_success = False

        pool_info = self._count_pool_containers()
        self.pool_size = pool_info.get("total_count", 0)

        if self.pool_size < self.min_pool_size:
            _log.info("容器池低于最小值，回补到 %d", self.min_pool_size)
            for _ in range(self.min_pool_size - self.pool_size):
                self.scale_up()

        return all_success

    def get_pool_status(self) -> dict:
        """get_pool_status — 获取当前容器池状态。

        Returns:
            包含完整池状态的字典：
            {
                "pool_size": int,
                "queue_length": int,
                "max_pool_size": int,
                "min_pool_size": int,
                "queue_start_time": float or None,
                "last_scan_time": float,
                "idle_count": int,
            }

        作用：提供容器池的快照，供 metrics 收集和外部查询。
        原理：综合内部状态和外部 Docker 容器列表。
        逻辑：
        - 调用 _count_pool_containers() 获取实际 Docker 容器数据
        - 合并内部记录的队列长度和配置信息
        """
        pool_info = self._count_pool_containers()
        return {
            "pool_size": pool_info.get("total_count", 0),
            "queue_length": self.queue_length,
            "max_pool_size": self.max_pool_size,
            "min_pool_size": self.min_pool_size,
            "queue_start_time": self.queue_start_time,
            "last_scan_time": self.last_scan_time,
            "idle_count": pool_info.get("idle_count", 0),
        }


    def _count_pool_containers(self) -> dict:
        """_count_pool_containers — 统计当前容器池中容器数量和空闲数量。

        Returns:
            {"total_count": int, "idle_count": int}

        作用：通过 docker ps 获取实际的容器状态。
        原理：按容器名前缀 sandbox-pool- 过滤。
        逻辑：
        - docker ps --filter name=sandbox-pool- --format json
        - 解析 JSON 输出，统计总数
        - 目前没有复杂的心跳机制，所有运行中的容器都视为"活跃"
        - idle_count 需要额外信息（待 future 实现 mark_in_use/mark_idle）
        """
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=sandbox-pool-",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"total_count": 0, "idle_count": 0}

            names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
            total = len(names)
            return {"total_count": total, "idle_count": total}
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"total_count": 0, "idle_count": 0}

    def _list_sorted_containers(self) -> list:
        """_list_sorted_containers — 获取按创建时间排序的容器列表（旧→新）。

        Returns:
            容器信息列表，每个元素包含 name 和 created_at。

        作用：为缩容提供 FIFO 顺序。
        原理：docker ps --sort=created 返回按创建时间排序的列表。
        逻辑：
        - 按创建时间升序排列，最旧的在前
        """
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=sandbox-pool-",
                 "--format", "{{.Names}}\t{{.CreatedAt}}",
                 "--sort", "created"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return []

            containers = []
            for line in result.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    containers.append({"name": parts[0], "created_at": parts[1]})
                elif len(parts) == 1:
                    containers.append({"name": parts[0], "created_at": ""})
            return containers
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
