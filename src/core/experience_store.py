"""experience_store.py — 经验积累闭环

设计动机（面试话术）：
  "项目三之前的 evolve_learn.py 只记录修复成功/失败，
   但缺少一个关键环节：把成功的修复经验转化为下次可复用的知识。
   借鉴 HiveWard 的 FixExperience 设计，我实现了完整的经验闭环：
   记录 → 提取模式 → 校准置信度 → 注入上下文 → 自动建议创建 skill。"

闭环流程：
  1. 每次修复后，记录到 ExperienceStore（含完整上下文）
  2. 从成功修复中提取可复用模式（同类型问题的共同特征）
  3. 根据历史成功率动态校准修复器的置信度
  4. 下次遇到类似问题时，注入相关经验作为上下文
  5. 某个模式成功 3 次以上，自动建议创建 Hermes skill

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

    Returns:
        经验记录 ID

    设计决策（面试话术）：
      "为什么成功和失败都记录？因为'在什么情况下会失败'本身就是经验。
       比如 bare_except 修复在大多数文件成功率 90%，但在某些嵌套 except
       的文件里成功率只有 30%。如果不记录失败上下文，系统就无法学到
       '嵌套 except 场景需要降低置信度'这个规律。"
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

    核心思路：如果某个修复器在某类问题上的历史成功率只有 60%，
    那即使它自己声称 confidence=0.9，实际应该降为 0.6 附近。

    算法：
      calibrated = original * (success_rate ^ 0.5)
      用平方根是为了不让低成功率过度惩罚（保留探索空间）
    """
    relevant = [
        e for e in data["experiences"]
        if e["issue_type"] == issue_type and e["fixer"] == fixer
    ]
    if len(relevant) < 3:
        return  # 样本太少，不校准

    successes = sum(1 for e in relevant if e["success"])
    success_rate = successes / len(relevant)

    original_conf = relevant[-1]["confidence"]

    calibrated = original_conf * (success_rate ** 0.5)
    calibrated = round(max(0.1, min(0.99, calibrated)), 3)

    override_key = f"{issue_type}:{fixer}"
    data["confidence_overrides"][override_key] = {
        "calibrated": calibrated,
        "original": original_conf,
        "success_rate": round(success_rate, 3),
        "sample_size": len(relevant),
        "updated_at": datetime.now().isoformat(),
    }


def _check_skill_suggestion(data: dict, pattern_key: str, issue_type: str) -> None:
    """检查是否应建议创建 Hermes skill。

    条件：同一模式的成功修复 >= 3 次。
    """
    pattern = data["patterns"].get(pattern_key, {})
    if pattern.get("successes", 0) < 3:
        return

    existing = [s for s in data["skill_suggestions"] if s["pattern_key"] == pattern_key]
    if existing:
        existing[0]["success_count"] = pattern["successes"]
        existing[0]["last_updated"] = datetime.now().isoformat()
        return

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
    })



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
