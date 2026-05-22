#!/usr/bin/env python3
"""p3 — 项目三 CLI 工具

用法:
  p3 scan <target-dir>       — 对目标项目执行一次深度扫描（任意项目）
  p3 status                  — 查看项目三当前状态（轮次、成本、tier）
  p3 cost                    — 查看今日/近7天成本
  p3 setup <target-dir>      — 将目标目录注册为优化目标
  p3 targets                 — 列出所有注册的优化目标
  p3 cron [on|off]           — 开启/关闭自动循环
  p3 report                  — 最近优化报告
  p3 history [limit]         — 历史优化记录（面试展示用）
  p3 init-ci <target-dir>    — 生成 GitHub Actions CI 配置
  p3 help                    — 显示此帮助
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SWARM_DIR = Path(__file__).parent.resolve()
DATA_DIR = SWARM_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
TARGETS_FILE = DATA_DIR / "opt_target.txt"

# Ensure src/ is on sys.path for imports
SRC_DIR = SWARM_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SWARM_DIR) not in sys.path:
    sys.path.insert(0, str(SWARM_DIR))


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cmd_status(args):
    """查看系统状态"""
    state = _load_state()
    if not state:
        print("⚠️  state.json 未找到或无效")
        return 1

    print("╔══════════════════════════════════════╗")
    print("║     项目三：多Agent 状态面板        ║")
    print("╚══════════════════════════════════════╝")
    print(f"  当前轮次:     Round {state.get('current_round', '?')}")
    budget = state.get("daily_budget", {})
    print(f"  今日花费:     ${budget.get('dollar_spent_today', 0):.2f} / ${budget.get('dollar_limit', 5):.2f}")
    print(f"  当前级别:     {budget.get('tier', 'unknown').upper()}")
    if budget.get("readonly_mode"):
        print("  ⛔ 只读模式（已超预算上限）")
    last_scan = state.get("last_scan", {})
    if last_scan:
        print(f"  上次扫描:     {last_scan.get('target', '?')}")
        print(f"  扫描分数:     {last_scan.get('score_before', '?')} → {last_scan.get('score_after', '?')}")
        print(f"  发现问题:     {last_scan.get('total_issues', 0)}（严重: {last_scan.get('critical_issues', 0)}）")
        print(f"  修复尝试:     {last_scan.get('fixes_attempted', 0)}（成功: {last_scan.get('fixes_succeeded', 0)}）")
    return 0


def cmd_cost(args):
    """查看成本报告"""
    try:
        from src.infra.cost_tracker_db import get_today_spent, get_cost_trend
        today_spent = get_today_spent()
        today = datetime.now().strftime("%Y-%m-%d")

        print("╔══════════════════════════════════════╗")
        print("║     项目三：成本报告                 ║")
        print("╚══════════════════════════════════════╝")
        print(f"  今日累计:     ${today_spent:.2f}")
        print(f"  日预算:       $5.00")
        print(f"  剩余:         ${max(0, 5.0 - today_spent):.2f}")
        tier = "green"
        if today_spent >= 4.5: tier = "red"
        elif today_spent >= 2.0: tier = "yellow"
        print(f"  熔断级别:     {tier.upper()}")

        # 7-day trend
        trend = get_cost_trend(days=7)
        if trend:
            print(f"\n  近7天成本:")
            total_7d = 0
            for entry in trend:
                total_7d += entry.get("total", entry.get("cost", 0))
                marker = " ← 今天" if entry["date"] == today else ""
                print(f"    {entry['date']}: ${entry.get('total', entry.get('cost', 0)):.2f}{marker}")
            print(f"  7天合计:     ${total_7d:.2f}")
            print(f"  日均:        ${total_7d / max(len(trend), 1):.2f}")
        else:
            print("\n  暂无成本记录（系统尚未运行）")
    except Exception as e:
        print(f"⚠️  成本数据库不可用: {e}")
    return 0


def cmd_scan(args):
    """对任意目标目录执行深度扫描"""
    target = args.target_dir
    if not target:
        print("❌ 请指定目标目录: p3 scan <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        print(f"❌ 目标目录不存在: {target_path}")
        return 1

    print(f"🔍 正在扫描: {target_path}")
    print()

    try:
        from src.analysis.deep_enterprise_scanner import scan_deep
        result = scan_deep(str(target_path))
    except ImportError as e:
        print(f"⚠️  扫描引擎导入失败: {e}")
        print("   尝试直接用 subprocess 调用...")
        scan_script = SWARM_DIR / "src/analysis/deep_enterprise_scanner.py"
        proc = subprocess.run(
            [sys.executable, str(scan_script), str(target_path)],
            capture_output=True, text=True, timeout=120
        )
        print(proc.stdout or proc.stderr[:500])
        return 0 if proc.returncode == 0 else 1

    score = result.get("score", 0)
    issue_count = result.get("issue_count", 0)
    by_severity = result.get("by_severity", {})
    files_scanned = result.get("files_scanned", 0)
    issues = result.get("issues", [])

    print(f"📊 评分: {score}/100")
    print(f"📁 扫描文件: {files_scanned} 个")
    print(f"🐛 发现 {issue_count} 个问题:")
    for sev in ["critical", "high", "medium", "low"]:
        count = by_severity.get(sev, 0)
        if count:
            print(f"     [{sev.upper():8s}] {count} 个")
    print()

    # Print top issues
    if issues:
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_issues = sorted(issues, key=lambda x: sev_order.get(x.get("severity", "low"), 99))
        print("  前十问题列表:")
        for i in sorted_issues[:10]:
            print(f"    [{i.get('severity','?'):8s}] {i.get('type','?'):30s} {i.get('file','?')}:{i.get('line','?')}")
            desc = i.get('description', '')[:80]
            if desc:
                print(f"            {desc}")
        print()

    # Save report to target project
    docs_dir = target_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = docs_dir / f"p3_scan_{timestamp}.md"

    report_lines = [
        f"# P3 扫描报告 — {target_path.name}",
        f"",
        f"> 扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 引擎: deep_enterprise_scanner",
        f"",
        "---",
        f"",
        f"## 概览",
        f"",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 评分 | {score}/100 |",
        f"| 扫描文件 | {files_scanned} |",
        f"| 发现问题 | {issue_count} |",
        f"| Critical | {by_severity.get('critical', 0)} |",
        f"| High | {by_severity.get('high', 0)} |",
        f"| Medium | {by_severity.get('medium', 0)} |",
        f"| Low | {by_severity.get('low', 0)} |",
        f"",
        f"## 问题详情",
        f"",
    ]
    for i in sorted_issues:
        report_lines.append(f"### [{i.get('severity','?')}] {i.get('type','?')}")
        report_lines.append(f"")
        report_lines.append(f"- **文件**: `{i.get('file','?')}:{i.get('line','?')}`")
        report_lines.append(f"- **描述**: {i.get('description','?')}")
        if i.get('suggestion'):
            report_lines.append(f"- **建议**: {i['suggestion']}")
        report_lines.append(f"")

    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"✅ 报告已保存: {report_path}")
    print()

    # Record cost
    try:
        from src.infra.cost_tracker_db import record_cost
        record_cost(provider="deepseek", model="scan", cost=0.50, task_id=f"scan_{target_path.name}")
        print(f"💰 已记录扫描成本 $0.50")
    except Exception:
        pass

    return 0


def cmd_setup(args):
    """注册新目标"""
    target = args.target_dir
    if not target:
        print("❌ 请指定目标目录: p3 setup <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        print(f"❌ 目标目录不存在: {target_path}")
        return 1

    TARGETS_FILE.write_text(str(target_path), encoding="utf-8")
    print(f"✅ 已注册优化目标: {target_path}")
    print("   下次 cron 触发时将扫描此项目")
    print(f"\n💡 如需持久化，在 ~/.bashrc 中添加:")
    print(f'   export PROJECT1_DIR="{target_path}"')
    return 0


def cmd_targets(args):
    """列出所有目标"""
    if TARGETS_FILE.exists():
        target = TARGETS_FILE.read_text(encoding="utf-8").strip()
        print(f"  当前目标: {target}")
    else:
        print("  未注册目标")
    print("  提示: 用 p3 setup <target-dir> 注册新目标")

    # Also check self_evolve_log for historical targets
    log_file = DATA_DIR / "self_evolve_log.json"
    if log_file.exists():
        try:
            log = json.loads(log_file.read_text(encoding="utf-8"))
            rounds = log.get("rounds", [])
            targets_used = set()
            for r in rounds:
                t = r.get("target", "") or r.get("target_dir", "")
                if t:
                    targets_used.add(t)
            if targets_used:
                print(f"\n  历史扫描过的目标:")
                for t in sorted(targets_used):
                    print(f"    • {t}")
        except Exception:
            pass
    return 0


def cmd_cron(args):
    """控制 cron"""
    if args.action == "on":
        subprocess.run(["cronjob", "resume", "79cb9d06dc5d"], capture_output=True)
        print("✅ 项目三 cronjob 已恢复（每2小时）")
        print("   首次运行可能需要等2小时内的调度点")
    elif args.action == "off":
        subprocess.run(["cronjob", "pause", "79cb9d06dc5d"], capture_output=True)
        print("⏸️  项目三 cronjob 已暂停")
    else:
        print("  用法: p3 cron on|off")
    return 0


def cmd_report(args):
    """查看最近优化报告"""
    print("📊 最近优化报告:")
    found = False
    for days_back in range(7):
        d = datetime.now() - timedelta(days=days_back)
        report_file = SWARM_DIR / f"优化报告_{d.strftime('%Y%m%d')}.md"
        if report_file.exists():
            print(f"  📄 {report_file}（{days_back}天前）")
            found = True
            # Print first 10 lines as preview
            content = report_file.read_text(encoding="utf-8").split("\n")[:15]
            print()
            print("\n".join(content))
            break

    if not found:
        print("  近7天无优化报告")

    # Also check for p3 scan reports in the data dir
    print("\n📋 p3 扫描报告:")
    scan_reports = list(SWARM_DIR.glob("docs/p3_scan_*.md"))
    scan_reports += list(SWARM_DIR.glob("p3_scan_*.md"))
    if scan_reports:
        latest = max(scan_reports, key=lambda p: p.stat().st_mtime)
        print(f"  最新: {latest}")
    else:
        print("  暂无（运行 p3 scan <target-dir> 生成）")

    return 0


def cmd_history(args):
    """历史优化记录（面试展示用）"""
    limit = args.limit if args.limit else 20

    log_file = DATA_DIR / "self_evolve_log.json"
    if not log_file.exists():
        print("❌ 未找到历史记录 (data/self_evolve_log.json)")
        return 1

    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ 日志解析失败: {e}")
        return 1

    rounds = log.get("rounds", [])
    if not rounds:
        print("  暂无轮次记录")
        return 0

    # Stats
    total = len(rounds)
    success = sum(1 for r in rounds if r.get("result") == "success")
    failed = sum(1 for r in rounds if r.get("result") == "failed")
    total_added = sum(r.get("lines_added", 0) for r in rounds)
    total_removed = sum(r.get("lines_removed", 0) for r in rounds)
    scores = [r for r in rounds if r.get("score_before") is not None]

    print("╔══════════════════════════════════════╗")
    print("║     项目三：优化历史总览              ║")
    print("╚══════════════════════════════════════╝")
    print(f"  总轮次:       {total}")
    print(f"  成功:         {success} ({success/max(total,1)*100:.0f}%)")
    print(f"  失败:         {failed}")
    print(f"  总代码增减:   +{total_added} / -{total_removed}")
    if scores:
        before = sum(r.get("score_before", 0) for r in scores) / len(scores)
        after = sum(r.get("score_after", 0) for r in scores) / len(scores)
        print(f"  平均评分变化: {before:.0f} → {after:.0f}")
    print()

    # Diagnosis (面试亮点)
    diag = log.get("diagnosis", {})
    if diag:
        print("📈 诊断数据:")
        print(f"  成功率趋势: 近5轮 {diag.get('trend',{}).get('recent_5_success_rate',0)*100:.0f}%")
        print(f"  委托成功率: {diag.get('delegate_success_rate',0)*100:.0f}%")
        print(f"  累计代码量: +{diag.get('total_lines_added',0)} / -{diag.get('total_lines_removed',0)}")
        print()

    # Recent rounds (interview showcase)
    print(f"📋 最近 {min(limit, len(rounds))} 轮:")
    print(f"  {'轮次':>5} {'日期':<18} {'结果':<8} {'增减':<10} {'任务'}")
    print(f"  {'─'*5} {'─'*18} {'─'*8} {'─'*10} {'─'*40}")
    for r in reversed(rounds[-limit:]):
        ts = r.get("timestamp", "?")[:16]
        res = r.get("result", "?")
        added = r.get("lines_added", 0)
        removed = r.get("lines_removed", 0)
        delta = f"+{added}/-{removed}" if added or removed else ""
        task = (r.get("task", "") or "")[:55]
        print(f"  #{r.get('round','?'):>3} {ts:<18} {res:<8} {delta:<10} {task}")

    # Insights
    insights = log.get("accumulated_insights", {})
    if insights:
        print(f"\n🧠 经验积累:")
        for k, v in list(insights.items())[:3]:
            print(f"  • {v[:120]}...")
        if len(insights) > 3:
            print(f"  ...还有 {len(insights)-3} 条累积经验")

    return 0


def cmd_init_ci(args):
    """生成 GitHub Actions CI 配置"""
    target = args.target_dir
    if not target:
        print("❌ 请指定目标目录: p3 init-ci <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        print(f"❌ 目标目录不存在: {target_path}")
        return 1

    github_dir = target_path / ".github" / "workflows"
    github_dir.mkdir(parents=True, exist_ok=True)
    ci_path = github_dir / "p3-audit.yml"

    ci_content = f"""# P3 Code Quality Audit — 自动代码质量门禁
