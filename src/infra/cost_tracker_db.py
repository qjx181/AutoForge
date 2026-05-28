"""cost_tracker_db.py — SQLite 持久化成本跟踪

职责：
  1. 创建 cost_tracker.db SQLite 数据库
  2. 存储每条 API 调用的成本记录（provider / model / cost / task_id）
  3. 支持按日期查询历史成本趋势
  4. 异常降级到内存模式（不丢失本轮数据）

使用方式：
  from cost_tracker_db import CostTrackerDB
  db = CostTrackerDB()
  db.record_cost("deepseek", "deepseek-v4-flash", 0.15, "task_001")
  hist = db.get_trend(days=7)
"""

import sqlite3
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()
DB_PATH = SWARM_DIR / "data" / "cost_tracker.db"
LOG_FILE = SWARM_DIR / "logs" / "cost_tracker.log"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cost_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,           -- ISO 8601: "2026-05-19 14:30:00"
    date        TEXT NOT NULL,           -- "2026-05-19" (索引列)
    provider    TEXT NOT NULL DEFAULT 'unknown',
    model       TEXT NOT NULL DEFAULT 'unknown',
    cost        REAL NOT NULL DEFAULT 0.0,
    task_id     TEXT,                    -- 可选，关联的任务ID
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cost_date ON cost_records(date);
CREATE INDEX IF NOT EXISTS idx_cost_task ON cost_records(task_id);
"""


class CostTrackerDB:
    """SQLite 成本跟踪器，自动降级到内存模式。"""

    def __init__(self, db_path: Optional[Path] = None):
        self._path = db_path or DB_PATH
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._in_memory = False  # 标记是否降级
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库连接和表结构。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(CREATE_SQL)
            conn.commit()
            self._conn = conn
            self._in_memory = False
        except Exception as e:
            self._log_warning(f"SQLite 初始化失败，降级到内存: {e}")
            self._conn = sqlite3.connect(":memory:")
            self._conn.executescript(CREATE_SQL)
            self._conn.commit()
            self._in_memory = True

    def record_cost(
        self,
        provider: str,
        model: str,
        cost: float,
        task_id: Optional[str] = None,
    ) -> bool:
        """记录一条成本记录。"""
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO cost_records
                       (timestamp, date, provider, model, cost, task_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (timestamp, date_str, provider, model, cost, task_id),
                )
                self._conn.commit()
            return True
        except Exception as e:
            self._log_warning(f"record_cost 失败: {e}")
            return False

    def get_daily_summary(self, date_str: Optional[str] = None) -> dict:
        """获取指定日期的成本汇总。返回 {provider: total_cost}。"""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        try:
            rows = self._conn.execute(
                """SELECT provider, SUM(cost)
                   FROM cost_records
                   WHERE date = ?
                   GROUP BY provider""",
                (date_str,),
            ).fetchall()
            return dict(rows) if rows else {}
        except Exception as e:
            self._log_warning(f"get_daily_summary 失败: {e}")
            return {}

    def get_today_spent(self) -> float:
        """兼容别名 — 供 self_evolve_round.py 的 cost_db.get_today_spent() 调用。"""
        return self.get_total_spent_today()

    def get_total_spent_today(self) -> float:
        """获取当日总花费。"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        try:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM cost_records WHERE date = ?",
                (date_str,),
            ).fetchone()
            return row[0] if row else 0.0
        except Exception as e:
            self._log_warning(f"get_total_spent_today 失败: {e}")
            return 0.0

    def get_trend(self, days: int = 7) -> list[dict]:
        """获取最近 N 天的成本趋势。"""
        try:
            rows = self._conn.execute(
                """SELECT date, SUM(cost) as total
                   FROM cost_records
                   WHERE date >= date('now', ?)
                   GROUP BY date
                   ORDER BY date""",
                (f"-{days} days",),
            ).fetchall()
            return [{"date": r[0], "total": round(r[1], 4)} for r in rows]
        except Exception as e:
            self._log_warning(f"get_trend 失败: {e}")
            return []

    def get_task_costs(self, task_id: str) -> list[dict]:
        """获取指定任务的成本记录。"""
        try:
            rows = self._conn.execute(
                """SELECT timestamp, provider, model, cost
                   FROM cost_records
                   WHERE task_id = ?
                   ORDER BY timestamp""",
                (task_id,),
            ).fetchall()
            return [
                {
                    "timestamp": r[0],
                    "provider": r[1],
                    "model": r[2],
                    "cost": r[3],
                }
                for r in rows
            ]
        except Exception as e:
            self._log_warning(f"get_task_costs 失败: {e}")
            return []

    @property
    def is_in_memory(self) -> bool:
        return self._in_memory

    def close(self) -> None:
        """关闭连接。"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @staticmethod
    def _log_warning(msg: str) -> None:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: {msg}\n")
        except Exception:
            pass  # 静默失败，避免递归


# ─── 快捷函数（供 self_evolve_round.py 直接调用） ─────────────────────

_db_instance: Optional[CostTrackerDB] = None
_db_lock = threading.Lock()


def get_db() -> CostTrackerDB:
    """获取全局 CostTrackerDB 单例。"""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = CostTrackerDB()
    return _db_instance


def record_cost(provider: str, model: str, cost: float, task_id: Optional[str] = None) -> bool:
    """快捷记录成本。"""
    return get_db().record_cost(provider, model, cost, task_id)


def get_today_spent() -> float:
    """快捷获取当日总花费。"""
    return get_db().get_total_spent_today()


def get_cost_trend(days: int = 7) -> list[dict]:
    """快捷获取成本趋势。"""
    return get_db().get_trend(days)


def get_cost_summary(date_str: Optional[str] = None) -> dict:
    """快捷获取日汇总。"""
    return get_db().get_daily_summary(date_str)


# ─── main（测试入口） ──────────────────────────────────────────────────

if __name__ == "__main__":
    db = CostTrackerDB()
    print(f"数据库路径: {db._path}")
    print(f"降级到内存: {db.is_in_memory}")

    db.record_cost("deepseek", "deepseek-v4-flash", 0.15, "test_001")
    db.record_cost("ollama", "qwen2.5:7b", 0.0, "test_001")
    print(f"今日花费: ${db.get_total_spent_today():.4f}")
    print(f"趋势(3天): {db.get_trend(3)}")
    print(f"任务成本: {db.get_task_costs('test_001')}")
    db.close()
