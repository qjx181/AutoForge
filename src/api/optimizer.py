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



START_TIME = datetime.datetime.now()




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





def _run_optimization_in_bg(target_dir: str, dimensions: list[str],
                            run_id: str, dry_run: bool) -> None:
    """后台执行优化扫描，结果写回 JSON 文件"""
    import sys as _sys
    import traceback as _tb
    _SRC = PROJECT_DIR / "src"
    if str(_SRC) not in _sys.path:
        _sys.path.insert(0, str(_SRC))
    if str(PROJECT_DIR) not in _sys.path:
        _sys.path.insert(0, str(PROJECT_DIR))
    try:
        from src.analysis.optimizer_core import run_full_pipeline
        result = run_full_pipeline(target_dir, dimensions=dimensions)

        def _make_json_safe(obj):
            if hasattr(obj, '__dict__'):
                return _make_json_safe(obj.__dict__)
            if isinstance(obj, dict):
                return {k: _make_json_safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_make_json_safe(i) for i in obj]
            if isinstance(obj, (str, int, float, bool, type(None))):
                return obj
            if isinstance(obj, (datetime.datetime,)):
                return obj.isoformat()
            if isinstance(obj, Path):
                return str(obj)
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)

        result = _make_json_safe(result)

        result["target_dir"] = target_dir
        result["dry_run"] = dry_run
        result["run_id"] = run_id
        result["finished_at"] = datetime.datetime.now().isoformat()
        result["status"] = "completed"

        _write_json(OPT_RUNS_DIR / f"{run_id}.json", result)
    except Exception as e:
        _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
            "run_id": run_id,
            "target_dir": target_dir,
            "dimensions": dimensions,
            "dry_run": dry_run,
            "status": "failed",
            "error": str(e),
            "traceback": _tb.format_exc(),
            "finished_at": datetime.datetime.now().isoformat(),
        })
    finally:
        run_lock = OPT_RUNS_DIR / f"{run_id}.running"
        if run_lock.exists():
            run_lock.unlink()


@app.post("/api/optimize")
async def start_optimization(body: dict):
    """启动优化扫描

    POST JSON body:
        target_dir: str  — 目标项目路径（必填）
        dimensions: list[str] | null — 维度列表，null=全部
        dry_run: bool — true=仅扫描，false=扫描+修复（默认true）

    Returns:
        dict: { run_id, status }
    """
    target_dir = body.get("target_dir", "").strip()
    if not target_dir:
        raise HTTPException(status_code=400, detail="target_dir 是必填字段")
    target_path = Path(target_dir)
    if not target_path.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在: {target_dir}")
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {target_dir}")

    dimensions = body.get("dimensions", None)
    dry_run = body.get("dry_run", True)

    import uuid
    run_id = uuid.uuid4().hex[:12]
    _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
        "run_id": run_id,
        "target_dir": target_dir,
        "dimensions": dimensions,
        "dry_run": dry_run,
        "status": "running",
        "started_at": datetime.datetime.now().isoformat(),
        "finished_at": None,
    })
    (OPT_RUNS_DIR / f"{run_id}.running").write_text("1")

    import threading
    t = threading.Thread(
        target=_run_optimization_in_bg,
        args=(target_dir, dimensions, run_id, dry_run),
        daemon=True,
    )
    t.start()

    return {"run_id": run_id, "status": "running",
            "message": f"优化已启动（{'仅扫描' if dry_run else '扫描+修复'}），请稍后查看结果"}
