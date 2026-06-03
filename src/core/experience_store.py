"""experience_store.py — 经验积累闭环（含配置变更自动传播）

设计动机（面试话术）：
  "AutoForge之前的 evolve_learn.py 只记录修复成功/失败，
   但缺少一个关键环节：把成功的修复经验转化为下次可复用的知识。
   借鉴 HiveWard 的 FixExperience 设计，我实现了完整的经验闭环：
   记录 → 提取模式 → 校准置信度 → 传播到修复器 → 自动建议创建 skill。"

新增：配置变更自动传播机制（借鉴 HiveWard 的 applyHarnessPermissionModesToBlueprint）
  HiveWard 的设计：Harness 权限变更时，自动遍历所有蓝图节点，同步权限配置。
  我的设计：置信度校准变更时，自动遍历所有使用该 fixer 的 pipeline 节点，
  将校准后的置信度传播到 fixer 的运行时配置中。

闭环流程：
  1. 每次修复后，记录到 ExperienceStore（含完整上下文）
  2. 从成功修复中提取可复用模式（同类型问题的共同特征）
  3. 根据历史成功率动态校准修复器的置信度
  4. 校准结果自动传播到 pipeline 中引用该 fixer 的节点
  5. 下次遇到类似问题时，注入相关经验作为上下文
  6. 某个模式成功 3 次以上，自动建议创建 Hermes skill

与 evolve_learn.py 的关系：
  evolve_learn.py 负责"失败学习"（哪些不该修），
  experience_store.py 负责"成功积累"（哪些修法好用）。
  两者互补，共同构成完整的自进化学习闭环。
"""

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
EXPERIENCE_FILE = SWARM_DIR / "data" / "experience_store.json"




def _load() -> dict:
    """加载经验数据库。"""
    if not EXPERIENCE_FILE.exists():
        return {
            "experiences": [],           # 所有经验记录
            "patterns": {},              # 提取的可复用模式
            "confidence_overrides": {},  # 动态校准的置信度覆盖
            "skill_suggestions": [],     # 建议创建的 skill
        }
    try:
        return json.loads(EXPERIENCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"experiences": [], "patterns": {},
                "confidence_overrides": {}, "skill_suggestions": []}


def _save(data: dict) -> None:
    """保存经验数据库。"""
    EXPERIENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXPERIENCE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _generate_id(issue_type: str, file: str, action: str) -> str:
    """生成经验记录唯一 ID。"""
    key = f"{issue_type}:{file}:{action}:{datetime.now().strftime('%Y%m%d%H')}"
    return hashlib.md5(key.encode()).hexdigest()[:10]



def record_experience(
    issue_type: str,
    file: str,
    line: int,
    fixer: str,
    action: str,
    confidence: float,
    success: bool,
    code_snippet: str = "",
    project: str = "",
    error: str = "",
    code_before: str = "",
    code_after: str = "",
) -> str:
    """记录一次修复经验。

    这是经验闭环的入口。每次修复完成后调用，
    无论成功还是失败都记录（失败经验同样有价值）。

    Args:
        issue_type: 问题类型（如 "swallowed_exception"）
        file: 修复的文件路径
        line: 修复的行号
        fixer: 使用的修复器名称
        action: 执行的修复动作描述
        confidence: 修复器给出的置信度
        success: 修复是否成功
        code_snippet: 问题代码片段（用于模式提取）
        project: 所属项目（如 "项目二"）
        error: 失败原因（success=False 时）
        code_before: 修复前的精确代码（用于 ExperienceFixer 模式匹配）
        code_after: 修复后的精确代码（用于 ExperienceFixer 直接套用）

    Returns:
        经验记录 ID

    设计决策（面试话术）：
      "为什么成功和失败都记录？因为'在什么情况下会失败'本身就是经验。
       比如 bare_except 修复在大多数文件成功率 90%，但在某些嵌套 except
       的文件里成功率只有 30%。如果不记录失败上下文，系统就无法学到
       '嵌套 except 场景需要降低置信度'这个规律。"

      "code_before/code_after 是精确的代码对，不是摘要。
       这让 ExperienceFixer 可以做精确字符串匹配+替换，
       而不是依赖 LLM 重新推理。零成本复用。"
    """
    data = _load()

    pattern_key = _extract_pattern_key(issue_type, code_snippet)

    exp_id = _generate_id(issue_type, file, action)
    experience = {
        "id": exp_id,
        "issue_type": issue_type,
        "file_pattern": _extract_file_pattern(file),
        "fixer": fixer,
        "action": action,
        "confidence": confidence,
        "success": success,
        "error": error[:200] if error else "",
        "context": {
            "file": file,
            "line": line,
            "code_snippet": code_snippet[:200],
            "project": project,
        },
        "code_before": code_before[:500] if code_before else "",
        "code_after": code_after[:500] if code_after else "",
        "outcome": {
            "syntax_ok": success,  # 简化：成功即语法OK
            "tests_passed": None,
            "reverted": False,
        },
        "at": datetime.now().isoformat(),
        "pattern_key": pattern_key,
    }

    data["experiences"].append(experience)

    _update_pattern_stats(data, pattern_key, success, confidence)

    _recalibrate_confidence(data, issue_type, fixer)

    _check_skill_suggestion(data, pattern_key, issue_type)

    if len(data["experiences"]) > 1000:
        data["experiences"] = data["experiences"][-1000:]

    data["index_dirty"] = True
    _save(data)
    return exp_id


