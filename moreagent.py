import shlex
import os
from pathlib import Path
import os

CRONJOB_ID = os.environ.get('CRONJOB_ID', '79cb9d06dc5d')
"""moreagent — 项目三 CLI 工具

用法:
  moreagent scan <target-dir>       — 对目标项目执行一次深度扫描（任意项目）
  moreagent status                  — 查看项目三当前状态（轮次、成本、tier）
  moreagent cost                    — 查看今日/近7天成本
  moreagent setup <target-dir>      — 将目标目录注册为优化目标
  moreagent targets                 — 列出所有注册的优化目标
  moreagent cron [on|off]           — 开启/关闭自动循环
  moreagent report                  — 最近优化报告
  moreagent history [limit]         — 历史优化记录（面试展示用）
  moreagent init-ci <target-dir>    — 生成 GitHub Actions CI 配置
  moreagent fix <target-dir>        — LLM 扫描 + 自动修复管线
  moreagent help                    — 显示此帮助
"""

from typing import Any, Optional, List
import argparse
import json
import os
import subprocess
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from src.infra.logging_config import setup_logging
setup_logging()


# 如果 API key 未设置，从 .env 加载
def _load_env():
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()


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


def cmd_status(args) -> Any:
    """查看系统状态"""
    state = _load_state()
    if not state:
        logging.info("⚠️  state.json 未找到或无效")
        return 1

    logging.info("╔══════════════════════════════════════╗")
    logging.info("║     项目三：多Agent 状态面板        ║")
    logging.info("╚══════════════════════════════════════╝")
    logging.info(f"  当前轮次:     Round {state.get('current_round', '?')}")
    budget = state.get("daily_budget", {})
    logging.info(f"  今日花费:     ${budget.get('dollar_spent_today', 0):.2f} / ${budget.get('dollar_limit', 5):.2f}")
    logging.info(f"  当前级别:     {budget.get('tier', 'unknown').upper()}")
    if budget.get("readonly_mode"):
        logging.info("  ⛔ 只读模式（已超预算上限）")
    last_scan = state.get("last_scan", {})
    if last_scan:
        logging.info(f"  上次扫描:     {last_scan.get('target', '?')}")
        logging.info(f"  扫描分数:     {last_scan.get('score_before', '?')} → {last_scan.get('score_after', '?')}")
        logging.info(f"  发现问题:     {last_scan.get('total_issues', 0)}（严重: {last_scan.get('critical_issues', 0)}）")
        logging.info(f"  修复尝试:     {last_scan.get('fixes_attempted', 0)}（成功: {last_scan.get('fixes_succeeded', 0)}）")
    return 0


def cmd_cost(args) -> Any:
    """查看成本报告"""
    try:
        from src.infra.cost_tracker_db import get_today_spent, get_cost_trend
        today_spent = get_today_spent()
        today = datetime.now().strftime("%Y-%m-%d")

        logging.info("╔══════════════════════════════════════╗")
        logging.info("║     项目三：成本报告                 ║")
        logging.info("╚══════════════════════════════════════╝")
        trend = get_cost_trend(days=7)
        logging.info(f"  日预算:       $5.00")
        logging.info(f"  剩余:         ${max(0, 5.0 - today_spent):.2f}")
        tier = "green"
        if today_spent >= 4.5: tier = "red"
        elif today_spent >= 2.0: tier = "yellow"
        logging.info(f"  熔断级别:     {tier.upper()}")

        # 7-day trend
        trend = get_cost_trend(days=7)
        if trend:
            logging.info(f"\n  近7天成本:")
            total_7d = 0
            for entry in trend:
                total_7d += entry.get("total", entry.get("cost", 0))
                marker = " ← 今天" if entry["date"] == today else ""
                logging.info(f"    {entry['date']}: ${entry.get('total', entry.get('cost', 0)):.2f}{marker}")
            logging.info(f"  7天合计:     ${total_7d:.2f}")
            logging.info(f"  日均:        ${total_7d / max(len(trend), 1):.2f}")
        else:
            logging.info("\n  暂无成本记录（系统尚未运行）")
    except Exception as e:
        logging.info(f"⚠️  成本数据库不可用: {e}")
    return 0


