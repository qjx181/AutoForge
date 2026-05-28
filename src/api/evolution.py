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





def _run_evolution_task(target_dir: str, dimensions: list, max_fixes: int, run_id: str, progress_cb) -> None:
    """后台执行单轮进化循环。"""
    import sys as _sys
    import traceback as _tb
    _SRC = PROJECT_DIR / "src"
    for p in [str(_SRC), str(PROJECT_DIR)]:
        if p not in _sys.path:
            _sys.path.insert(0, p)
    try:
        from src.analysis.evolution_engine import run_evolution_round
        result = run_evolution_round(
            target_dir=target_dir,
            dimensions=dimensions,
            max_fixes_per_round=max_fixes,
            progress_callback=progress_cb,
        )
        result["run_id"] = run_id
        result = _make_json_safe(result)
        _write_json(OPT_RUNS_DIR / f"{run_id}.json", result)
        _update_auto_progress(run_id, {
            "status": "completed",
            "phase": "done",
            "message": f"进化完成！评分 {result.get('score_before','?')}→{result.get('score_after','?')}，修复 {result.get('fixes',{}).get('succeeded',0)} 个问题",
            "score_before": result.get("score_before"),
            "score_after": result.get("score_after"),
            "fixes_succeeded": result.get("fixes",{}).get("succeeded",0),
            "fixes_failed": result.get("fixes",{}).get("failed",0),
        })
    except Exception as e:
        _update_auto_progress(run_id, {"status": "failed", "phase": "error", "error": str(e)})
        _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
            "run_id": run_id, "status": "failed", "error": str(e),
        })
    finally:
        run_lock = OPT_RUNS_DIR / f"{run_id}.running"
        if run_lock.exists():
            run_lock.unlink()


def _make_json_safe(obj):
    """递归清理对象使其可 JSON 序列化。"""
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


@app.post("/api/optimize/evolve")
async def start_evolution(body: dict):
    """启动单轮进化循环：扫描 → 修复 → 验证 → 重扫

    POST JSON body:
        target_dir: str  — 目标项目路径（必填）
        dimensions: list[str] | null — 维度列表，null=全部
        max_fixes: int — 每轮最大修复数（默认 30）

    Returns:
        dict: { run_id, status }
    """
    target_dir = body.get("target_dir", "").strip()
    if not target_dir:
        raise HTTPException(status_code=400, detail="target_dir 是必填字段")
    wsl_path, target_path = _convert_windows_path(target_dir)
    if not target_path.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在（已转换为: {wsl_path}）")
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {wsl_path}")

    max_fixes = body.get("max_fixes", 30)
    dimensions = body.get("dimensions", None)
    import uuid
    run_id = uuid.uuid4().hex[:12]

    _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
        "run_id": run_id,
        "target_dir": target_dir,
        "dimensions": dimensions,
        "type": "evolve",
        "status": "running",
        "started_at": datetime.datetime.now().isoformat(),
    })
    (OPT_RUNS_DIR / f"{run_id}.running").write_text("1")

    def _progress_cb(phase, data):
        data["phase"] = phase
        data["status"] = "running"
        _update_auto_progress(run_id, data)

    import threading
    t = threading.Thread(
        target=_run_evolution_task,
        args=(target_dir, dimensions, max_fixes, run_id, _progress_cb),
        daemon=True,
    )
    t.start()

    return {"run_id": run_id, "status": "running", "type": "evolve",
            "message": f"进化循环已启动！将扫描 → 修复（最多{max_fixes}个）→ 验证 → 重扫"}