def _extract_pattern_key(issue_type: str, code_snippet: str) -> str:
    """从问题类型和代码片段提取模式键。

    相同类型 + 相似代码结构 = 同一个模式。
    这样后续可以聚合"这类问题用这种修复成功率最高"。
    """
    if not code_snippet:
        return issue_type

    import re
    normalized = re.sub(r'\b\w+\b', 'W', code_snippet.strip())[:50]
    normalized = re.sub(r'\s+', ' ', normalized)
    return f"{issue_type}:{normalized}"


def _extract_file_pattern(file: str) -> str:
    """从文件路径提取模式（如 *.py, src/api/*.py）。"""
    from pathlib import PurePosixPath
    p = PurePosixPath(file)
    suffix = p.suffix or "*"
    parts = p.parts[-3:] if len(p.parts) >= 3 else p.parts
    return "/".join(parts[:-1]) + f"/*{suffix}" if len(parts) > 1 else f"*{suffix}"


def _update_pattern_stats(data: dict, pattern_key: str, success: bool, confidence: float) -> None:
    """更新模式统计数据。"""
    if pattern_key not in data["patterns"]:
        data["patterns"][pattern_key] = {
            "total": 0,
            "successes": 0,
            "failures": 0,
            "avg_confidence": 0.0,
            "first_seen": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
        }

    p = data["patterns"][pattern_key]
    p["total"] += 1
    if success:
        p["successes"] += 1
    else:
        p["failures"] += 1
    p["avg_confidence"] = (
        (p["avg_confidence"] * (p["total"] - 1) + confidence) / p["total"]
    )
    p["last_seen"] = datetime.now().isoformat()


def _recalibrate_confidence(data: dict, issue_type: str, fixer: str) -> None:
    """根据历史成功率重新校准修复器置信度。

    核心思路：直接用历史成功率作为校准后的置信度，
    而不是乘以原始置信度（避免死亡螺旋）。

    算法：
      calibrated = success_rate
      如果样本不足，不校准（保留修复器自报的置信度）
    """
    relevant = [
        e for e in data["experiences"]
        if e["issue_type"] == issue_type and e["fixer"] == fixer
    ]
    if len(relevant) < 3:
        return  # 样本太少，不校准

    successes = sum(1 for e in relevant if e["success"])
    success_rate = successes / len(relevant)

    # 直接用成功率作为校准值，避免死亡螺旋
    calibrated = round(max(0.1, min(0.99, success_rate)), 3)

    override_key = f"{issue_type}:{fixer}"
    data["confidence_overrides"][override_key] = {
        "calibrated": calibrated,
        "original": success_rate,  # 用成功率作为原始值
        "success_rate": round(success_rate, 3),
        "sample_size": len(relevant),
        "updated_at": datetime.now().isoformat(),
    }