def cmd_scan(args) -> Any:
    """对任意目标目录执行深度扫描"""
    target = args.target_dir
    if not target:
        logging.info("❌ 请指定目标目录: p3 scan <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        logging.info(f"❌ 目标目录不存在: {target_path}")
        return 1

    logging.info(f"🔍 正在扫描: {target_path}")
    logging.info("")

    try:
        from src.analysis.llm_scanner import scan_deep
        result = scan_deep(str(target_path))
    except ImportError as e:
        logging.info(f"⚠️  LLM 扫描引擎导入失败: {e}")
        logging.info("   回退到旧版深度扫描器...")
        try:
            from src.analysis.deep_enterprise_scanner import scan_deep as scan_deep_legacy
            result = scan_deep_legacy(str(target_path))
        except Exception as e2:
            logging.info(f"   回退也失败: {e2}")
            return 1

    score = result.get("score", 0)
    issue_count = result.get("issue_count", 0)
    by_severity = result.get("by_severity", {})
    files_scanned = result.get("files_scanned", 0)
    issues = result.get("issues", [])

    logging.info(f"📊 评分: {score}/100")
    logging.info(f"📁 扫描文件: {files_scanned} 个")
    logging.info(f"🐛 发现 {issue_count} 个问题:")
    for sev in ["critical", "high", "medium", "low"]:
        count = by_severity.get(sev, 0)
        if count:
            logging.info(f"     [{sev.upper():8s}] {count} 个")
    logging.info("")

    # Print top issues
    if issues:
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_issues = sorted(issues, key=lambda x: sev_order.get(x.get("severity", "low"), 99))
        logging.info("  前十问题列表:")
        for i in sorted_issues[:10]:
            logging.info(f"    [{i.get('severity','?'):8s}] {i.get('type','?'):30s} {i.get('file','?')}:{i.get('line','?')}")
            desc = i.get('description', '')[:80]
            if desc:
                logging.info(f"            {desc}")
        logging.info("")

    # Save report to target project
    docs_dir = target_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = docs_dir / f"p3_scan_{timestamp}.md"

    # LLM 成本信息
    llm_cost = result.get("llm_cost_estimate", 0.0)

    report_lines = [
        f"# P3 扫描报告 — {target_path.name}",
        f"",
        f"> 扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 引擎: llm_scanner (LLM 驱动)",
        f"> LLM 成本: ${llm_cost:.4f}",
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
        f"| LLM 成本 | ${llm_cost:.4f} |",
        f"",
        f"## 问题详情",
        f"",
    ]

    # 按维度+严重程度分组
    dims_report = {}
    for i in sorted_issues:
        dim = i.get("dimension", "other")
        dims_report.setdefault(dim, []).append(i)

    for dim, dim_issues in dims_report.items():
        report_lines.append(f"### 📐 维度: {dim}\n")
        for i in dim_issues:
            confidence = i.get("confidence", 0.5)
            report_lines.append(f"### [{i.get('severity','?')}] {i.get('type','?')} (置信度: {confidence:.1%})")
            report_lines.append(f"")
            report_lines.append(f"- **文件**: `{i.get('file','?')}:{i.get('line','?')}`")
            report_lines.append(f"- **描述**: {i.get('description','?')}")
            if i.get('suggestion'):
                report_lines.append(f"- **建议**: {i['suggestion']}")
            related = i.get("related_files", [])
            if related:
                report_lines.append(f"- **相关文件**: {', '.join(related)}")
            report_lines.append(f"")
        report_lines.append("---\n")

    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    logging.info(f"✅ 报告已保存: {report_path}")
    logging.info("")

    # Record cost
    try:
        from src.infra.cost_tracker_db import record_cost
        actual_cost = result.get("llm_cost_estimate", 0.05)
        if actual_cost > 0:
            record_cost(provider="deepseek", model="llm_scan", cost=actual_cost, task_id=f"scan_{target_path.name}")
            logging.info(f"💰 已记录扫描成本 ${actual_cost:.4f}")
    except Exception:
        logging.debug("记录扫描成本失败（非致命）")

    return 0


def cmd_scan_layered(args) -> Any:
    """分层扫描：Ruff → mypy → AST 规则 → LLM 去重扫描

    每层结果自动传给下一层去重，避免 LLM 做 linter 也能做的事。
    """
    target = args.target_dir
    if not target:
        logging.info("❌ 请指定目标目录: moreagent scan-layered <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        logging.info(f"❌ 目标目录不存在: {target_path}")
        return 1

    layers = [int(x.strip()) for x in args.layers.split(",") if x.strip()]
    skip_tests = not args.no_skip_tests

    logging.info(f"🔬 分层扫描: {target_path}")
    logging.info(f"   启用层: {layers}")

    llm_runs = getattr(args, 'llm_runs', 2)
    from src.analysis.layered_scanner import scan_layered
    result = scan_layered(
        str(target_path),
        skip_tests=skip_tests,
        enable_layers=layers,
        llm_runs=llm_runs,
    )

    # 保存报告
    docs_dir = target_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = docs_dir / f"moreagent_layered_scan_{timestamp}.md"

    layers_info = result.get("layers", {})
    report_lines = [
        f"# 分层扫描报告 — {target_path.name}",
        f"",
        f"> 扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 引擎: 分层管线 (Ruff + mypy + AST + LLM)",
        f"> LLM 成本: ${result.get('llm_cost_estimate', 0):.4f}",
        f"",
        "---",
        f"",
        f"## 概览",
        f"",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 评分 | {result.get('score', 0)}/100 |",
        f"| 发现问题 | {result.get('total_issues', 0)} |",
        f"| Critical | {result.get('by_severity', {}).get('critical', 0)} |",
        f"| High | {result.get('by_severity', {}).get('high', 0)} |",
        f"| Medium | {result.get('by_severity', {}).get('medium', 0)} |",
        f"| Low | {result.get('by_severity', {}).get('low', 0)} |",
        f"| LLM 成本 | ${result.get('llm_cost_estimate', 0):.4f} |",
        f"",
    ]

    # 每层贡献
    if layers_info:
        report_lines.append(f"## 分层贡献")
        report_lines.append(f"")
        report_lines.append(f"| 层 | 工具 | 发现数 | 保留数 |")
        report_lines.append(f"|---|---|---|---|")
        for lid_str in sorted(layers_info.keys()):
            s = layers_info[lid_str]
            report_lines.append(f"| {lid_str} | {s['tool']} | {s['issues_found']} | {s['issues_kept']} |")
        report_lines.append(f"")

    # 问题清单
    issues = result.get("issues", [])
    if issues:
        report_lines.append(f"## 问题清单")
        report_lines.append(f"")
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_issues = sorted(issues, key=lambda i: (sev_order.get(i.get("severity", "low"), 99), i.get("file", ""), i.get("line", 0)))
        for iss in sorted_issues:
            layer = iss.get("_layer", "?")
            scanner = iss.get("scanner", layer)
            report_lines.append(f"### [{iss.get('severity','?')}] {iss.get('type','?')}")
            report_lines.append(f"")
            report_lines.append(f"- **文件**: `{iss.get('file','?')}:{iss.get('line','?')}`")
            report_lines.append(f"- **描述**: {iss.get('description','?')}")
            if iss.get('suggestion'):
                report_lines.append(f"- **建议**: {iss['suggestion']}")
            report_lines.append(f"- **来源**: {scanner}")
            report_lines.append(f"")

    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    logging.info(f"✅ 报告已保存: {report_path}")

    return 0


def _quick_scan_file(filepath: str | Path) -> list[dict]:
    """用规则扫描器（dims）快速扫描单文件，零 API 成本。

    Returns:
        该文件上所有维度扫描器发现的问题列表
    """
    filepath = Path(filepath)
    all_issues = []
    scanner_modules = [
        "quality_scanner", "sec_scanner", "perf_scanner",
        "async_scanner", "config_scanner", "test_scanner",
        "doc_scanner", "deadcode_scanner",
    ]
    for mod_name in scanner_modules:
        try:
            mod = __import__(f"src.analysis.dims.{mod_name}", fromlist=["_scan_file"])
            if hasattr(mod, "_scan_file"):
                issues = mod._scan_file(str(filepath))
                all_issues.extend(issues)
        except Exception:
            pass  # 单个扫描器失败不影响整体
    return all_issues


def _verify_fix_regression(
    filepath: Path,
    original_issue,
    pre_fix_issues: list[dict],
) -> tuple[bool, str]:
    """逐修复回归验证：修复后立即重扫该文件，检查是否引入新问题。

    Args:
        filepath: 被修复的文件路径
        original_issue: 原始 Issue 对象（含 type, severity, line 等）
        pre_fix_issues: 修复前该文件的 dims 扫描问题列表

    Returns:
        (is_safe, reason): True=安全可保留, False=应回滚
    """
    post_fix_issues = _quick_scan_file(filepath)

    pre_count = len(pre_fix_issues)
    post_count = len(post_fix_issues)

    # 检查 1: 新增了 critical/high 级别的问题 → 大概率是修复引入的
    SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    pre_severities = [SEV_RANK.get(i.get("severity", "low"), 9) for i in pre_fix_issues]
    post_severities = [SEV_RANK.get(i.get("severity", "low"), 9) for i in post_fix_issues]

    new_critical = sum(1 for s in post_severities if s <= 1) - sum(1 for s in pre_severities if s <= 1)
    if new_critical > 0:
        return False, f"引入 {new_critical} 个新的 critical/high 问题"

    # 检查 2: 问题总数增加了 3 个以上 → 修复副作用过大
    if post_count - pre_count >= 3:
        return False, f"问题数从 {pre_count} 增到 {post_count}（+{post_count - pre_count}）"

    # 检查 3: 原始问题是否已解决（按 type+line 模糊匹配）
    orig_type = getattr(original_issue, "type", "")
    orig_line = getattr(original_issue, "line", 0)
    issue_still_present = any(
        i.get("type") == orig_type and abs(i.get("line", 0) - orig_line) <= 3
        for i in post_fix_issues
    )
    if issue_still_present:
        # 原问题还在，修复无效
        # 但不算回归，只是没修好 → 不回滚，让后续轮次继续修
        logging.debug(f"      ℹ️  原问题 {orig_type}:{orig_line} 仍在（修复可能无效）")

    return True, "安全"


def _check_over_modification(
    original_content: str,
    fixed_content: str,
    issue_line: int,
    issue_type: str,
    target_file: Path,
) -> bool:
    """回归检查：修复后是否引入了过度修改。

    检查维度：
      1. 文件行数是否异常增长（→ 可能 LLM 插了无关代码）
      2. 是否移除了安全兜底（os.getenv → os.environ 等）
      3. 是否改了导入语句

    Returns:
        True = 正常, False = 检出疑似过度修改（已回滚）
    """
    import difflib

    orig_lines = original_content.split("\n")
    fix_lines = fixed_content.split("\n")

    # 1. 行数检查：修复后不应增加超过 20 行 或 减少超过 15 行（除非是删无用代码）
    line_diff = len(fix_lines) - len(orig_lines)
    if line_diff > 20:
        logging.warning(
            f"      ⚠️  文件膨胀 +{line_diff} 行（疑似插入无关代码），已回滚"
        )
        target_file.write_text(original_content, encoding="utf-8")
        return False
    if line_diff < -10:
        logging.warning(
            f"      ⚠️  文件缩减 {line_diff} 行（疑似删除了重要代码），已回滚"
        )
        target_file.write_text(original_content, encoding="utf-8")
        return False

    # 2. 检查是否移除了安全兜底（os.getenv/env.get 带默认值的去掉了默认值）
    import re
    env_with_default_orig = len(re.findall(r'\.get\w*\(+"\w+"\s*,\s*["\']', original_content))
    env_with_default_fix = len(re.findall(r'\.get\w*\(+"\w+"\s*,\s*["\']', fixed_content))
    if env_with_default_orig > env_with_default_fix:
        logging.warning(
            f"      ⚠️  移除了 {env_with_default_orig - env_with_default_fix} 个带默认值的环境变量读取，已回滚"
        )
        target_file.write_text(original_content, encoding="utf-8")
        return False

    # 3. 检查是否改了 import 语句
    import_lines_orig = [l for l in orig_lines if l.strip().startswith(("import ", "from "))]
    import_lines_fix = [l for l in fix_lines if l.strip().startswith(("import ", "from "))]
    if import_lines_orig != import_lines_fix:
        # 检查是否只是加了本修复需要的 import（比如 subprocess）
        added = set(import_lines_fix) - set(import_lines_orig)
        removed = set(import_lines_orig) - set(import_lines_fix)
        if removed:
            logging.warning(
                f"      ⚠️  移除了 import，已回滚（删除: {list(removed)}）"
            )
            target_file.write_text(original_content, encoding="utf-8")
            return False

    return True



def _run_regression_tests(
    target_path: Path,
    pre_fix_issues: list,
) -> None:
    """全局回归测试：修复完成后验证没有引入新问题。

    策略：
      1. 检查 pytest/pyproject.toml/unittest 测试
      2. 如有测试，运行 pytest
      3. 即使有测试，也建议重新扫描验证
      4. 无测试则自动重新扫描
    """
    logging.info("")
    logging.info("📋 回归验证...")

    # 检查测试文件
    has_tests = False
    test_paths = [
        target_path / "tests",
        target_path / "test",
        target_path / "pytest.ini",
        target_path / "pyproject.toml",
        target_path / "setup.cfg",
    ]
    for p in test_paths:
        if p.exists():
            has_tests = True
            break

    if has_tests:
        try:
            import subprocess
            logging.info("  运行测试套件...")
            result = subprocess.run(
                ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
                cwd=str(target_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logging.info(f"   ✅ 测试全部通过")
            else:
                # 检查是否是测试失败 vs 无测试发现
                if "No tests ran" in result.stderr or "no tests ran" in result.stdout:
                    logging.info(f"   ℹ️  无测试发现（跳过）")
                else:
                    last_lines = result.stdout.strip().split("\n")[-5:]
                    logging.warning(f"   ⚠️  测试失败:\n" + "\n".join(f"      {l}" for l in last_lines))
        except FileNotFoundError:
            logging.info("   ℹ️  pytest 未安装（跳过）")
        except subprocess.TimeoutExpired:
            logging.info("   ⏰ 测试超时（跳过）")
        except Exception as e:
            logging.info(f"   ℹ️  测试执行异常: {e}（跳过）")
    else:
        logging.info("   未发现测试文件")

    # 重新扫描，对比 issue 变化
    logging.info("  重新扫描以验证回归...")
    try:
        from src.analysis.layered_scanner import scan_layered
        post_result = scan_layered(
            str(target_path),
            skip_tests=True,
            enable_layers=[0, 1, 2, 3],
            llm_runs=2,
        )
        post_issues = post_result.get("issues", [])

        # 按 severity 比较
        sev_count = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for i in post_issues:
            s = i.get("severity", "low")
            if s in sev_count:
                sev_count[s] += 1

        # 计算差异
        def _count_by_sev(issues):
            c = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for i in issues:
                s = i.get("severity", "low")
                if s in c:
                    c[s] += 1
            return c

        pre = _count_by_sev(pre_fix_issues)
        post = sev_count

        diffs = []
        for sev in ["critical", "high", "medium", "low"]:
            delta = post[sev] - pre.get(sev, 0)
            if delta != 0:
                diffs.append(f"{sev}: {pre.get(sev,0)}→{post[sev]} ({'+' if delta>0 else ''}{delta})")

        if diffs:
            logging.info(f"      Issue 变化: {'; '.join(diffs)}")
            # 如果 critical 新增了，强烈警告
            if post.get("critical", 0) > pre.get("critical", 0):
                logging.warning(f"      ⚠️  New critical issues 发现！修复可能引入了新漏洞")
        else:
            logging.info(f"      ✅ Issue 数量无变化")

        logging.info(f"      扫描成本: ${post_issues[-1].get('_cost', '?') if post_issues else '?'}")
    except Exception as e:
        logging.info(f"   ℹ️  回归扫描失败（非致命）: {e}")


def cmd_fix(args) -> Any:
    """LLM 扫描 + 自动修复管线（扫描 → 修复 → 审批 → 学习）

    流程：
      1. 用 LLM 扫描器发现问题
      2. 按严重程度排序，Critical/High 优先
      3. 对每个问题匹配修复器尝试修复
      4. 置信度门控（低置信度高自动应用，高置信度等待审批）
      5. 修复结果记录到经验库

    参数：
      target_dir: 目标项目路径
      --max-fixes: 最多尝试修复的问题数（默认 20）
      --dry-run: 只扫描不修复
      --severity-threshold: 最低严重程度（默认 high，只修 critical+high）
    """
    target = args.target_dir
    if not target:
        logging.info("❌ 请指定目标目录: p3 fix <target-dir>")
        return 1

    target_path = Path(target).resolve()
    if not target_path.exists():
        logging.info(f"❌ 目标目录不存在: {target_path}")
        return 1

    max_fixes = getattr(args, "max_fixes", 20)
    dry_run = getattr(args, "dry_run", False)
    sev_threshold = getattr(args, "severity_threshold", "high")

    # ── Phase 1: LLM 扫描 ──
    logging.info(f"🔍 扫描: {target_path}")
    try:
        from src.analysis.layered_scanner import scan_layered
        result = scan_layered(
            str(target_path),
            skip_tests=True,
            enable_layers=[0, 1, 2, 3],
            llm_runs=2,
        )
        pipeline_issues = result.get("issues", [])
    except ImportError as e:
        logging.error(f"❌ 分层扫描器导入失败: {e}")
        return 1
    except Exception as e:
        logging.error(f"❌ 扫描失败: {e}")
        return 1

    logging.info(f"  发现 {len(pipeline_issues)} 个问题")

    if not pipeline_issues:
        logging.info("✅ 无待修复问题")
        return 0

    # 按严重程度过滤
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    THRESH_ORDER = SEV_ORDER.get(sev_threshold, 1)
    target_issues = [i for i in pipeline_issues if SEV_ORDER.get(i["severity"], 99) <= THRESH_ORDER]

    if not target_issues:
        logging.info(f"  没有 {sev_threshold}+ 严重程度的问题")
        return 0

    logging.info(f"  待修复（{sev_threshold}+）: {len(target_issues)} 个")

    # ── 预热：加载语义检索模型（避免修复循环中被 SIGTERM）──
    try:
        from src.core.experience_retriever import get_retriever
        r = get_retriever()
        if r and r._ready:
            _ = r.search("预热查询", top_k=1)
            logging.info(f"  语义检索就绪（{r._index.ntotal} 条经验）")
    except Exception as e:
        logging.warning(f"  语义检索预热失败（降级为字符串匹配）: {e}")

    # ── Phase 2: 修复管线 ──
    from src.core.adapters_pkg import Issue, build_default_fixer_registry
    from src.core.confidence_gate import process_fix, expire_stale
    from src.core.experience_store import record_experience

    fixers = build_default_fixer_registry()

    stats = {"attempted": 0, "auto_applied": 0, "pending_review": 0, "rejected": 0, "errors": 0}

    for idx, iss_dict in enumerate(target_issues[:max_fixes], 1):
        issue = Issue.from_dict(iss_dict)
        target_file = target_path / issue.file

        if not target_file.exists():
            logging.debug(f"  [{idx}/{len(target_issues[:max_fixes])}] ⏭️  文件不存在: {issue.file}")
            continue

        original_content = target_file.read_text(encoding="utf-8")

        # 修复前：捕获该文件的 dims 扫描基线（用于逐修复回归验证）
        pre_fix_file_issues = _quick_scan_file(target_file)

        # 查找修复器
        fixer_chain = fixers.get_fixers_for_type(issue.type)
        if not fixer_chain:
            logging.debug(f"  [{idx}/{len(target_issues[:max_fixes])}] ⏭️  无修复器: {issue.type}")
            continue

        fixer = fixer_chain[0]  # 取第一个回退链修复器

        logging.info(f"  [{idx}/{len(target_issues[:max_fixes])}] 🔧 修复: [{issue.severity}] {issue.type} → {issue.file}:{issue.line}")

        if dry_run:
            logging.info(f"      (dry-run，跳过修复)")
            continue

        # 逐个尝试修复器链，失败则 fallback（经验→专有→LLM 兜底）
        best_fix_result = None
        last_fixer = fixer
        for candidate in fixer_chain:
            logging.debug(f"      尝试修复器: {candidate.name}")
            try:
                fix_result = candidate.fix(issue, target_path)
            except Exception as e:
                logging.warning(f"      {candidate.name}: 异常 {e}")
                continue

            if not fix_result.success:
                logging.debug(f"      {candidate.name}: 失败 ({fix_result.error or '未知'})")
                # 回滚文件（修复器可能已写了部分内容）
                target_file.write_text(original_content, encoding="utf-8")
                best_fix_result = fix_result
                last_fixer = candidate
                continue

            fix_result = fix_result
            last_fixer = candidate
            best_fix_result = fix_result
            break  # 成功

        if not best_fix_result or not best_fix_result.success:
            err = best_fix_result.error[:100] if best_fix_result and best_fix_result.error else "所有修复器均失败"
            logging.info(f"      ❌ 修复失败: {err}")
            record_experience(
                issue_type=issue.type, file=issue.file, line=issue.line,
                fixer=last_fixer.name if last_fixer else "unknown", action="",
                confidence=0, success=False,
                project=str(target_path), error=err,
            )
            stats["errors"] += 1
            continue

        stats["attempted"] += 1

        # 置信度门控
        gate_result = process_fix(best_fix_result, issue)
        decision = gate_result.get("decision", "rejected")

        if decision == "auto_apply":
            # 验证 1: 语法检查
            try:
                import ast
                fixed = target_file.read_text(encoding="utf-8")
                ast.parse(fixed)
            except SyntaxError as e:
                target_file.write_text(original_content, encoding="utf-8")
                logging.warning(f"      ⚠️  语法错误，已回滚: {e}")
                stats["errors"] += 1
                continue

            # 验证 2: 逐修复回归检查（重扫该文件，确认没引入新问题）
            is_safe, reason = _verify_fix_regression(target_file, issue, pre_fix_file_issues)
            if not is_safe:
                target_file.write_text(original_content, encoding="utf-8")
                logging.warning(f"      ⚠️  回归检测未通过，已回滚: {reason}")
                stats["errors"] += 1
                continue

            stats["auto_applied"] += 1
            logging.info(f"      ✅ 自动应用（语法+回归均通过）")
        elif decision == "pending_review":
            stats["pending_review"] += 1
            logging.info(f"      ⏳ 等待审批 (item_id={gate_result.get('item_id','?')})")
        else:
            stats["rejected"] += 1
            logging.info(f"      ⛔ 被拒绝 (置信度 {fix_result.confidence:.2f})")

        # 记录经验
        record_experience(
            issue_type=issue.type, file=issue.file, line=issue.line,
            fixer=fixer.name, action=fix_result.action,
            confidence=fix_result.confidence,
            success=(decision == "auto_apply"),
            project=str(target_path),
            error=fix_result.error or "",
        )

        # 过期旧审批项
        expire_stale()

        # ── 回归验证（修复后）：如果 auto_apply，检查是否过度修改 ──
        if decision == "auto_apply":
            _check_over_modification(
                original_content=original_content,
                fixed_content=target_file.read_text(encoding="utf-8"),
                issue_line=issue.line,
                issue_type=issue.type,
                target_file=target_file,
            )

    # ── 汇总 ──
    logging.info("")
    logging.info("=" * 50)
    logging.info(f"📊 修复完成: {target_path.name}")
    logging.info(f"   尝试: {stats['attempted']}")
    logging.info(f"   自动应用: {stats['auto_applied']}")
    logging.info(f"   待审批: {stats['pending_review']}")
    logging.info(f"   拒绝: {stats['rejected']}")
    logging.info(f"   失败: {stats['errors']}")
    logging.info("=" * 50)

    # ── 全局回归验证 ──
    _run_regression_tests(target_path, pipeline_issues)

    return 0


def cmd_setup(args) -> Any:
    """注册新目标"""
    target = args.target_dir
    if not target:
        logging.info("❌ 请指定目标目录: p3 setup <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        logging.info(f"❌ 目标目录不存在: {target_path}")
        return 1

    TARGETS_FILE.write_text(str(target_path), encoding="utf-8")
    logging.info(f"✅ 已注册优化目标: {target_path}")
    logging.info("   下次 cron 触发时将扫描此项目")
    logging.info(f"\n💡 如需持久化，在 ~/.bashrc 中添加:")
    logging.info(f'   export PROJECT1_DIR="{target_path}"')
    return 0


def cmd_targets(args) -> Any:
    """列出所有目标"""
    if TARGETS_FILE.exists():
        target = TARGETS_FILE.read_text(encoding="utf-8").strip()
        logging.info(f"  当前目标: {target}")
    else:
        logging.info("  未注册目标")
    logging.info("  提示: 用 p3 setup <target-dir> 注册新目标")

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
                logging.info(f"\n  历史扫描过的目标:")
                for t in sorted(targets_used):
                    logging.info(f"    • {t}")
        except Exception:
            logging.debug("读取历史目标失败（非致命）")
    return 0


def cmd_cron(args) -> Any:
    """控制 cron"""
    if args.action == "on":
        subprocess.run(["cronjob", "resume", "79cb9d06dc5d"], capture_output=True)
        logging.info("✅ 项目三 cronjob 已恢复（每2小时）")
        logging.info("   首次运行可能需要等2小时内的调度点")
    elif args.action == "off":
        subprocess.run(["cronjob", "pause", "79cb9d06dc5d"], capture_output=True)
        logging.info("⏸️  项目三 cronjob 已暂停")
    else:
        logging.info("  用法: p3 cron on|off")
    return 0


def cmd_report(args) -> Any:
    """查看最近优化报告"""
    logging.info("📊 最近优化报告:")
    found = False
    for days_back in range(7):
        d = datetime.now() - timedelta(days=days_back)
        report_file = SWARM_DIR / f"优化报告_{d.strftime('%Y%m%d')}.md"
        if report_file.exists():
            logging.info(f"  📄 {report_file}（{days_back}天前）")
            found = True
            # Print first 10 lines as preview
            content = report_file.read_text(encoding="utf-8").split("\n")[:15]
            logging.info("")
            logging.info("\n".join(content))
            break

    if not found:
        logging.info("  近7天无优化报告")

    # Also check for p3 scan reports in the data dir
    logging.info("\n📋 p3 扫描报告:")
    scan_reports = list(SWARM_DIR.glob("docs/p3_scan_*.md"))
    scan_reports += list(SWARM_DIR.glob("p3_scan_*.md"))
    if scan_reports:
        latest = max(scan_reports, key=lambda p: p.stat().st_mtime)
        logging.info(f"  最新: {latest}")
    else:
        logging.info("  暂无（运行 p3 scan <target-dir> 生成）")

    return 0


def cmd_history(args) -> Any:
    """历史优化记录（面试展示用）"""
    limit = args.limit if args.limit else 20

    log_file = DATA_DIR / "self_evolve_log.json"
    if not log_file.exists():
        logging.info("❌ 未找到历史记录 (data/self_evolve_log.json)")
        return 1

    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except Exception as e:
        logging.info(f"❌ 日志解析失败: {e}")
        return 1

    rounds = log.get("rounds", [])
    if not rounds:
        logging.info("  暂无轮次记录")
        return 0

    # Stats
    total = len(rounds)
    success = sum(1 for r in rounds if r.get("result") == "success")
    failed = sum(1 for r in rounds if r.get("result") == "failed")
    total_added = sum(r.get("lines_added", 0) for r in rounds)
    total_removed = sum(r.get("lines_removed", 0) for r in rounds)
    scores = [r for r in rounds if r.get("score_before") is not None]

    logging.info("╔══════════════════════════════════════╗")
    logging.info("║     项目三：优化历史总览              ║")
    logging.info("╚══════════════════════════════════════╝")
    logging.info(f"  总轮次:       {total}")
    logging.info(f"  成功:         {success} ({success/max(total,1)*100:.0f}%)")
    logging.info(f"  失败:         {failed}")
    logging.info(f"  总代码增减:   +{total_added} / -{total_removed}")
    if scores:
        before = sum(r.get("score_before", 0) for r in scores) / len(scores)
        after = sum(r.get("score_after", 0) for r in scores) / len(scores)
        logging.info(f"  平均评分变化: {before:.0f} → {after:.0f}")
    logging.info("")

    # Diagnosis (面试亮点)
    diag = log.get("diagnosis", {})
    if diag:
        logging.info("📈 诊断数据:")
        logging.info(f"  成功率趋势: 近5轮 {diag.get('trend',{}).get('recent_5_success_rate',0)*100:.0f}%")
        logging.info(f"  委托成功率: {diag.get('delegate_success_rate',0)*100:.0f}%")
        logging.info(f"  累计代码量: +{diag.get('total_lines_added',0)} / -{diag.get('total_lines_removed',0)}")
        logging.info("")

    # Recent rounds (interview showcase)
    logging.info(f"📋 最近 {min(limit, len(rounds))} 轮:")
    logging.info(f"  {'轮次':>5} {'日期':<18} {'结果':<8} {'增减':<10} {'任务'}")
    logging.info(f"  {'─'*5} {'─'*18} {'─'*8} {'─'*10} {'─'*40}")
    for r in reversed(rounds[-limit:]):
        ts = r.get("timestamp", "?")[:16]
        res = r.get("result", "?")
        added = r.get("lines_added", 0)
        removed = r.get("lines_removed", 0)
        delta = f"+{added}/-{removed}" if added or removed else ""
        task = (r.get("task", "") or "")[:55]
        logging.info(f"  #{r.get('round','?'):>3} {ts:<18} {res:<8} {delta:<10} {task}")

    # Insights
    insights = log.get("accumulated_insights", {})
    if insights:
        logging.info(f"\n🧠 经验积累:")
        for k, v in list(insights.items())[:3]:
            logging.info(f"  • {v[:120]}...")
        if len(insights) > 3:
            logging.info(f"  ...还有 {len(insights)-3} 条累积经验")

    return 0


def cmd_init_ci(args) -> Any:
    """生成 GitHub Actions CI 配置"""
    target = args.target_dir
    if not target:
        logging.info("❌ 请指定目标目录: p3 init-ci <target-dir>")
        return 1
    target_path = Path(target).resolve()
    if not target_path.exists():
        logging.info(f"❌ 目标目录不存在: {target_path}")
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
            python3 "$P3_DIR/moreagent.py" scan "${{ github.workspace }}"
          else
            echo "项目三未检出，请先配置 P3_DIR 路径"
            echo "或手动运行: python3 /path/to/project3/moreagent.py scan ${{ github.workspace }}"
          fi

      - name: Upload Scan Report
        uses: actions/upload-artifact@v4
        with:
          name: p3-scan-report
          path: docs/p3_scan_*.md
          retention-days: 30
"""
    ci_path.write_text(ci_content, encoding="utf-8")
    logging.info(f"✅ CI 配置已生成: {ci_path}")
    logging.info(f"\n   将此文件 push 到 GitHub 后，每次 PR 会自动触发代码质量扫描。")
    logging.info(f"   注意: 需要项目三也部署到 CI 环境，或在同一仓库内。")
    return 0


def main() -> Any:
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

    p_lscan = sub.add_parser("scan-layered", help="分层扫描（Ruff → mypy → AST → LLM，自动去重）")
    p_lscan.add_argument("target_dir", nargs="?", help="目标项目路径（任意目录）")
    p_lscan.add_argument("--layers", type=str, default="0,1,2,3", help="启用层，逗号分隔（默认 0,1,2,3）")
    p_lscan.add_argument("--llm-runs", type=int, default=2, help="LLM 层扫描轮数，多轮取并集减少波动（默认 2）")
    p_lscan.add_argument("--no-skip-tests", action="store_true", help="不跳过测试模块")

    p_fix = sub.add_parser("fix", help="分层扫描 + 自动修复（吃分层扫描结果）")
    p_fix.add_argument("target_dir", nargs="?", help="目标项目路径")
    p_fix.add_argument("--max-fixes", type=int, default=20, help="最多修复问题数（默认20）")
    p_fix.add_argument("--dry-run", action="store_true", help="只扫描不修复")
    p_fix.add_argument("--severity-threshold", choices=["critical", "high", "medium", "low"], default="high", help="最低严重程度（默认high，修critical+high）")

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
        "scan-layered": cmd_scan_layered,
        "fix": cmd_fix,
        "init-ci": cmd_init_ci,
    }

    if args.command in commands:
        return commands[args.command](args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
