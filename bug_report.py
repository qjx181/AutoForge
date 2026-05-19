#!/usr/bin/env python3
"""bug_report.py — Bug 提交和查询 CLI

命令行工具，用于向 bug 分析引擎提交错误日志、查询历史记录、自动生成修复。

用法:
  python bug_report.py --submit "错误日志文本"     # 从参数直接提交
  python bug_report.py --file /tmp/error.log       # 从文件读取后提交
  python bug_report.py --list                      # 列出最近分析记录
  python bug_report.py --view 00042                # 查看单条详情
  python bug_report.py --fix 00042                 # 生成修复建议
  python bug_report.py --fix-all-pending           # 修复所有待处理 bug
  python bug_report.py --stats                     # 统计概览
"""

import argparse
import json
import sys
from pathlib import Path

# 添加当前目录到路径，以便导入 bug_analysis_engine
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from bug_analysis_engine import analyze_bug, _load_history, rank_possible_causes


BUGS_DIR = Path(__file__).parent / "bugs"
BUGS_DIR.mkdir(exist_ok=True)
PENDING_FILE = BUGS_DIR / "pending.json"


def _load_pending() -> list[dict]:
    """加载待处理 bug 列表"""
    if not PENDING_FILE.exists():
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_pending(pending: list[dict]) -> None:
    """保存待处理 bug 列表"""
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def cmd_submit(text: str, source: str = "python") -> dict:
    """提交错误日志并分析

    Args:
        text: 错误日志文本
        source: 来源类型 (python/java/ci)

    Returns:
        dict: 分析结果
    """
    result = analyze_bug(text, source)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # 如果 confidence >= 0.7，自动加入待处理列表
    if result["confidence"] >= 0.7:
        pending = _load_pending()
        pending.append(result)
        _save_pending(pending)
        print(f"\n✅ 已加入待处理列 表 (ID: {result['id']})")
    else:
        print(f"\n⚠️  置信度过低 ({result['confidence']})，未加入待处理列表")

    return result