def _check_skill_suggestion(data: dict, pattern_key: str, issue_type: str) -> None:
    """检查是否应创建 Hermes skill，达到条件自动创建。

    条件：同一模式的成功修复 >= 3 次。
    自动从成功经验中提取修复模式，生成 SKILL.md 并写入 ~/.hermes/skills/。
    """
    pattern = data["patterns"].get(pattern_key, {})
    if pattern.get("successes", 0) < 3:
        return

    existing = [s for s in data["skill_suggestions"] if s["pattern_key"] == pattern_key]
    if existing:
        existing[0]["success_count"] = pattern["successes"]
        existing[0]["last_updated"] = datetime.now().isoformat()
        # 如果已经创建过 skill，不重复创建
        if existing[0].get("skill_created"):
            return
    else:
        data["skill_suggestions"].append({
            "pattern_key": pattern_key,
            "issue_type": issue_type,
            "success_count": pattern["successes"],
            "total_attempts": pattern["total"],
            "success_rate": round(pattern["successes"] / pattern["total"], 3),
            "suggested_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "message": (
                f"模式 '{pattern_key}' 已成功修复 {pattern['successes']} 次"
                f"（成功率 {pattern['successes']}/{pattern['total']}），"
                f"建议封装为 Hermes skill 以提高复用效率。"
            ),
            "skill_created": False,
        })
        existing = [data["skill_suggestions"][-1]]

    # 自动创建 Skill
    _auto_create_skill(data, pattern_key, issue_type, existing[0])


def _auto_create_skill(data: dict, pattern_key: str, issue_type: str, suggestion: dict) -> None:
    """根据成功经验自动创建 Hermes Skill。

    提取同类问题的成功修复模式，生成 SKILL.md 写入 ~/.hermes/skills/。
    """
    import json as _json

    # 收集该模式的所有成功经验
    successful_exps = [
        e for e in data["experiences"]
        if e.get("pattern_key") == pattern_key and e.get("success")
    ]
    if not successful_exps:
        return

    # 提取最常见的 fixer 和 action
    fixer_counts: dict[str, int] = {}
    action_examples: list[str] = []
    for exp in successful_exps:
        fixer = exp.get("fixer", "unknown")
        fixer_counts[fixer] = fixer_counts.get(fixer, 0) + 1
        action = exp.get("action", "")
        if action and action not in action_examples:
            action_examples.append(action[:200])

    best_fixer = max(fixer_counts, key=lambda k: fixer_counts[k]) if fixer_counts else "unknown"
    success_rate = suggestion.get("success_rate", 0)

    # 生成 skill 名称（小写+连字符）
    skill_name = f"auto-fix-{issue_type.replace('_', '-').lower()}"
    skill_name = skill_name[:64]  # Hermes 限制 64 字符

    # 生成 SKILL.md 内容
    skill_content = f"""---
name: {skill_name}
description: "自动修复 {issue_type} 类型问题（成功率 {success_rate:.0%}，经验数据自动生成）"
---

# 自动修复: {issue_type}

## 触发条件
- Scanner 检测到 `{issue_type}` 类型的代码问题
- 历史成功率: {suggestion.get('success_count', 0)}/{suggestion.get('total_attempts', 0)} ({success_rate:.0%})

## 推荐修复策略
- **首选修复器**: `{best_fixer}`（历史使用 {fixer_counts.get(best_fixer, 0)} 次）
- **其他可用修复器**: {', '.join(f for f in fixer_counts if f != best_fixer)}

## 成功修复模式
"""

    for i, action in enumerate(action_examples[:5], 1):
        skill_content += f"\n### 模式 {i}\n```\n{action}\n```\n"

    skill_content += f"""
## 注意事项
- 此 Skill 由经验数据自动生成，首次使用需验证修复效果
- 如果修复失败，经验系统会自动降低该模式的置信度
- 建议在修复后运行语法检查确认无误
"""

    # 写入 skill 文件
    try:
        import os
        skill_dir = os.path.expanduser(f"~/.hermes/skills/software-development/{skill_name}")
        os.makedirs(skill_dir, exist_ok=True)
        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(skill_content)
        logger.info("[AutoSkill] 已创建 Skill: %s (%s)", skill_name, skill_path)
        suggestion["skill_created"] = True
        suggestion["skill_path"] = skill_path
    except Exception as e:
        logger.warning("[AutoSkill] 创建 Skill 失败: %s", e)



