"""confidence_gate.py — 审批门控（置信度分级决策）

设计动机（面试话术）：
  "项目三之前的修复是'修了就提交'，没有区分'我确定这没问题'和
   '我大概率是对的但可能有副作用'。借鉴 HiveWard 的审批节点设计，
   我加入了置信度门控：高置信度自动应用，中置信度等人审批，
   低置信度只记录不修。这样系统知道自己的能力边界。"

三级门控：
  > 0.8  → AUTO_APPLY    自动应用，记录到 git
  0.5~0.8 → PENDING_REVIEW 进入审批队列，等人或 Agent 审批
  < 0.5  → REJECTED       拒绝修复，只记录问题

审批队列持久化在 data/approval_queue.json，支持：
  - 查看待审批列表
  - 批准/拒绝单个修复
  - 批量批准高置信度修复
  - 审批历史（谁批准了什么、什么时候）
"""

import hashlib
import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from .adapters_pkg import FixResult, Issue

logger = logging.getLogger(__name__)

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
QUEUE_FILE = SWARM_DIR / "data" / "approval_queue.json"
HISTORY_FILE = SWARM_DIR / "data" / "approval_history.json"



class GateDecision(str, Enum):
    """门控决策结果。"""
    AUTO_APPLY = "auto_apply"         # 自动应用
    PENDING_REVIEW = "pending_review" # 等待人工审批
    REJECTED = "rejected"             # 拒绝修复


class InboxItemStatus(str, Enum):
    """Inbox 审批项状态机（借鉴 HiveWard Inbox 完整生命周期）。

    状态流转：
      pending  → approved  → (触发回调，自动应用修复)
      pending  → rejected  → (记录原因，经验积累)
      pending  → replied   → (补充信息后重新评估)
      pending  → expired   → (超过 TTL 自动过期)
      replied  → approved / rejected / expired（重新评估后走主流程）

    设计决策（面试话术）：
      "HiveWard 的 Inbox 不是简单的待审批列表，而是有完整生命周期。
       我借鉴了这个设计：replied 状态让审批者可以'补充信息后再决定'，
       expired 状态防止审批队列无限膨胀。审批批准后自动触发回调，
       不需要额外的'应用修复'步骤。"
    """
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REPLIED = "replied"
    EXPIRED = "expired"


DEFAULT_AUTO_APPLY_THRESHOLD = 0.8    # >= 此值自动应用
DEFAULT_REJECT_THRESHOLD = 0.5        # < 此值拒绝


def _get_thresholds() -> tuple[float, float]:
    """从 config.yaml 读取阈值，读不到用默认值。"""
    cfg_path = SWARM_DIR / "config.yaml"
    auto_t = DEFAULT_AUTO_APPLY_THRESHOLD
    reject_t = DEFAULT_REJECT_THRESHOLD
    if cfg_path.exists():
        try:
            text = cfg_path.read_text(encoding="utf-8")
            import re
            m = re.search(r"auto_apply_threshold:\s*([\d.]+)", text)
            if m:
                auto_t = float(m.group(1))
            m = re.search(r"reject_threshold:\s*([\d.]+)", text)
            if m:
                reject_t = float(m.group(1))
        except Exception:
            logging.debug("配置文件解析失败，使用默认阈值")
    return auto_t, reject_t


def evaluate(fix_result: FixResult) -> GateDecision:
    """根据修复结果的置信度，做出门控决策。

    Args:
        fix_result: 修复器返回的标准化结果

    Returns:
        GateDecision 枚举值：AUTO_APPLY / PENDING_REVIEW / REJECTED

    设计决策（面试话术）：
      "为什么不直接用 fix_result.success 来决定？
       因为 success=True 只代表代码通过了语法检查，
       不代表修复逻辑是正确的。比如把 bare_except 改成 except Exception
       语法上一定没问题，但把 open() 改成 with open() 可能破坏原有逻辑。
       置信度量化了'这个修复在语义上也是正确的'的概率。"
    """
    if not fix_result.success:
        return GateDecision.REJECTED

    auto_t, reject_t = _get_thresholds()

    if fix_result.confidence >= auto_t:
        return GateDecision.AUTO_APPLY
    elif fix_result.confidence >= reject_t:
        return GateDecision.PENDING_REVIEW
    else:
        return GateDecision.REJECTED



