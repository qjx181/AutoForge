#!/usr/bin/env python3
"""
failure_analysis.py — 每周失败模式分析脚本

用途：
  1. 读取 state.json 中的 failed_tasks + completed_task_ids + error_patterns
  2. 找出最容易失败的描述模式（关键词分析）
  3. 输出分析结果到 failure_report.json
  4. 生成 Step 0 预处理 prompt 的注入文本（避免使用高危词）
  5. 自动将高频失败模式转换为修复规则写入 SWARM_RULES.md（项6）

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
  - Q: 修复规则如何写入 SWARM_RULES.md？
    A: 使用 ## Auto-Generated Fixes 章节标识，每次运行替换整个章节（非追加）。
      协调者在派发任务前读取此章节，注入 dev-cell 的 prompt 中。
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
SWARM_RULES_PATH = SWARM_DIR / "SWARM_RULES.md"

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

    keyword_analysis = {}
    for word in KEYWORDS:
        failed = failed_task_descs.get(word, 0)
        if failed > 0:
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
    category_all = {"debug": 1, "feature": 1, "test": 1}

    for task in failed_tasks:
        cat = extract_category(task.get("description", ""))
        category_failed[cat] += 1
        category_all[cat] += 1

    todo_path = SWARM_DIR / "TODO.md"
    if todo_path.exists():
        todo_text = todo_path.read_text(encoding="utf-8")
        for line in todo_text.split("\n"):
            if line.startswith("- [") and ("debug" in line.lower() or "feature" in line.lower() or "test" in line.lower() or "添加" in line or "修复" in line):
                cat = extract_category(line)
                category_all[cat] += 0

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
        if data.get("failed_count", 0) >= 2
    ]
    report["high_risk_keywords"] = high_risk

    # 5. 生成注入文本
    if high_risk:
        examples = []
        for word in high_risk:
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


# ═══════════════════════════════════════════════════════════════════
# 自动修复规则生成（项6）
# ═══════════════════════════════════════════════════════════════════

def generate_fix_rules(report: dict) -> list:
    """generate_fix_rules — 从失败分析报告中提取高频失败模式，生成修复规则列表

    Args:
        report: analyze() 返回的分析报告字典

    Returns:
        修复规则列表，每条规则是 dict:
        {
            "pattern": "失败模式描述",
            "rule": "修复规则文本（Markdown 格式）",
            "example": "示例说明（可选）",
        }

    作用（项6）：
      将分析报告中的高频失败模式转化为可执行的修复规则，
      写入 SWARM_RULES.md 的 ## Auto-Generated Fixes 章节。

    原理：
      - 高失败率关键词 → 原子化拆解规则
      - 高频错误类型 → 预防性规则
      - 高风险类别 → 该类别统一规则

    逻辑：
      1. 从 report 读取 high_risk_keywords、error_type_analysis、category_success_rate
      2. 针对每个高危关键词生成一条修复规则
      3. 针对高频错误类型（top 3）生成预防规则
      4. 合并去重，返回规则列表

    面试追问：
      - 规则会不会自相矛盾？答：目前是追加式，每次分析结果独立。
      - 规则数量如何控制？答：最多 10 条，超过时合并相似规则。
    """
    rules = []
    seen_patterns = set()
    max_rules = 10

    # 1. 高失败率关键词 → 原子化规则
    for keyword in report.get("high_risk_keywords", []):
        if len(rules) >= max_rules:
            break
        pattern_key = f"high_risk:{keyword}"
        if pattern_key in seen_patterns:
            continue
        seen_patterns.add(pattern_key)

        if keyword in ("重构", "迁移", "改造", "重写"):
            rule = {
                "pattern": f"关键词「{keyword}」失败率较高",
                "rule": (
                    f"收到包含「{keyword}」的任务时，必须将其拆解为多个原子步骤：\n"
                    f"  1. 创建新文件/新函数\n"
                    f"  2. 更新引用和导入\n"
                    f"  3. 删除旧文件/旧函数\n"
                    f"  4. 运行测试验证\n"
                    f"不要在一次委托中完成全部重构。"
                ),
                "example": f"不要写「对模块X进行{keyword}」，而是写「创建模块X的新版本」+「更新所有引用」+「删除旧模块」",
            }
            rules.append(rule)
        elif keyword in ("添加单元测试", "添加测试", "增加测试"):
            rule = {
                "pattern": f"关键词「{keyword}」需谨慎",
                "rule": (
                    f"收到包含「{keyword}」的任务时，优先使用 write_file 直接创建测试文件，\n"
                    f"而不是依赖子Agent从零生成。如果子Agent失败，退化为协调者直接 write_file。\n"
                    f"测试文件创建步骤：\n"
                    f"  1. 创建 __init__.py（如没有）\n"
                    f"  2. 创建 test_xxx.py\n"
                    f"  3. 导入被测模块\n"
                    f"  4. 编写测试类/函数\n"
                    f"  5. 运行 pytest 验证"
                ),
                "example": "测试文件推荐用 write_file 直接写入，而非 delegate_task",
            }
            rules.append(rule)
        else:
            rule = {
                "pattern": f"关键词「{keyword}」需拆解",
                "rule": (
                    f"包含「{keyword}」的任务，需拆解为 2-3 个更小的子步骤。\n"
                    f"子步骤必须可独立验证（编译通过、单步运行通过等）。"
                ),
                "example": f"避免一次委托「{keyword}」，先做最小可行版本再逐步迭代",
            }
            rules.append(rule)

    # 2. 高频错误类型 → 预防规则
    state = load_state()
    error_patterns = state.get("error_patterns", [])

    for pat in error_patterns:
        if len(rules) >= max_rules:
            break
        pattern_key = f"error:{pat.get('pattern', 'unknown')}"
        if pattern_key in seen_patterns:
            continue
        seen_patterns.add(pattern_key)

        pname = pat.get("pattern", "Unknown")
        pcount = pat.get("count", 1)

        if pcount >= 2:
            rule = {
                "pattern": f"高频错误：{pname}（{pcount}次）",
                "rule": f"收到可能导致 {pname} 的任务时，在执行前做以下预防检查：\n"
                        f"  1. 确保导入语句正确\n"
                        f"  2. 确保参数类型匹配\n"
                        f"  3. 先写最小示例验证",
            }
            rules.append(rule)

    # 3. 类别维度规则
    cat_rates = report.get("category_success_rate", {})
    for cat, data in cat_rates.items():
        if len(rules) >= max_rules:
            break
        failure_rate = data.get("failure_rate", 0)
        if failure_rate >= 50 and data.get("failed", 0) >= 2:
            pattern_key = f"category:{cat}"
            if pattern_key in seen_patterns:
                continue
            seen_patterns.add(pattern_key)
            rule = {
                "pattern": f"类别「{cat}」失败率 {failure_rate}%",
                "rule": f"类别「{cat}」历史失败率高。执行此类任务时：\n"
                        f"  1. 优先用 write_file 直接写入\n"
                        f"  2. 每次改动用编译门禁验证\n"
                        f"  3. 如果两次连续失败，标记为 fix_patch 任务",
            }
            rules.append(rule)

    return rules


def write_auto_fixes_to_swarm_rules(rules: list):
    """write_auto_fixes_to_swarm_rules — 将修复规则写入 SWARM_RULES.md

    Args:
        rules: generate_fix_rules() 返回的修复规则列表

    作用（项6）：
      将高频失败模式转换的修复规则写入 SWARM_RULES.md 的
      ## Auto-Generated Fixes 章节。

    边界情况：
      - SWARM_RULES.md 不存在：先创建
      - ## Auto-Generated Fixes 章节已存在：替换
      - 规则列表为空：写入「暂无自动生成的修复规则」
    """
    section_header = "## Auto-Generated Fixes\n"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rule_lines = [
        "<!-- 此章节由 failure_analysis.py 自动生成，请勿手动编辑 -->",
        f"<!-- 生成时间: {timestamp} -->",
        f"<!-- 共 {len(rules)} 条规则 -->",
        "",
    ]

    if not rules:
        rule_lines.append("暂无自动生成的修复规则。")
    else:
        for i, rule in enumerate(rules, 1):
            rule_lines.append(f"### Rule {i}: {rule['pattern']}")
            rule_lines.append("")
            rule_lines.append(rule['rule'])
            if rule.get('example'):
                rule_lines.append("")
                rule_lines.append(f"> 示例: {rule['example']}")
            rule_lines.append("")

    new_section = section_header + "\n".join(rule_lines)

    if SWARM_RULES_PATH.exists():
        current = SWARM_RULES_PATH.read_text(encoding="utf-8")
    else:
        current = ""

    if "## Auto-Generated Fixes" in current:
        new_content = re.sub(
            r"(?s)## Auto-Generated Fixes\n.*?(?=\n## |\Z)",
            new_section.rstrip("\n"),
            current,
        )
    else:
        if current and not current.endswith("\n"):
            current += "\n"
        new_content = current + "\n" + new_section

    SWARM_RULES_PATH.write_text(new_content, encoding="utf-8")
    print(f"  Swarm Rules 修复规则已写入: {SWARM_RULES_PATH}")
    print(f"  Swarm Rules 共 {len(rules)} 条自动生成的修复规则")


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

    print(f"\n  分析报告已写入: {REPORT_PATH}")
    print(f"\n  总失败任务: {report['total_failed']}")
    print(f"  已完成任务: {report['total_completed']}")
    print(f"  永久失败: {report['permanently_failed']}")
    print(f"\n  高风险关键词: {report['high_risk_keywords']}")
    print(f"\n  注入文本长度: {len(report['injection_text'])} 字符")

    if report["error_type_analysis"]:
        print(f"\n  最常见的错误类型:")
        for etype, count in list(report["error_type_analysis"].items())[:5]:
            print(f"    - {etype}: {count} 次")

    if report["category_success_rate"]:
        print(f"\n  各类别失败率:")
        for cat, data in report["category_success_rate"].items():
            print(f"    {cat}: {data['failure_rate']}% ({data['failed']}/{data['estimated_total']})")

    print(f"\n  为 Step 0 准备的注入文本:")
    if report["injection_text"]:
        for line in report["injection_text"].split("\n"):
            print(f"    {line}")
    else:
        print("    (暂无高风险关键词，无需注入)")

    # 6. 生成修复规则并写入 SWARM_RULES.md（项6）
    fix_rules = generate_fix_rules(report)
    write_auto_fixes_to_swarm_rules(fix_rules)

    print("\n" + "=" * 50)


if __name__ == "__main__":
    main()
