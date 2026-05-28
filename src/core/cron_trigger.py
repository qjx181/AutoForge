"""cron_trigger.py — Cronjob 入口脚本

用法（在 crontab 中配置）：
    */30 * * * * python3 /path/to/cron_trigger.py

功能：
  1. 检查 PID 文件，防止多实例并发
  2. 调用 self_evolve_round.py 主循环
  3. 将 stdout/stderr 重定向到日志文件
  4. 支持环境变量 PROJECT1_DIR 配置

环境变量：
  PROJECT1_DIR   — 项目一目录（用于 git 同步 + Bug 管道扫描）
  SWARM_DIR      — 项目三根目录（默认自动检测）
  LOG_DIR        — 日志输出目录（默认 <SWARM_DIR>/logs）
"""

import os
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import sys
import subprocess
import datetime
from pathlib import Path
def get_project_root() -> Path:
    """自动检测项目根目录。"""
    env_root = os.environ.get("SWARM_DIR", "").strip()
    if env_root:
        p = Path(env_root)
        if p.exists():
            return p

    return Path(__file__).parent.parent.parent.resolve()


def check_pid(pid_file: Path) -> bool:
    """检查 PID 文件，返回 True 表示可以运行，False 表示已有实例在跑。"""
    if not pid_file.exists():
        return True

    try:
        existing_pid = int(pid_file.read_text().strip())
        try:
            os.kill(existing_pid, 0)  # 信号 0 不发送任何信号，只检查存活
            print(f"[cron_trigger] 实例仍在运行 (PID {existing_pid})，跳过")
            return False
        except OSError:
            print(f"[cron_trigger] 发现过期 PID 文件 (PID {existing_pid})，清除后继续")
            pid_file.unlink()
            return True
    except (ValueError, OSError):
        pid_file.unlink()
        return True


def write_pid(pid_file: Path) -> None:
    """写入当前进程 PID。"""
    pid_file.write_text(str(os.getpid()))


def main() -> int:
    root = get_project_root()
    log_dir = Path(os.environ.get("LOG_DIR", str(root / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"cron_trigger_{today}.log"
    pid_file = root / ".self_evolve_round.pid"

    timestamp = datetime.datetime.now().isoformat()
    header = f"\n{'='*60}\n  cron_trigger @ {timestamp}\n{'='*60}\n"

    if not check_pid(pid_file):
        return 0

    write_pid(pid_file)

    script = root / "src" / "core" / "self_evolve_round.py"
    if not script.exists():
        print(f"[cron_trigger] ERROR: self_evolve_round.py 不存在: {script}")
        return 1

    env = os.environ.copy()
    env["SWARM_DIR"] = str(root)

    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟超时
        )

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(header)
            f.write(f"Exit code: {result.returncode}\n")
            if result.stdout:
                f.write(f"STDOUT:\n{result.stdout}\n")
            if result.stderr:
                f.write(f"STDERR:\n{result.stderr}\n")
            f.write(f"Finished @ {datetime.datetime.now().isoformat()}\n\n")

        if result.returncode == 0:
            print(f"[cron_trigger] 轮次完成 (exit 0)，日志: {log_file}")
        else:
            print(f"[cron_trigger] 轮次异常 (exit {result.returncode})，日志: {log_file}")
            print(f"  stderr: {result.stderr[:200]}")

        return result.returncode

    except subprocess.TimeoutExpired:
        msg = f"[cron_trigger] 超时（600s），日志: {log_file}"
        print(msg)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(header)
            f.write("TIMEOUT: 超过 600s 限制\n\n")
        return 124
    except Exception as e:
        msg = f"[cron_trigger] 异常: {e}"
        print(msg)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(header)
            f.write(f"EXCEPTION: {e}\n\n")
        return 1
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