@app.post("/api/optimize/auto")
async def start_auto_optimize(body: dict):
    """启动持续优化循环

    POST JSON body:
        target_dir: str  — 目标项目路径（必填）
        dimensions: list[str] | null — 维度列表，null=全部

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
    import uuid
    run_id = uuid.uuid4().hex[:12]

    _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
        "run_id": run_id,
        "target_dir": target_dir,
        "dimensions": dimensions,
        "type": "auto",
        "status": "running",
        "started_at": datetime.datetime.now().isoformat(),
    })
    (OPT_RUNS_DIR / f"{run_id}.running").write_text("1")

    _update_auto_progress(run_id, {
        "status": "running",
        "phase": "starting",
        "round": 0,
        "score_history": [],
        "message": "准备启动持续优化循环...",
    })

    import threading
    t = threading.Thread(
        target=_auto_optimize_loop,
        args=(target_dir, dimensions, run_id),
        daemon=True,
    )
    t.start()

    return {"run_id": run_id, "status": "running",
            "type": "auto",
            "message": "持续优化循环已启动！它将自动扫描→修复→重扫→再优化，直到分数稳定"}


@app.get("/api/optimize/auto/{run_id}/progress")
async def get_auto_progress(run_id: str):
    """获取持续优化循环的实时进度"""
    progress_file = OPT_RUNS_DIR / f"{run_id}.progress"
    if not progress_file.exists():
        run_file = OPT_RUNS_DIR / f"{run_id}.json"
        if not run_file.exists():
            raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在")
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
            return data
        except json.JSONDecodeError:
            return {"run_id": run_id, "status": "unknown", "error": "无法读取进度"}
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        running_file = OPT_RUNS_DIR / f"{run_id}.running"
        if not running_file.exists() and data.get("status") == "running":
            data["status"] = "completed"
        return data
    except json.JSONDecodeError:
        return {"run_id": run_id, "status": "unknown"}


@app.get("/api/optimizer", response_class=HTMLResponse)
async def optimizer_page():
    """返回优化引擎操作页面"""
    from fastapi.responses import HTMLResponse
    opt_file = PROJECT_DIR / "api" / "optimizer.html"
    if opt_file.exists():
        return HTMLResponse(content=opt_file.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<html><body style='background:#0d1117;color:#c9d1d9;padding:40px;font-family:sans-serif'>"
                "<h1>优化器页面未找到</h1><p>请检查 api/optimizer.html 是否存在</p></body></html>"
    )




@app.post("/api/optimize/start-agent")
async def start_agent_evolution(body: dict):
    """启动多Agent持续自进化：设置目标路径 + 恢复 cronjob（不做一次性扫描）

    POST JSON body:
        target_dir: str  — 目标项目路径（必填）
        start_now: bool — 是否立即触发一轮（默认 true）

    Returns:
        dict: { cronjob_status, target_dir, message }
    """
    target_dir = body.get("target_dir", "").strip()
    if not target_dir:
        raise HTTPException(status_code=400, detail="target_dir 是必填字段")

    wsl_path = target_dir
    if ":" in target_dir and not target_dir.startswith("/"):
        drive = target_dir[0].lower()
        rest = target_dir[2:].replace("\\", "/")
        wsl_path = f"/mnt/{drive}{rest}"

    target_path = Path(wsl_path)
    if not target_path.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在（已转换为: {wsl_path}）")
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {wsl_path}")

    target_file = PROJECT_DIR / "data" / "opt_target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(wsl_path + "\n", encoding="utf-8")

    cron_msg = "cronjob_resumed"
    try:
        import subprocess as _sp
        cr = _sp.run(
            ["python3", "-m", "hermes_cli.main", "cron", "resume", "79cb9d06dc5d"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_DIR),
        )
        if cr.returncode != 0:
            cron_msg = f"cron_warning: {cr.stderr[:100]}"
    except Exception as e:
        cron_msg = f"cron_warning: {e}"

    run_marker = PROJECT_DIR / "data" / ".current_run.json"
    run_marker.parent.mkdir(parents=True, exist_ok=True)
    run_marker.write_text(json.dumps({
        "status": "continuous",
        "target_dir": wsl_path,
        "phase": "running",
        "started_at": datetime.datetime.now().isoformat(),
        "message": "持续进化中，每30分钟一轮",
    }), encoding="utf-8")

    return {
        "status": "continuous",
        "target_dir": wsl_path,
        "cronjob": cron_msg,
        "cronjob_name": "swarm-evolve-round",
        "cronjob_schedule": "每30分钟",
        "message": f"多Agent持续自进化已启动！目标：{wsl_path}。每30分钟自动跑一轮。点击停止按钮可暂停。",
    }

    return {
        "status": "started",
        "target_dir": wsl_path,
        "cronjob": cron_msg,
        "cronjob_name": "swarm-evolve-round",
        "cronjob_schedule": "每30分钟",
        "message": f"多Agent自进化已启动！目标：{wsl_path}。cronjob 每30分钟自动运行。同时已触发即时扫描。",
    }