# 由项目三 (https://github.com/your-org/project3) 驱动
# 每次 push 和 PR 自动运行深度扫描

name: P3 Code Quality Audit

on:
  push:
    branches: [main, master, develop]
  pull_request:
    branches: [main, master]

jobs:
  audit:
    name: Code Quality Scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi

      - name: Run P3 Quality Scan
        run: |
          # 如果项目三在同一仓库，用相对路径；否则需要单独检出
          P3_DIR="${{{{ github.workspace }}}}/../project3"
          if [ -d "$P3_DIR" ]; then
            python3 "$P3_DIR/p3.py" scan "${{{{ github.workspace }}}}"
          else
            echo "项目三未检出，请先配置 P3_DIR 路径"
            echo "或手动运行: python3 /path/to/project3/p3.py scan ${{{{ github.workspace }}}}"
          fi

      - name: Upload Scan Report
        uses: actions/upload-artifact@v4
        with:
          name: p3-scan-report
          path: docs/p3_scan_*.md
          retention-days: 30
"""
    ci_path.write_text(ci_content, encoding="utf-8")
    print(f"✅ CI 配置已生成: {ci_path}")
    print(f"\n   将此文件 push 到 GitHub 后，每次 PR 会自动触发代码质量扫描。")
    print(f"   注意: 需要项目三也部署到 CI 环境，或在同一仓库内。")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="项目三：多Agent — 自进化代码质量引擎 CLI",
        usage="p3 <command> [options]"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="查看系统状态（轮次、成本、tier）")
    sub.add_parser("cost", help="查看成本报告（今日+近7天）")

    p_scan = sub.add_parser("scan", help="深度扫描任意项目")
    p_scan.add_argument("target_dir", nargs="?", help="目标项目路径（任意目录）")

    p_setup = sub.add_parser("setup", help="注册优化目标")
    p_setup.add_argument("target_dir", nargs="?", help="目标项目路径")

    sub.add_parser("targets", help="列出所有注册和历史目标")

    p_cron = sub.add_parser("cron", help="控制自动循环")
    p_cron.add_argument("action", nargs="?", choices=["on", "off"], help="on=开启 off=暂停")

    sub.add_parser("report", help="查看最近优化报告")

    p_history = sub.add_parser("history", help="历史优化记录（面试展示）")
    p_history.add_argument("limit", nargs="?", type=int, default=20, help="显示最近N轮（默认20）")

    p_init = sub.add_parser("init-ci", help="生成 GitHub Actions CI 配置")
    p_init.add_argument("target_dir", nargs="?", help="目标项目路径")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "cost": cmd_cost,
        "scan": cmd_scan,
        "setup": cmd_setup,
        "targets": cmd_targets,
        "cron": cmd_cron,
        "report": cmd_report,
        "history": cmd_history,
        "init-ci": cmd_init_ci,
    }

    if args.command in commands:
        return commands[args.command](args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
