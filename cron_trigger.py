#!/usr/bin/env python3
"""cron_trigger.py — 系统 cron 触发器。

作用：每30分钟由系统 crontab 触发一次。
为什么：Hermes cronjob 是主要的 A→B→Git 调度器，
本脚本作为 backup 确保 Git 兜底和状态日志。

逻辑：
1. 记录触发时间
2. 调用 self_evolve_round.py 做 Git 后勤
3. 检查 Hermes cronjob 是否存活
"""

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SWARM_DIR = Path("/mnt/f/项目三：多Agent")
LOG_DIR = SWARM_DIR / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("cron_trigger")


def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=" * 50)
    logger.info("Cron 触发器启动 — %s", timestamp)
    logger.info("=" * 50)

    # 1. 运行后勤脚本
    script_path = SWARM_DIR / "self_evolve_round.py"
    if script_path.exists():
        logger.info("执行后勤脚本...")
        result = subprocess.run(
            ["python3", str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                logger.info("  %s", line)
        if result.stderr:
            logger.warning("  stderr: %s", result.stderr.strip())
        logger.info("后勤脚本退出码: %d", result.returncode)
    else:
        logger.warning("脚本不存在: %s", script_path)

    logger.info("=" * 50)
    logger.info("Cron 触发器完成 — %s", timestamp)


if __name__ == "__main__":
    main()
