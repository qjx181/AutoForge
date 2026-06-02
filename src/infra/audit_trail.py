import hashlib
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
AUDIT_DIR = SWARM_DIR / "logs"
AUDIT_FILE = AUDIT_DIR / "audit.jsonl"
STATE_FILE = SWARM_DIR / "data" / "state.json"

MAX_BYTES = int(os.environ.get("AUDIT_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
BACKUP_COUNT = int(os.environ.get("AUDIT_BACKUP_COUNT", "3"))

SENSITIVE_PATTERNS = [
    "api_key", "api_secret", "password", "token",
    "authorization", "bearer", "secret_key", "auth_key",
]

_current_round: Optional[int] = None


def _get_round() -> int:
    """获取当前 round 编号。"""
    global _current_round
    if _current_round is not None:
        return _current_round
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            _current_round = state.get("current_round", 0)
        else:
            _current_round = 0
    except Exception:
        _current_round = 0
    return _current_round




def summarize(content: str, max_len: int = 80) -> str:
    """生成内容摘要行。

    Args:
        content: 原始内容
        max_len: 摘要最大长度

    Returns:
        摘要字符串（如 "def create_session... +def create_session async"）
    """
    if not content:
        return ""

    stripped = content.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                keys = list(parsed.keys())[:5]
                return f"JSON: keys={keys}"
        except (json.JSONDecodeError, ValueError):
                        logging.exception('异常捕获: ')

    first_line = stripped.split("\n")[0].strip()
    if len(first_line) > max_len:
        first_line = first_line[: max_len - 3] + "..."

    return first_line


def content_hash(content: str) -> str:
    """计算内容的 SHA-256 摘要（用于验证内容完整性）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def redact_sensitive(text: str) -> str:
    """脱敏：将内容中的敏感值替换为 ***。

    查找 SENSITIVE_PATTERNS 中定义的 key=value 模式并脱敏 value。
    """
    import re
    result = text
    for pattern in SENSITIVE_PATTERNS:
        result = re.sub(
            rf'({pattern}\s*[=:]\s*)["\']?([^"\' \n,;}}]{{8,}})["\']?',
            r"\1***",
            result,
            flags=re.IGNORECASE,
        )
    return result




def _rotate_if_needed():
    """如果审计文件超过 MAX_BYTES，进行轮转。"""
    if not AUDIT_FILE.exists():
        return

    size = AUDIT_FILE.stat().st_size
    if size < MAX_BYTES:
        return

    for i in range(BACKUP_COUNT - 1, 0, -1):
        old = AUDIT_DIR / f"audit.jsonl.{i}"
        new = AUDIT_DIR / f"audit.jsonl.{i + 1}"
        if old.exists():
            old.rename(new)

    AUDIT_FILE.rename(AUDIT_DIR / "audit.jsonl.1")




def audit_log(
    operation: str,
    target: str,
    content_preview: str = "",
    success: bool = True,
    source: str = "unknown",
    extra: Optional[dict] = None,
) -> None:
    """记录一条审计日志。

    Args:
        operation: 操作类型（write_file / patch / delete / confirm）
        target: 操作目标文件路径
        content_preview: 内容摘要（自动脱敏）
        success: 是否成功
        source: 调用来源（模块名）
        extra: 附加信息字典
    """
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()

        safe_preview = redact_sensitive(content_preview[:200])

        record = {
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "operation": operation,
            "target": str(target),
            "content_summary": safe_preview[:80] if safe_preview else "",
            "content_hash": content_hash(content_preview) if content_preview else "",
            "success": success,
            "round": _get_round(),
        }
        if extra:
            safe_extra = {}
            for k, v in extra.items():
                safe_extra[k] = redact_sensitive(str(v)) if isinstance(v, str) else v
            record.update(safe_extra)

        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            line = json.dumps(record, ensure_ascii=False)
            f.write(line + "\n")

    except Exception:
        logging.exception("审计日志写入失败")


def get_recent_ops(limit: int = 20, operation: Optional[str] = None) -> list[dict]:
    """查询最近的审计日志。

    Args:
        limit: 返回条数
        operation: 可选的操作类型过滤

    Returns:
        最近 N 条审计事件
    """
    if not AUDIT_FILE.exists():
        return []

    records = []
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if operation and record.get("operation") != operation:
                        continue
                    records.append(record)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    return records[-limit:]


def print_summary(limit: int = 10) -> None:
    """打印最近的审计事件摘要。"""
    records = get_recent_ops(limit=limit)
    if not records:
        logging.info("[audit] 暂无审计记录")
        return

    logging.info(f"[audit] 最近 {len(records)} 条操作记录:")
    for r in reversed(records):
        status = "✅" if r.get("success") else "❌"
        ts = r.get("timestamp", "?")[11:19]  # 只显示 HH:MM:SS
        op = r.get("operation", "?").ljust(12)
