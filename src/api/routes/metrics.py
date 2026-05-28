#!/usr/bin/env python3
"""api_service.py — FastAPI 服务 + Web 仪表盘

位于 src/api/ 目录，入口为 api_entrypoint()。
直接运行：python src/api/api_service.py
Docker 入口：docker-entrypoint.sh api（挂载 /app → 容器 /app）

提供 HTTP 接口和 Web 仪表盘，让用户通过网页控制 swarm。

端点:
  POST /api/tasks        — 提交新任务
  GET  /api/tasks        — 列出所有任务
  GET  /api/tasks/:id    — 查看单个任务
  DELETE /api/tasks/:id  — 删除任务
  POST /api/trigger      — 触发一轮进化
  GET  /api/metrics      — 核心指标
  GET  /api/status       — 完整状态
  GET  /api/logs         — 查看日志
  POST /api/bugs         — 提交 Bug 或扫描项目
  GET  /api/bugs         — 列出所有 Bug
  GET  /api/bugs/:id     — 查看 Bug 详情
  POST /api/bugs/:id/fix — 执行 Bug 修复
  GET  /api/bugs/:id/fix — 查看修复结果
  GET  /health           — 健康检查
  GET  /                 — 返回 Web 仪表盘

设计理由:
  - 所有数据从磁盘文件读取，不依赖内存状态，重启后数据不丢失
  - CORS 全开，方便开发调试
  - 日志读取限制 100 行避免大文件加载
  面试官可能问:
  - 为什么不用数据库？答：项目三的状态数据是文件化的（TODO.md、state.json），保持一致性
  - 触发进化用后台任务？答：subprocess.Popen 独立于 FastAPI 生命周期，避免阻塞
"""

import json
import os
import subprocess
import datetime
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# ── 路径（向上两级，从 src/api/ 到项目根目录）────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
_SRC_ROOT = Path(__file__).parent.parent.resolve()
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

PROJECT_DIR = _PROJECT_ROOT
SRC_DIR = _SRC_ROOT
STATE_FILE = PROJECT_DIR / "state.json"
TODO_FILE = PROJECT_DIR / "TODO.md"
LOGS_DIR = PROJECT_DIR / "logs"


# ── 应用 ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="项目三：多Agent — API 服务",
    description="Swarm 多Agent 自进化引擎的 HTTP 接口和 Web 仪表盘",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 启动时间 ─────────────────────────────────────────────────────────────

START_TIME = datetime.datetime.now()


# ── 辅助函数 ────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    """安全读取 JSON 文件

    Returns:
        dict: 解析后的 JSON 内容。文件不存在或解析失败返回空字典。
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    """安全写入 JSON 文件（原子写入）"""
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _read_lines(path: Path, n: int = 50) -> list[str]:
    """读取文件最后 N 行"""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError:
        return []


def _parse_task_from_match(task_match: re.Match) -> dict:
    """从正则匹配创建新任务字典。"""
    done = task_match.group(1) == "x"
    return {
        "id": task_match.group(2),
        "status": "completed" if done else "pending",
        "description": "",
        "category": "debug",
        "depends": [],
    }


def _update_task_from_line(current_task: dict, line: str) -> None:
    """根据行内容更新当前任务（描述/依赖/类别）。"""
    # 使用映射减少 if/elif 链深度
    _UPDATERS = {
        "描述:": lambda l, t: t.update({"description": l.split("描述:", 1)[1].strip()}),
        "依赖:": lambda l, t: t.update({
            "depends": [d.strip() for d in l.split("依赖:", 1)[1].strip().split(",")]
            if l.split("依赖:", 1)[1].strip() and l.split("依赖:", 1)[1].strip() != "无"
            else []}),
        "类别:": lambda l, t: t.update({"category": l.split("类别:", 1)[1].strip()}),
    }
    for prefix, updater in _UPDATERS.items():
        if prefix in line:
            updater(line, current_task)
            return


def _parse_tasks_from_todo() -> list[dict]:
    """从 TODO.md 解析任务列表

    Returns:
        list[dict]: 任务列表，每项含 id, description, status, category, depends
    """
    if not TODO_FILE.exists():
        return []

    tasks = []
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    current_task = None
    for line in lines:
        # [ ] 或 [x] 标记
        task_match = __import__("re").match(
            r"^- \[([ x])\] 任务ID:\s*(\S+)", line
        )
        if task_match:
            if current_task:
                tasks.append(current_task)
            current_task = _parse_task_from_match(task_match)
        elif current_task and (":" in line):
            _update_task_from_line(current_task, line)

    if current_task:
        tasks.append(current_task)

    return tasks


# ── 路由 ────────────────────────────────────────────────────────────────



@app.get("/api/metrics")
async def get_metrics():
    """返回核心指标

    Returns:
        dict: {
            current_round: int,
            completed_tasks: int,
            pending_tasks: int,
            success_rate: float,
            dollar_spent_today: float,
            dollar_limit: float,
            uptime_seconds: float,
            last_round_at: str,
            rounds_total: int,
        }
    """
    state = _read_json(STATE_FILE)
    tasks = _parse_tasks_from_todo()

    completed = sum(1 for t in tasks if t["status"] == "completed")
    pending = len(tasks) - completed
    total = len(tasks)

    budget = state.get("daily_budget", {})
    rounds_history = state.get("completed_task_ids", [])
    # 从 self_evolve_log.json 读取轮次
    evo_log = _read_json(PROJECT_DIR / "self_evolve_log.json")
    rounds = evo_log.get("rounds", []) if isinstance(evo_log, dict) else []

    return {
        "current_round": len(rounds) + 1,
        "completed_tasks": completed,
        "pending_tasks": pending,
        "total_tasks": total,
        "success_rate": round(completed / max(total, 1) * 100, 1),
        "dollar_spent_today": budget.get("dollar_spent_today", 0),
        "dollar_limit": budget.get("dollar_limit", 5.0),
        "uptime_seconds": round((datetime.datetime.now() - START_TIME).total_seconds(), 1),
        "rounds_total": len(rounds),
        "last_round_at": rounds[-1].get("timestamp", "") if rounds else "",
    }
