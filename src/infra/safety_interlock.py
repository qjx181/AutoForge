"""safety_interlock.py — 访问控制与操作确认模块

职责：
  1. 关键操作二次确认：删除文件、git push、覆写文件
  2. 权限校验：操作前检查是否允许执行
  3. 操作记录：所有拦截/拒绝/确认事件写入审计日志

使用方式：
  from safety_interlock import confirm_destructive_op
  if not confirm_destructive_op("delete_file", path):
      return  # 用户拒绝，跳过

设计原则：
  - 默认"拒绝所有危险操作"（Deny-by-default）
  - 每轮最多 N 次危险操作（防失控）
  - 配置白名单（可绕过确认的路径模式）
"""

import json
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
STATE_FILE = SWARM_DIR / "data" / "state.json"
AUDIT_FILE = SWARM_DIR / "logs" / "audit.jsonl"

DANGEROUS_OPS = {
    "delete_file": {
        "level": "critical",
        "description": "删除文件",
        "max_per_round": 3,
        "whitelist_patterns": ["*.pyc", "__pycache__/*", ".pytest_cache/*", "logs/*", "heartbeats/*", "tmp_agent/*"],
    },
    "git_push": {
        "level": "high",
        "description": "推送代码到远程仓库",
        "max_per_round": 1,
        "whitelist_patterns": [],
    },
    "overwrite_file": {
        "level": "warning",
        "description": "覆写已有文件",
        "max_per_round": 10,
        "whitelist_patterns": ["logs/*.json", "*.csv", "*.jsonl"],
    },
    "clear_state": {
        "level": "critical",
        "description": "清空 state.json / 重置进度",
        "max_per_round": 1,
        "whitelist_patterns": [],
    },
}

_round_op_count: dict[str, int] = {}
_current_round: Optional[int] = None


def _get_round() -> int:
    """从 state.json 获取当前 round 编号。"""
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


def _reset_count_if_new_round():
    """如果 round 变了，重置计数器。"""
    r = _get_round()
    if _round_op_count.get("_round") != r:
        _round_op_count.clear()
        _round_op_count["_round"] = r




def _matches_whitelist(path: str, patterns: list[str]) -> bool:
    """检查路径是否匹配白名单模式（简单通配符匹配）。"""
    p = Path(path)
    for pattern in patterns:
        if pattern.endswith("/*"):
            if pattern[:-2] in str(p.parent):
                return True
        elif p.match(pattern):
            return True
        elif p.suffix == pattern:
            return True
    return False


def _is_inside_dangerous_dir(path: str) -> bool:
    """检查路径是否在项目关键目录内。"""
    dangerous_dirs = [str(SWARM_DIR / d) for d in
                      ["self_evolve", "services", "routes", "core"]]
    abs_path = str(Path(path).resolve())
    for d in dangerous_dirs:
        if d in abs_path:
            return True
    return False




def confirm_destructive_op(
    op_type: str,
    path_or_description: str,
    force_skip: bool = False,
) -> bool:
    """对危险操作进行二次确认。

    Args:
        op_type: 操作类型（delete_file / git_push / overwrite_file / clear_state）
        path_or_description: 操作目标路径或描述
        force_skip: True = 跳过确认直接允许（用于测试）

    Returns:
        True = 允许执行，False = 拒绝执行

    流程：
      1. 检查操作类型是否在定义中 → 不在则拒绝
      2. 检查路径是否在白名单中 → 在白名单中直接允许
      3. 检查本轮已执行次数 → 超出 max_per_round 拒绝
      4. 输出警告并等待确认
    """
    if force_skip:
        return True

    op_def = DANGEROUS_OPS.get(op_type)
    if not op_def:
        _log_audit("reject", op_type, path_or_description, "未知操作类型")
        return False

    if _matches_whitelist(path_or_description, op_def["whitelist_patterns"]):
        _log_audit("whitelist_pass", op_type, path_or_description, "白名单")
        return True

    _reset_count_if_new_round()
    current = _round_op_count.get(op_type, 0)
    if current >= op_def["max_per_round"]:
        msg = (
            f"本轮已执行 {current}/{op_def['max_per_round']} 次 "
            f"'{op_def['description']}'，已达上限"
        )
        _log_audit("reject", op_type, path_or_description, msg)
        logging.info(f"[safety] ⛔ {msg}")
        return False

    logging.info(f"\n⚠️  【安全确认】危险操作: {op_def['description']}")
    logging.info(f"  级别: {op_def['level']}")
    logging.info(f"  目标: {path_or_description}")
    logging.info(f"  本轮已执行: {current}/{op_def['max_per_round']}")
    logging.info(f"  确认执行请输入: CONFIRM (大写)，其他任意键跳过")
    logging.info(f"  > ", end="", flush=True)

    try:
        user_input = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        user_input = ""

    if user_input == "CONFIRM":
        _round_op_count[op_type] = current + 1
        _log_audit("confirm", op_type, path_or_description, "用户确认执行")
        logging.info(f"[safety] ✅ 已确认")
        return True
    else:
        _log_audit("skip", op_type, path_or_description, "用户跳过")
        logging.info(f"[safety] ⏭ 已跳过")
        return False


def guard_delete(path: str, force_skip: bool = False) -> bool:
    """删除操作的专用守卫。"""
    protected = [
        "config.yaml",
        ".gitignore",
        "TODO.md",
        "state.json",
        "delegable_tasks.json",
        "agent_capability_map.json",
        "self_evolve_round.py",
        "swarm_metrics.py",
    ]
    filename = Path(path).name
    if filename in protected:
        msg = f"保护文件 {filename} 禁止删除"
        _log_audit("block", "delete_file", path, msg)
        logging.info(f"[safety] 🛑 {msg}")
        return False

    return confirm_destructive_op("delete_file", path, force_skip)


def guard_git_push(force_skip: bool = False) -> bool:
    """git push 操作的专用守卫。"""
    return confirm_destructive_op("git_push", "git push 到远程仓库", force_skip)




def _log_audit(action: str, op_type: str, target: str, reason: str):
    """将安全拦截事件写入审计日志。"""
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "source": "safety_interlock",
            "action": action,
            "operation": op_type,
            "target": target,
            "reason": reason,
            "round": _get_round(),
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计日志写入失败不应该影响主逻辑


if __name__ == "__main__":
    logging.info(f"[safety] 测试模式")
    logging.info(f"  Round: {_get_round()}")

    result = guard_delete("/tmp/test.py", force_skip=True)
    logging.info(f"  guard_delete(/tmp/test.py): {'允许' if result else '拒绝'}")

    result = guard_delete("config.yaml", force_skip=True)
    logging.info(f"  guard_delete(config.yaml): {'允许' if result else '拒绝'}")

    result = guard_git_push(force_skip=True)
    logging.info(f"  guard_git_push: {'允许' if result else '拒绝'}")
