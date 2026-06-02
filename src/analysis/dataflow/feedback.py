"""feedback.py — 误报反馈循环。

核心机制：
  1. 用户标记某个 finding 为误报 → 记录 (filepath, source_kind, sink_kind, 代码模式)
  2. 同一模式被标记 2 次 → 自动抑制，后续扫描不再报
  3. 抑制记录持久化到 JSON 文件，跨扫描生效
  4. 支持"解除抑制"（误标了可以撤回）

数据结构：
  {
    "suppressed_patterns": [
      {
        "key": "filepath:source_kind:sink_kind:pattern_hash",
        "filepath": "...",
        "source_kind": "http_param",
        "sink_kind": "sql_query",
        "code_pattern": "cursor.execute(f\"...{user_id}...\")",
        "suppress_count": 2,
        "first_seen": "2026-06-01T...",
        "last_seen": "2026-06-01T...",
      }
    ],
    "stats": {
      "total_marked": 15,
      "total_suppressed": 5,
      "total_automatically_skipped": 42
    }
  }
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .schemas import SinkKind, SourceKind

logger = logging.getLogger(__name__)

# 同一模式被标记为误报多少次后自动抑制
SUPPRESS_THRESHOLD = 2

# 默认存储路径
DEFAULT_STORE_PATH = Path(__file__).parent.parent.parent.parent / "data" / "feedback_store.json"


class FeedbackStore:
    """误报反馈存储。

    用法：
        store = FeedbackStore()  # 自动从默认路径加载
        store.mark_false_positive(candidate)
        store.is_suppressed(candidate)  # → True/False
        store.save()
    """

    def __init__(self, store_path: Path | str | None = None):
        self._path = Path(store_path) if store_path else DEFAULT_STORE_PATH
        self._data: dict[str, Any] = self._load()
        self._stats = self._data.get("stats", {
            "total_marked": 0,
            "total_suppressed": 0,
            "total_automatically_skipped": 0,
        })

    def _load(self) -> dict[str, Any]:
        """从文件加载反馈数据。"""
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("反馈文件损坏，重新初始化: %s", e)
        return {"suppressed_patterns": [], "stats": {}}

    def save(self) -> None:
        """持久化到文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data["stats"] = self._stats
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def mark_false_positive(
        self,
        filepath: str,
        source_kind: str,
        sink_kind: str,
        code_pattern: str,
    ) -> dict[str, Any]:
        """标记一个 finding 为误报。

        Returns:
            {"suppressed": bool, "count": int} — 是否达到了抑制阈值
        """
        key = self._make_key(filepath, source_kind, sink_kind, code_pattern)
        patterns = self._data.setdefault("suppressed_patterns", [])

        # 查找已有记录
        existing = None
        for p in patterns:
            if p["key"] == key:
                existing = p
                break

        if existing:
            existing["suppress_count"] += 1
            existing["last_seen"] = datetime.now().isoformat()
        else:
            existing = {
                "key": key,
                "filepath": filepath,
                "source_kind": source_kind,
                "sink_kind": sink_kind,
                "code_pattern": code_pattern[:300],
                "suppress_count": 1,
                "first_seen": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
            }
            patterns.append(existing)

        self._stats["total_marked"] = self._stats.get("total_marked", 0) + 1

        if existing["suppress_count"] >= SUPPRESS_THRESHOLD:
            self._stats["total_suppressed"] = self._stats.get("total_suppressed", 0) + 1
            self.save()
            return {"suppressed": True, "count": existing["suppress_count"]}

        self.save()
        return {"suppressed": False, "count": existing["suppress_count"]}

    def is_suppressed(
        self,
        filepath: str,
        source_kind: str,
        sink_kind: str,
        code_pattern: str,
    ) -> bool:
        """检查某个 finding 是否已被抑制。"""
        key = self._make_key(filepath, source_kind, sink_kind, code_pattern)
        for p in self._data.get("suppressed_patterns", []):
            if p["key"] == key and p["suppress_count"] >= SUPPRESS_THRESHOLD:
                self._stats["total_automatically_skipped"] = (
                    self._stats.get("total_automatically_skipped", 0) + 1
                )
                return True
        return False

    def unmark_false_positive(
        self,
        filepath: str,
        source_kind: str,
        sink_kind: str,
        code_pattern: str,
    ) -> bool:
        """解除误报标记（撤销）。"""
        key = self._make_key(filepath, source_kind, sink_kind, code_pattern)
        patterns = self._data.get("suppressed_patterns", [])
        for i, p in enumerate(patterns):
            if p["key"] == key:
                patterns.pop(i)
                self.save()
                return True
        return False

    def get_stats(self) -> dict[str, Any]:
        """获取反馈统计。"""
        return {
            **self._stats,
            "active_suppressions": sum(
                1 for p in self._data.get("suppressed_patterns", [])
                if p.get("suppress_count", 0) >= SUPPRESS_THRESHOLD
            ),
            "pending_suppressions": sum(
                1 for p in self._data.get("suppressed_patterns", [])
                if 0 < p.get("suppress_count", 0) < SUPPRESS_THRESHOLD
            ),
        }

    def get_suppressed_patterns(self) -> list[dict]:
        """获取所有已抑制的模式列表。"""
        return [
            p for p in self._data.get("suppressed_patterns", [])
            if p.get("suppress_count", 0) >= SUPPRESS_THRESHOLD
        ]

    @staticmethod
    def _make_key(filepath: str, source_kind: str, sink_kind: str, code_pattern: str) -> str:
        """生成模式的唯一 key。

        用 (filepath, source_kind, sink_kind, 代码模式哈希) 四元组。
        代码模式只取前 100 字符做哈希，避免微小格式差异导致不匹配。
        """
        # 标准化代码模式：去掉空白差异
        normalized = " ".join(code_pattern[:100].split())
        pattern_hash = hashlib.md5(normalized.encode()).hexdigest()[:12]
        return f"{filepath}:{source_kind}:{sink_kind}:{pattern_hash}"