def _generate_item_id(fix_result: FixResult) -> str:
    """根据修复结果生成唯一 ID。"""
    key = f"{fix_result.issue_type}:{fix_result.file}:{fix_result.line}:{fix_result.action}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _load_queue() -> list[dict]:
    """加载审批队列。"""
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_queue(queue: list[dict]) -> None:
    """保存审批队列。"""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_history() -> list[dict]:
    """加载审批历史。"""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(history: list[dict]) -> None:
    """保存审批历史。"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue(fix_result: FixResult, issue: Issue) -> str:
    """将一个修复放入审批队列。

    Args:
        fix_result: 修复器返回的结果
        issue: 对应的 Issue

    Returns:
        队列项 ID
    """
    item_id = _generate_item_id(fix_result)
    queue = _load_queue()

    if any(item["id"] == item_id for item in queue):
        logger.info("Item %s already in queue, skipping", item_id)
        return item_id

    item = {
        "id": item_id,
        "fix_result": fix_result.to_dict(),
        "issue": issue.to_dict(),
        "status": "pending",       # pending / approved / rejected
        "queued_at": datetime.now().isoformat(),
        "reviewed_at": None,
        "reviewed_by": None,       # "human" 或 "auto_agent"
        "review_comment": "",
    }
    queue.append(item)
    _save_queue(queue)
    logger.info("Enqueued fix for review: %s (%s:%s)", item_id, issue.type, issue.file)
    return item_id


def approve(item_id: str, reviewer: str = "human", comment: str = "") -> bool:
    """批准一个待审批的修复。

    Args:
        item_id: 队列项 ID
        reviewer: 审批者（"human" 或 "auto_agent"）
        comment: 审批备注

    Returns:
        是否成功批准
    """
    queue = _load_queue()
    for item in queue:
        if item["id"] == item_id and item["status"] == "pending":
            item["status"] = "approved"
            item["reviewed_at"] = datetime.now().isoformat()
            item["reviewed_by"] = reviewer
            item["review_comment"] = comment
            _save_queue(queue)

            _record_history(item, "approved", reviewer, comment)
            _fire_approval_callbacks(item)
            logger.info("Approved: %s by %s", item_id, reviewer)
            return True
    logger.warning("Item %s not found or not pending", item_id)
    return False


def reject(item_id: str, reviewer: str = "human", comment: str = "") -> bool:
    """拒绝一个待审批的修复。"""
    queue = _load_queue()
    for item in queue:
        if item["id"] == item_id and item["status"] == "pending":
            item["status"] = "rejected"
            item["reviewed_at"] = datetime.now().isoformat()
            item["reviewed_by"] = reviewer
            item["review_comment"] = comment
            _save_queue(queue)

            _record_history(item, "rejected", reviewer, comment)
            logger.info("Rejected: %s by %s", item_id, reviewer)
            return True
    return False


def approve_all_above(confidence_threshold: float = 0.7, reviewer: str = "auto_agent") -> int:
    """批量批准置信度高于阈值的待审批项。

    用于：系统在人工审批前，先自动批准高置信度的项，
    减少人工审批负担。

    Returns:
        批准的数量
    """
    queue = _load_queue()
    count = 0
    for item in queue:
        if item["status"] != "pending":
            continue
        conf = item.get("fix_result", {}).get("confidence", 0)
        if conf >= confidence_threshold:
            item["status"] = "approved"
            item["reviewed_at"] = datetime.now().isoformat()
            item["reviewed_by"] = reviewer
            item["review_comment"] = f"Auto-approved: confidence {conf} >= {confidence_threshold}"
            _record_history(item, "approved", reviewer, item["review_comment"])
            count += 1
    if count:
        _save_queue(queue)
        logger.info("Auto-approved %d items above threshold %.2f", count, confidence_threshold)
    return count


def get_pending() -> list[dict]:
    """获取所有待审批的项。"""
    return [item for item in _load_queue() if item["status"] == "pending"]


def get_queue_stats() -> dict:
    """获取审批队列统计。"""
    queue = _load_queue()
    return {
        "total": len(queue),
        "pending": sum(1 for i in queue if i["status"] == "pending"),
        "approved": sum(1 for i in queue if i["status"] == "approved"),
        "rejected": sum(1 for i in queue if i["status"] == "rejected"),
        "replied": sum(1 for i in queue if i["status"] == "replied"),
        "expired": sum(1 for i in queue if i["status"] == "expired"),
        "recent": queue[-5:] if queue else [],
    }


def reply(item_id: str, message: str, reviewer: str = "human") -> bool:
    """对一个待审批项回复补充信息，状态变为 replied。

    借鉴 HiveWard 的 InboxItem reply 机制：
    审批者可能需要更多信息才能做出决定（比如'这个修复会影响哪些模块？'）。
    replied 状态的项可以被重新评估后再次走审批流程。

    Args:
        item_id: 队列项 ID
        message: 回复内容（补充信息、疑问、建议）
        reviewer: 回复者

    Returns:
        是否成功回复
    """
    queue = _load_queue()
    for item in queue:
        if item["id"] == item_id and item["status"] in ("pending", "replied"):
            item["status"] = "replied"
            replies = item.get("replies", [])
            replies.append({
                "reviewer": reviewer,
                "message": message,
                "at": datetime.now().isoformat(),
            })
            item["replies"] = replies
            _save_queue(queue)
            _record_history(item, "replied", reviewer, message)
            logger.info("Replied to %s: %s", item_id, message[:80])
            return True
    logger.warning("Item %s not found or not in reply-able state", item_id)
    return False


DEFAULT_INBOX_TTL_HOURS = 72  # 3 天未审批自动过期


def _get_inbox_ttl_hours() -> float:
    """从 config.yaml 读取 inbox TTL。"""
    cfg_path = SWARM_DIR / "config.yaml"
    ttl = DEFAULT_INBOX_TTL_HOURS
    if cfg_path.exists():
        try:
            text = cfg_path.read_text(encoding="utf-8")
            import re
            m = re.search(r"inbox_ttl_hours:\s*([\d.]+)", text)
            if m:
                ttl = float(m.group(1))
        except Exception:
            logging.debug("配置文件解析失败，使用默认TTL")
    return ttl


def expire_stale() -> int:
    """将超过 TTL 未处理的 pending/replied 项标记为 expired。

    设计决策（面试话术）：
      "HiveWard 没有过期机制，但我认为审批队列不能无限膨胀。
       如果一个修复提案放了 3 天没人管，说明它要么不重要，
       要么项目已经变了。expired 状态不影响经验积累——
       过期的项仍然记录在 history 里，供后续分析。"

    Returns:
        过期的数量
    """
    queue = _load_queue()
    ttl_hours = _get_inbox_ttl_hours()
    now = datetime.now()
    count = 0
    for item in queue:
        if item["status"] not in ("pending", "replied"):
            continue
        queued_at = item.get("queued_at", "")
        if not queued_at:
            continue
        try:
            queued_time = datetime.fromisoformat(queued_at)
            age_hours = (now - queued_time).total_seconds() / 3600
            if age_hours >= ttl_hours:
                item["status"] = "expired"
                item["expired_at"] = now.isoformat()
                item["expire_reason"] = f"Stale for {age_hours:.0f}h (TTL={ttl_hours:.0f}h)"
                _record_history(item, "expired", "system", item["expire_reason"])
                count += 1
        except (ValueError, TypeError):
            continue
    if count:
        _save_queue(queue)
        logger.info("Expired %d stale inbox items (TTL=%dh)", count, int(ttl_hours))
    return count


_APPROVAL_CALLBACKS: list = []  # list[Callable[[dict], None]]


def register_approval_callback(callback) -> None:
    """注册审批通过后的回调函数。

    approve() 成功后自动调用所有已注册的回调，传入审批项 dict。
    这样 pipeline 不需要轮询队列，而是被动接收通知。

    用法：
        def on_approved(item):
            # item["fix_result"] 包含修复信息，可以自动应用
            apply_fix(item["fix_result"])
        register_approval_callback(on_approved)
    """
    _APPROVAL_CALLBACKS.append(callback)


def _fire_approval_callbacks(item: dict) -> None:
    """触发所有审批回调。"""
    for cb in _APPROVAL_CALLBACKS:
        try:
            cb(item)
        except Exception as e:
            logger.error("Approval callback failed: %s", e)


def _record_history(item: dict, action: str, reviewer: str, comment: str) -> None:
    """记录审批历史。"""
    history = _load_history()
    history.append({
        "item_id": item["id"],
        "action": action,
        "reviewer": reviewer,
        "comment": comment,
        "issue_type": item.get("issue", {}).get("type", ""),
        "file": item.get("issue", {}).get("file", ""),
        "confidence": item.get("fix_result", {}).get("confidence", 0),
        "at": datetime.now().isoformat(),
    })
    if len(history) > 500:
        history = history[-500:]
    _save_history(history)


def get_history(limit: int = 20) -> list[dict]:
    """获取审批历史。"""
    history = _load_history()
    return history[-limit:]



def process_fix(fix_result: FixResult, issue: Issue, apply_fn=None) -> dict:
    """端到端处理一个修复结果：评估 → 自动应用或入队。

    Args:
        fix_result: 修复器返回的结果
        issue: 对应的 Issue
        apply_fn: 实际应用修复的函数（签名：fn(fix_result) -> bool）
                  如果不提供，AUTO_APPLY 的修复只记录不实际应用

    Returns:
        {
            "decision": "auto_apply" | "pending_review" | "rejected",
            "item_id": str | None,
            "applied": bool,
        }

    设计决策（面试话术）：
      "process_fix 是 pipeline 和门控的唯一交互点。
       pipeline 调用 fixer.fix() 拿到结果后，直接传给 process_fix()，
       不需要自己判断置信度。这样门控逻辑是集中管理的，
       改阈值只需要改 config.yaml，不需要到处找代码。"
    """
    decision = evaluate(fix_result)

    if decision == GateDecision.AUTO_APPLY:
        applied = False
        if apply_fn:
            try:
                applied = apply_fn(fix_result)
            except Exception as e:
                logger.error("Auto-apply failed: %s", e)
                item_id = enqueue(fix_result, issue)
                return {"decision": "pending_review", "item_id": item_id, "applied": False}
        _record_history(
            {"id": _generate_item_id(fix_result)},
            "auto_applied" if applied else "auto_apply_noop",
            "system",
            f"Confidence {fix_result.confidence:.2f}",
        )
        return {"decision": "auto_apply", "item_id": None, "applied": applied}

    elif decision == GateDecision.PENDING_REVIEW:
        item_id = enqueue(fix_result, issue)
        return {"decision": "pending_review", "item_id": item_id, "applied": False}

    else:  # REJECTED
        _record_history(
            {"id": _generate_item_id(fix_result)},
            "rejected_low_confidence",
            "system",
            f"Confidence {fix_result.confidence:.2f} below threshold",
        )
        return {"decision": "rejected", "item_id": None, "applied": False}
