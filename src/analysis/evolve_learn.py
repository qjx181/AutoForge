"""evolve_learn.py — 自进化学习引擎

功能：
  1. 追踪修复失败的原因，下次跳过相同错误
  2. 发现重复3次以上的修复模式，自动建议创建 skill
  3. 维护失败知识库，持续减少相同错误
"""

import json
import hashlib
from datetime import datetime
from pathlib import Path

SWARM_DIR = Path(__file__).parent.parent.resolve()
LEARN_FILE = SWARM_DIR / "data" / "evolve_learn.json"


def _load() -> dict:
    """加载学习数据库"""
    if not LEARN_FILE.exists():
        return {"failed_fixes": [], "skip_patterns": {}, "skill_suggestions": [], "fix_history": []}
    try:
        return json.loads(LEARN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"failed_fixes": [], "skip_patterns": {}, "skill_suggestions": [], "fix_history": []}


def _save(data: dict):
    LEARN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_fix(issue_type: str, file_path: str, success: bool, error: str = ""):
    """记录一次修复尝试的结果"""
    data = _load()

    entry = {
        "issue_type": issue_type,
        "file": file_path,
        "success": success,
        "error": error[:200],
        "at": datetime.now().isoformat(),
    }
    data["fix_history"].append(entry)

    if not success and error:
        fingerprint = hashlib.md5(f"{issue_type}:{error[:100]}".encode()).hexdigest()[:12]
        existing = [f for f in data["failed_fixes"] if f["fingerprint"] == fingerprint]

        if existing:
            existing[0]["count"] += 1
            existing[0]["last_seen"] = datetime.now().isoformat()
        else:
            data["failed_fixes"].append({
                "fingerprint": fingerprint,
                "issue_type": issue_type,
                "error": error[:200],
                "count": 1,
                "first_seen": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
            })
        for f in data["failed_fixes"]:
            if f["count"] >= 3 and f["fingerprint"] not in data["skip_patterns"]:
                data["skip_patterns"][f["fingerprint"]] = {
                    "issue_type": f["issue_type"],
                    "reason": f"连续失败 {f['count']} 次: {f['error'][:100]}",
                    "skipped_at": datetime.now().isoformat(),
                }

    success_history = [h for h in data["fix_history"] if h["success"]]
    type_counts = {}
    for h in success_history:
        key = h["issue_type"]
        type_counts[key] = type_counts.get(key, 0) + 1

    for issue_type, count in type_counts.items():
        if count >= 3:
            suggestion_key = f"skill_{issue_type}"
            if suggestion_key not in [s["suggestion_key"] for s in data["skill_suggestions"]]:
                data["skill_suggestions"].append({
                    "suggestion_key": suggestion_key,
                    "issue_type": issue_type,
                    "count": count,
                    "suggested_at": datetime.now().isoformat(),
                    "message": f"修复模式 '{issue_type}' 已成功 {count} 次，建议封装为 Hermes skill",
                })

    _save(data)


def should_skip(issue_type: str, file_path: str, error_hint: str = "") -> bool:
    """检查是否应该跳过这个修复（之前失败过3次以上）"""
    data = _load()
    if not data["skip_patterns"]:
        return False

    fingerprint = hashlib.md5(f"{issue_type}:{error_hint[:100]}".encode()).hexdigest()[:12]
    return fingerprint in data["skip_patterns"]


def get_skip_reason(issue_type: str, error_hint: str = "") -> str:
    """获取跳过原因"""
    data = _load()
    fingerprint = hashlib.md5(f"{issue_type}:{error_hint[:100]}".encode()).hexdigest()[:12]
    info = data["skip_patterns"].get(fingerprint, {})
    return info.get("reason", "")


def get_stats() -> dict:
    """获取学习统计"""
    data = _load()
    return {
        "total_fixes_attempted": len(data.get("fix_history", [])),
        "total_skipped_patterns": len(data.get("skip_patterns", {})),
        "skill_suggestions": data.get("skill_suggestions", []),
        "recent_failures": data.get("failed_fixes", [])[-5:],
    }