def get_calibrated_confidence(issue_type: str, fixer: str, original: float) -> float:
    """获取校准后的置信度。

    pipeline 在调用 fixer.fix() 后，用此函数替换原始置信度。
    如果没有足够历史数据，返回原始值。

    Args:
        issue_type: 问题类型
        fixer: 修复器名称
        fixer 给出的原始置信度

    Returns:
        校准后的置信度（0.0~1.0）

    设计决策（面试话术）：
      "为什么不直接改修复器的 confidence 实现？
       因为修复器不应该知道自己的历史表现——这是关注点分离。
       修复器只负责'根据当前代码判断我有多大把握'，
       经验系统负责'根据历史表现调整这个判断'。
       两者独立演化，互不影响。"
    """
    data = _load()
    override_key = f"{issue_type}:{fixer}"
    override = data["confidence_overrides"].get(override_key)
    if override and override["sample_size"] >= 3:
        return override["calibrated"]
    return original


# ──────────────────────────────────────────────
# 配置变更自动传播（借鉴 HiveWard 的 applyHarnessPermissionModesToBlueprint）
# ──────────────────────────────────────────────

def propagate_confidence_to_fixers() -> dict:
    """将经验校准的置信度传播到所有使用该 fixer 的 pipeline 节点。

    借鉴 HiveWard 的设计：
      applyHarnessPermissionModesToBlueprint 遍历蓝图中所有 agent 节点，
      将 Harness 权限变更同步到每个节点的 permissionProfile。
      我用同样的模式：遍历所有 confidence_overrides，
      将校准后的置信度"传播"到 pipeline 运行时可读取的格式。

    为什么需要这个？
      get_calibrated_confidence 只在 pipeline 单次执行中生效（per-issue 调用）。
      但如果 pipeline 重启、或有多个 pipeline 实例并行运行，
      校准结果不会自动传播——需要一个显式的传播机制。

    传播目标：
      在 experience_store.json 中维护一个 "propagated_confidences" 字段，
      格式为 {issue_type: {fixer: calibrated_value}}，
      供 pipeline 启动时批量加载，而不是每次修复都读文件。

    Returns:
        传播结果摘要 {propagated_count, overrides, timestamp}
    """
    data = _load()
    overrides = data.get("confidence_overrides", {})
    if not overrides:
        return {"propagated_count": 0, "overrides": {}, "timestamp": datetime.now().isoformat()}

    # 构建 propagated_confidences：pipeline 启动时可批量读取
    propagated = {}
    for key, info in overrides.items():
        if info.get("sample_size", 0) < 3:
            continue
        # key 格式: "issue_type:fixer"
        parts = key.split(":", 1)
        if len(parts) != 2:
            continue
        issue_type, fixer = parts
        if issue_type not in propagated:
            propagated[issue_type] = {}
        propagated[issue_type][fixer] = {
            "calibrated": info["calibrated"],
            "success_rate": info["success_rate"],
            "sample_size": info["sample_size"],
            "updated_at": info["updated_at"],
        }

    data["propagated_confidences"] = propagated
    data["propagated_at"] = datetime.now().isoformat()
    _save(data)

    total = sum(len(fixers) for fixers in propagated.values())
    logger.info(f"[经验传播] 已将 {total} 条校准置信度传播到 propagated_confidences")

    return {
        "propagated_count": total,
        "overrides": propagated,
        "timestamp": datetime.now().isoformat(),
    }


def get_propagated_confidence(issue_type: str, fixer: str) -> Optional[float]:
    """从传播后的配置中获取校准置信度（pipeline 启动时批量加载用）。

    与 get_calibrated_confidence 的区别：
      - get_calibrated_confidence：每次调用读文件，适合 per-issue 调用
      - get_propagated_confidence：从已传播的缓存中读，适合批量场景

    Returns:
        校准后的置信度，如果没有传播数据则返回 None
    """
    data = _load()
    propagated = data.get("propagated_confidences", {})
    fixer_info = propagated.get(issue_type, {}).get(fixer)
    if fixer_info:
        return fixer_info["calibrated"]
    return None