def cmd_file(filepath: str, source: str = "python") -> dict:
    """从文件读取错误日志并提交

    Args:
        filepath: 错误日志文件路径
        source: 来源类型

    Returns:
        dict: 分析结果
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        print(f"❌ 文件不存在: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ 读取文件失败: {e}", file=sys.stderr)
        sys.exit(1)

    return cmd_submit(text, source)


def cmd_list(limit: int = 20) -> None:
    """列出最近分析记录

    Args:
        limit: 最多显示条数
    """
    history = _load_history()
    if not history:
        print("暂无分析记录")
        return

    print(f"{'ID':<8} {'错误类型':<25} {'文件':<40} {'置信度':<8} {'时间'}")
    print("-" * 100)
    for record in reversed(history[-limit:]):
        rid = record.get("id", "?????")
        err = record.get("error_type", "UNKNOWN")[:24]
        file_ = record.get("file", "-")[:39]
        conf = record.get("confidence", 0)
        ts = record.get("timestamp", "")[:19]
        print(f"{rid:<8} {err:<25} {file_:<40} {conf:<8.2f} {ts}")


def cmd_view(bug_id: str) -> None:
    """查看单条分析详情

    Args:
        bug_id: 分析记录 ID
    """
    history = _load_history()
    for record in history:
        if record.get("id") == bug_id:
            print(json.dumps(record, indent=2, ensure_ascii=False))
            # 同时显示排名原因
            causes = rank_possible_causes(record)
            if causes:
                print("\n=== 可能的根因（按概率排序）===")
                for c in causes:
                    print(f"  [{c['probability']:.0%}] {c['cause']}")
                    print(f"      建议: {c['suggestion']}")
            return

    print(f"❌ 未找到 ID 为 {bug_id} 的记录", file=sys.stderr)
    sys.exit(1)


def cmd_fix(bug_id: str) -> None:
    """为指定 bug 生成修复建议

    输出可直接给 agent 使用的修复步骤。

    Args:
        bug_id: 分析记录 ID
    """
    history = _load_history()
    bug = None
    for record in history:
        if record.get("id") == bug_id:
            bug = record
            break

    if not bug:
        print(f"❌ 未找到 ID 为 {bug_id} 的记录", file=sys.stderr)
        sys.exit(1)

    if bug.get("fix_type") == "config_change":
        print(f"🔧 修复建议（配置变更）: {bug['suggested_fix']}")
    elif bug.get("fix_type") == "write_file":
        print(f"📝 修复建议（文件重写）: {bug['suggested_fix']}")
    else:
        print(f"🔧 修复建议（patch 修改）: {bug['suggested_fix']}")

    if bug.get("file") and bug.get("line", 0) > 0:
        print(f"\n📌 目标位置: {bug['file']}:{bug['line']}")

    causes = rank_possible_causes(bug)
    if causes:
        print(f"\n推荐修复方案: {causes[0]['suggestion']}")

    # 自动从待处理列表中移除
    pending = _load_pending()
    pending = [p for p in pending if p.get("id") != bug_id]
    _save_pending(pending)


def cmd_fix_all_pending() -> None:
    """修复所有待处理 bug"""
    pending = _load_pending()
    if not pending:
        print("✅ 没有待处理的 bug")
        return

    print(f"共 {len(pending)} 个待处理 bug：")
    for i, bug in enumerate(pending, 1):
        desc = f"[{bug.get('error_type', '?')}] {bug.get('message', '')[:60]}"
        print(f"\n  {i}. ID={bug['id']} {desc}")
        print(f"     建议修复: {bug.get('suggested_fix', '无')}")

    # 确认后应用（这里只输出，不自动执行）
    print(f"\n{'=' * 60}")
    print(f"共 {len(pending)} 个待处理 bug 的修复建议已输出。")
    print(f"使用 bug_report.py --fix <ID> 查看单条详情后手动执行。")

    # 清理待处理列表（已经输出过建议了）
    # _save_pending([])


def cmd_stats() -> None:
    """统计概览"""
    history = _load_history()
    if not history:
        print("暂无分析记录")
        return

    total = len(history)
    by_type = {}
    high_conf = 0
    for r in history:
        et = r.get("error_type", "UNKNOWN")
        by_type[et] = by_type.get(et, 0) + 1
        if r.get("confidence", 0) >= 0.7:
            high_conf += 1

    print(f"📊 Bug 分析统计")
    print(f"{'=' * 40}")
    print(f"总分析次数: {total}")
    print(f"高置信度 (>=0.7): {high_conf}")
    print(f"\n错误类型分布:")
    for et, count in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {et:<25} {count}")

    pending = _load_pending()
    print(f"\n待处理 bug: {len(pending)}")


def main():
    parser = argparse.ArgumentParser(
        description="Bug 提交和查询 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python bug_report.py --submit "Traceback: ValueError at line 42"
  python bug_report.py --file /tmp/error.log
  python bug_report.py --list
  python bug_report.py --view 00001
  python bug_report.py --fix 00001
  python bug_report.py --fix-all-pending
  python bug_report.py --stats
        """,
    )

    parser.add_argument("--submit", type=str, help="直接提交错误日志文本")
    parser.add_argument("--file", type=str, help="从文件读取错误日志")
    parser.add_argument("--list", action="store_true", help="列出最近分析记录")
    parser.add_argument("--view", type=str, help="查看指定 ID 的记录")
    parser.add_argument("--fix", type=str, help="为指定 ID 生成修复建议")
    parser.add_argument("--fix-all-pending", action="store_true", help="修复所有待处理 bug")
    parser.add_argument("--stats", action="store_true", help="统计概览")
    parser.add_argument(
        "--source",
        type=str,
        default="python",
        choices=["python", "java", "ci"],
        help="错误来源类型 (默认: python)",
    )

    args = parser.parse_args()

    # 计数调用的命令数，确保一次只执行一个
    commands = [
        args.submit is not None,
        args.file is not None,
        args.list,
        args.view is not None,
        args.fix is not None,
        args.fix_all_pending,
        args.stats,
    ]
    if sum(commands) > 1:
        print("❌ 一次只能使用一个命令（--submit/--file/--list/--view/--fix/--fix-all-pending/--stats）", file=sys.stderr)
        sys.exit(1)

    if args.submit:
        cmd_submit(args.submit, args.source)
    elif args.file:
        cmd_file(args.file, args.source)
    elif args.list:
        cmd_list()
    elif args.view:
        cmd_view(args.view)
    elif args.fix:
        cmd_fix(args.fix)
    elif args.fix_all_pending:
        cmd_fix_all_pending()
    elif args.stats:
        cmd_stats()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
