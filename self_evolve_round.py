#!/usr/bin/env python3
"""
self_evolve_round.py — 自我进化循环协调者脚本。

作用：协调一轮完整的 A队→B队→协调者→Git 自我进化循环。
      现在 Hermes cronjob 负责实际调度，此脚本作为备用手动触发器。

用法：
  python self_evolve_round.py              # 发报告（不派发任务，只检查状态）
  python self_evolve_round.py --report     # 同上，生成详细状态报告
  python self_evolve_round.py --hermes-run # 通过 Hermes CLI 触发一轮

原理：Hermes 的 cronjob (swarm-evolve-round) 已接管实际调度，
      加载 orchestrate-swarm skill 后会自动 delegate_task 派发 A/B 队。
      此脚本仅作为备用/手动模式保留。
"""

import datetime
import json
import os
import subprocess
import sys

WORKDIR = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_DIR = os.path.join(WORKDIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def log(msg: str) -> None:
    print(f"[{TIMESTAMP}] {msg}")


def generate_status_report() -> dict:
    """
    generate_status_report — 生成项目三当前状态报告。

    作用：收集 TODO、CHANGELOG、磁盘文件、Git 状态，输出结构化报告。
    原理：不修改任何文件，只读操作，用于手动检查和 cronjob 诊断。
    返回值：dict 包含 timestamp, todos, changelog_last, git_status, last_round。
    """
    report = {
        "timestamp": TIMESTAMP,
        "workdir": WORKDIR,
    }

    # TODO 状态
    if os.path.exists(os.path.join(WORKDIR, "TODO.md")):
        with open(os.path.join(WORKDIR, "TODO.md")) as f:
            lines = f.read().split("\n")
        pending = [l.strip() for l in lines if l.strip().startswith("- [ ]")]
        completed = [l.strip() for l in lines if l.strip().startswith("- [x]")]
        report["todos_pending"] = len(pending)
        report["todos_completed"] = len(completed)
        report["todos_pending_list"] = pending
    else:
        report["todos_pending"] = 0
        report["todos_completed"] = 0
        report["todos_pending_list"] = []

    # CHANGELOG 最后一轮
    if os.path.exists(os.path.join(WORKDIR, "CHANGELOG.md")):
        with open(os.path.join(WORKDIR, "CHANGELOG.md")) as f:
            content = f.read()
        rounds = []
        for line in content.split("\n"):
            if "Round" in line and "—" in line:
                try:
                    r = int(line.split("Round")[1].split("—")[0].strip())
                    rounds.append(r)
                except (ValueError, IndexError):
                    pass
        report["last_round"] = max(rounds) if rounds else 0
    else:
        report["last_round"] = 0

    # Git 状态
    git_status = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True, text=True, cwd=WORKDIR, timeout=30
    )
    report["git_changes"] = git_status.stdout.strip() or "(clean)"

    # Git 最新 commit
    git_log = subprocess.run(
        ["git", "log", "--oneline", "-3"],
        capture_output=True, text=True, cwd=WORKDIR, timeout=30
    )
    report["git_last_3_commits"] = git_log.stdout.strip()

    return report


def format_report(report: dict) -> str:
    """格式化状态报告为可读文本。"""
    lines = [
        "=" * 50,
        f"  项目三：多Agent自我进化 — 状态报告",
        f"  时间: {report['timestamp']}",
        "=" * 50,
        "",
        f"最后完成轮次: Round {report['last_round']}",
        f"待办任务: {report['todos_pending']} 个",
        f"已完成: {report['todos_completed']} 个",
        "",
        "待办列表:",
    ]
    for t in report["todos_pending_list"]:
        lines.append(f"  ▢ {t}")
    lines.append("")
    lines.append("Git 状态:")
    lines.append(f"  {report['git_changes']}")
    lines.append("")
    lines.append("最近 3 次提交:")
    for c in report["git_last_3_commits"].split("\n"):
        lines.append(f"  {c}")
    lines.append("")
    lines.append("=" * 50)
    return "\n".join(lines)


def main():
    log("=" * 50)

    if "--hermes-run" in sys.argv:
        log("Hermes 模式被触发 — 协调者由 Hermes cronjob 管理，此脚本不派发任务。")
        log("Hermes cronjob (swarm-evolve-round) 已在运行中。")
        log("生成状态报告...")
        report = generate_status_report()
        print(format_report(report))
        log("完成 ✅")
        log("=" * 50)
        return

    # 默认模式：生成状态报告
    log("生成状态报告...")
    report = generate_status_report()
    print(format_report(report))

    # 保存 JSON 报告
    report_path = os.path.join(LOG_DIR, f"status_{TIMESTAMP}.json")
    with open(report_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"报告已保存: {report_path}")

    log("Hermes cronjob 负责实际任务派发。如需手动触发，使用 --hermes-run")
    log("=" * 50)


if __name__ == "__main__":
    main()