def get_relevant_experiences(issue_type: str, file: str = "", limit: int = 5) -> list[dict]:
    """获取与当前问题相关的经验记录。

    pipeline 在修复前调用此函数，把相关经验注入上下文，
    帮助修复器做出更好的决策（特别是知道哪些场景会失败）。

    Args:
        issue_type: 问题类型
        file: 文件路径（用于匹配相似文件）
        limit: 最多返回几条

    Returns:
        经验记录列表，按相关性排序
    """
    data = _load()

    relevant = [e for e in data["experiences"] if e["issue_type"] == issue_type]
    if not relevant:
        return []

    def _relevance(exp: dict) -> float:
        score = 1.0 if exp["success"] else 0.5  # 成功经验权重更高
        if file and exp.get("context", {}).get("file", ""):
            exp_dir = str(Path(exp["context"]["file"]).parent)
            cur_dir = str(Path(file).parent)
            if exp_dir == cur_dir:
                score += 0.5
            if Path(exp["context"]["file"]).suffix == Path(file).suffix:
                score += 0.3
        return score

    relevant.sort(key=_relevance, reverse=True)
    return relevant[:limit]


def get_failure_warnings(issue_type: str) -> list[str]:
    """获取某类问题的已知失败场景警告。

    pipeline 在修复前调用，把这些警告作为 negative examples
    注入到修复器的 prompt 中，避免重蹈覆辙。
    """
    data = _load()
    failures = [
        e for e in data["experiences"]
        if e["issue_type"] == issue_type and not e["success"]
    ]
    if not failures:
        return []

    seen = set()
    warnings = []
    for e in failures:
        err = e.get("error", "")
        if err and err not in seen:
            seen.add(err)
            ctx = e.get("context", {})
            warnings.append(
                f"在 {ctx.get('file', '?')}:{ctx.get('line', '?')} "
                f"修复失败: {err[:100]}"
            )
    return warnings[:5]


def get_pattern_stats() -> dict:
    """获取所有模式的统计概览。"""
    data = _load()
    patterns = data.get("patterns", {})

    sorted_patterns = sorted(
        patterns.items(),
        key=lambda x: x[1].get("successes", 0) / max(x[1].get("total", 1), 1),
        reverse=True,
    )
    return {
        "total_patterns": len(patterns),
        "total_experiences": len(data.get("experiences", [])),
        "confidence_overrides": len(data.get("confidence_overrides", {})),
        "propagated_count": sum(
            len(fixers) for fixers in data.get("propagated_confidences", {}).values()
        ),
        "skill_suggestions": data.get("skill_suggestions", []),
        "top_patterns": [
            {
                "key": k,
                "success_rate": round(v["successes"] / max(v["total"], 1), 3),
                "total": v["total"],
                "avg_confidence": round(v.get("avg_confidence", 0), 3),
            }
            for k, v in sorted_patterns[:10]
        ],
    }


def get_pending_skill_suggestions() -> list[dict]:
    """获取待处理的 skill 创建建议。"""
    data = _load()
    return [
        s for s in data.get("skill_suggestions", [])
        if s.get("success_count", 0) >= 3
    ]


def load_experiences(issue_type: str = "", limit: int = 50) -> list[dict]:
    """加载经验记录，可按 issue_type 过滤。

    Args:
        issue_type: 过滤的问题类型，为空则返回全部
        limit: 最多返回条数

    Returns:
        最近 limit 条经验记录

    这个函数解决了 LLMFixer 一直在调但不存在的静默 ImportError。
    同时为 ExperienceRetriever 提供数据源。
    """
    data = _load()
    exps = data.get("experiences", [])
    if issue_type:
        exps = [e for e in exps if e.get("issue_type") == issue_type]
    return exps[-limit:]


def get_experience_by_id(exp_id: str) -> Optional[dict]:
    """按 ID 获取单条完整经验记录。

    Args:
        exp_id: 经验记录 ID

    Returns:
        经验记录，或 None

    ExperienceRetriever 检索到 ID 后调用此函数取完整数据。
    """
    data = _load()
    for exp in data.get("experiences", []):
        if exp.get("id") == exp_id:
            return exp
    return None
