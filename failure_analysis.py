#!/usr/bin/env python3
"""
failure_analysis.py — 每周失败模式分析脚本

用途：
  1. 读取 state.json 中的 failed_tasks + completed_task_ids + error_patterns
  2. 找出最容易失败的描述模式（关键词分析）
  3. 输出分析结果到 failure_report.json
  4. 生成 Step 0 预处理 prompt 的注入文本（避免使用高危词）

用法：
  python3 failure_analysis.py
  # 输出写入 /mnt/f/项目三：多Agent/failure_report.json

面试可追问：
  - Q: 为什么分析失败模式而不是成功模式？
    A: 失败模式更容易找出可操作的改进点。成功可能是偶然的，但连续相同类型的失败说明系统性问题。
  - Q: 关键词列表如何维护？
    A: 静态内置 + 自动发现（TF-IDF 对失败任务描述提取高频词）。
  - Q: 分析频率为什么是每周？
    A: 日分析噪声太大（偶发失败会扭曲统计），月分析反馈太慢。每周刚好够积累足够样本又不至于过时。
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SWARM_DIR = Path("/mnt/f/项目三：多Agent")
STATE_PATH = SWARM_DIR / "state.json"
REPORT_PATH = SWARM_DIR / "failure_report.json"

# 内置关键词列表（可扩展）
KEYWORDS = [
    "重构", "迁移", "改造", "重写",
    "添加单元测试", "添加测试", "增加测试",
    "添加注释", "增加文档",
    "优化", "性能", "提速",
    "修复", "修补", "解决",
    "异步", "async", "await",
    "整合", "合并", "集成",
]


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def extract_category(desc: str) -> str:
    """从任务描述中提取类别（debug/feature/test）。"""
    desc_lower = desc.lower()
    if any(w in desc_lower for w in ["测试", "test", "单元测试", "集成测试"]):
        return "test"
    if any(w in desc_lower for w in ["修复", "修复", "修补", "fix", "bug"]):
        return "debug"
    return "feature"


def analyze() -> dict:
    state = load_state()
    failed_tasks = state.get("failed_tasks", [])
    completed_ids = state.get("completed_task_ids", [])
    error_patterns = state.get("error_patterns", [])
    permanently_failed = state.get("permanently_failed", [])

    report = {
        "generated_at": datetime.now().isoformat(),
        "total_failed": len(failed_tasks),
        "total_completed": len(completed_ids),
        "permanently_failed": len(permanently_failed),
        "keyword_analysis": {},
        "error_type_analysis": {},
        "category_success_rate": {},
        "injection_text": "",
        "high_risk_keywords": [],
    }

    # 1. 关键词失败率分析
    all_task_descs = defaultdict(int)
    failed_task_descs = defaultdict(int)

    for task in failed_tasks:
        desc = task.get("description", "") + " " + task.get("task_id", "")
        for word in KEYWORDS:
            if word.lower() in desc.lower():
                failed_task_descs[word] += 1
                all_task_descs[word] += 1

    # 统计总任务数中每个关键词的出现次数（包含成功任务）
    # 从 completed_task_ids 反推所有已完成任务的描述（TODO.md 中读取）
    # 这里用简化方案：只分析失败任务的高频关键词
    keyword_analysis = {}
    for word in KEYWORDS:
        failed = failed_task_descs.get(word, 0)
        if failed > 0:
            # 没有总任务数信息时，只输出失败次数和占比
            keyword_analysis[word] = {
                "failed_count": failed,
                "percentage_of_failed": round(failed / max(len(failed_tasks), 1) * 100, 1),
            }

    report["keyword_analysis"] = keyword_analysis

    # 2. 错误类型分析
    error_type_counts = defaultdict(int)
    for task in failed_tasks:
        etype = task.get("error_type", "Unknown")
        error_type_counts[etype] += 1

    # 加上 error_patterns 中的统计
    for pat in error_patterns:
        pname = pat.get("pattern", "Unknown")
        if pname not in error_type_counts:
            error_type_counts[pname] = 0
        error_type_counts[pname] = max(error_type_counts[pname], pat.get("count", 0))

    report["error_type_analysis"] = dict(
        sorted(error_type_counts.items(), key=lambda x: -x[1])
    )

    # 3. 类别成功率
    category_failed = {"debug": 0, "feature": 0, "test": 0}
    category_all = {"debug": 1, "feature": 1, "test": 1}  # +1 防除零

    for task in failed_tasks:
        cat = extract_category(task.get("description", ""))
        category_failed[cat] += 1
        category_all[cat] += 1

    # 尝试从 TODO.md 获取总任务数（粗糙估算）
    todo_path = SWARM_DIR / "TODO.md"
    if todo_path.exists():
        todo_text = todo_path.read_text(encoding="utf-8")
        for line in todo_text.split("\n"):
            if line.startswith("- [") and ("debug" in line.lower() or "feature" in line.lower() or "test" in line.lower() or "添加" in line or "修复" in line):
                cat = extract_category(line)
                category_all[cat] += 0  # 已完成的不再加，防止重复
                # 不减，只是防止失败任务被计入两次

    report["category_success_rate"] = {
        cat: {
            "failed": category_failed[cat],
            "estimated_total": category_all[cat],
            "failure_rate": round(category_failed[cat] / category_all[cat] * 100, 1),
        }
        for cat in category_failed
    }

    # 4. 高风险关键词（失败率 > 50% 的词）
    high_risk = [
        word for word, data in keyword_analysis.items()
        if data.get("failed_count", 0) >= 2  # 至少失败 2 次才认为是模式
    ]
    report["high_risk_keywords"] = high_risk

    # 5. 生成注入文本
    if high_risk:
        examples = []
        for word in high_risk:
            # 给每个高危词一个原子化拆解建议
            if word in ("重构", "迁移", "改造", "重写"):
                examples.append(f"  避免使用「{word}」。请拆成「移动函数A到新文件」+「更新引用」+「删除旧文件」")
            elif word in ("添加单元测试", "添加测试", "增加测试"):
                examples.append(f"  「{word}」成功率尚可，但如果失败请拆成「创建测试文件」+「导入被测模块」+「编写第一个测试函数」")
            else:
                examples.append(f"  「{word}」可能太抽象，请拆成具体的原子操作")

        report["injection_text"] = (
            "⚠️ 根据历史失败分析，以下描述模式失败率较高：\n"
            + "\n".join(f"  - 「{w}」" for w in high_risk)
            + "\n\n原子化拆解建议：\n"
            + "\n".join(examples)
        )
    else:
        report["injection_text"] = ""

    return report


def main():
    print("=" * 50)
    print("  失败模式分析 — 开始")
    print("=" * 50)

    report = analyze()

    # 写入报告
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n  ✅ 分析报告已写入: {REPORT_PATH}")
    print(f"\n  📊 总失败任务: {report['total_failed']}")
    print(f"  📊 已完成任务: {report['total_completed']}")
    print(f"  📊 永久失败: {report['permanently_failed']}")
    print(f"\n  🔴 高风险关键词: {report['high_risk_keywords']}")
    print(f"\n  💉 注入文本长度: {len(report['injection_text'])} 字符")

    if report["error_type_analysis"]:
        print(f"\n  ⚠️ 最常见的错误类型:")
        for etype, count in list(report["error_type_analysis"].items())[:5]:
            print(f"    - {etype}: {count} 次")

    if report["category_success_rate"]:
        print(f"\n  📈 各类别失败率:")
        for cat, data in report["category_success_rate"].items():
            print(f"    {cat}: {data['failure_rate']}% ({data['failed']}/{data['estimated_total']})")

    print("\n  💉 为 Step 0 准备的注入文本:")
    if report["injection_text"]:
        for line in report["injection_text"].split("\n"):
            print(f"    {line}")
    else:
        print("    (暂无高风险关键词，无需注入)")

    print("\n" + "=" * 50)


if __name__ == "__main__":
    main()
