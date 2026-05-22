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


@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "project3-swarm-api",
        "timestamp": datetime.datetime.now().isoformat(),
        "uptime_seconds": (datetime.datetime.now() - START_TIME).total_seconds(),
    }


@app.get("/api/tasks")
async def list_tasks():
    """列出所有任务

    Returns:
        list[dict]: 任务列表（来自 TODO.md + state.json）
    """
    return _parse_tasks_from_todo()


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """查看单个任务详情"""
    tasks = _parse_tasks_from_todo()
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise HTTPException(status_code=404, detail=f"任务 {task_id} 未找到")


@app.post("/api/tasks")
async def create_task(task: dict):
    """提交新任务

    Args:
        task: JSON body 包含 task_id, description, category, depends

    Returns:
        dict: 创建结果

    Why:
        将任务追加到 TODO.md 而非 state.json，保持一致性
    """
    task_id = task.get("task_id", "")
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id 是必填字段")

    # 检查是否已存在
    existing = _parse_tasks_from_todo()
    for t in existing:
        if t["id"] == task_id:
            raise HTTPException(status_code=409, detail=f"任务 {task_id} 已存在")

    # 追加到 TODO.md
    description = task.get("description", "")
    category = task.get("category", "debug")
    depends = task.get("depends", "")
    if isinstance(depends, list):
        depends = ", ".join(depends)

    entry = f"\n- [ ] 任务ID: {task_id}\n  描述: {description}\n  类别: {category}\n"
    if depends:
        entry += f"  依赖: {depends}\n"

    try:
        with open(TODO_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入 TODO.md 失败: {e}")

    return {
        "message": "任务已创建",
        "task_id": task_id,
        "status": "pending",
    }


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务

    从 TODO.md 中移除对应任务条目。
    """
    if not TODO_FILE.exists():
        raise HTTPException(status_code=404, detail="TODO.md 不存在")

    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    # 精确匹配任务块
    import re
    pattern = re.compile(
        rf"^- \[([ x])\] 任务ID:\s*{re.escape(task_id)}.*?(?=^- \[|$)",
        re.DOTALL,
    )
    new_content = pattern.sub("", content).strip()
    # 清理多余空行
    new_content = re.sub(r"\n{3,}", "\n\n", new_content)

    if new_content == content:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 未找到")

    try:
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            f.write(new_content + "\n")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")

    return {"message": f"任务 {task_id} 已删除"}


@app.post("/api/trigger")
async def trigger_evolution(background_tasks: BackgroundTasks):
    """手动触发一轮进化

    在后台运行 self_evolve_round.py（不阻塞 API 响应）。

    Why:
        - 使用 subprocess.Popen 而非 asyncio.create_subprocess_exec
          因为后者在 FastAPI 后台任务中可能被事件循环生命周期影响
        - Popen 是独立进程，即使 API 重启也不会中断进化
    """
    evolve_script = PROJECT_DIR / "src" / "core" / "self_evolve_round.py"
    if not evolve_script.exists():
        raise HTTPException(status_code=500, detail="src/core/self_evolve_round.py 不存在")

    def _run_evolve():
        try:
            result = subprocess.run(
                ["python3", str(evolve_script)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(PROJECT_DIR),
            )
            with open(LOGS_DIR / "api_trigger.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- Trigger at {datetime.datetime.now().isoformat()} ---\n")
                f.write(f"stdout: {result.stdout[:2000]}\n")
                if result.stderr:
                    f.write(f"stderr: {result.stderr[:1000]}\n")
        except subprocess.TimeoutExpired:
            with open(LOGS_DIR / "api_trigger.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- Trigger TIMEOUT at {datetime.datetime.now().isoformat()} ---\n")
        except Exception as e:
            with open(LOGS_DIR / "api_trigger.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- Trigger ERROR at {datetime.datetime.now().isoformat()}: {e} ---\n")

    background_tasks.add_task(_run_evolve)

    return {
        "message": "进化已触发",
        "script": "self_evolve_round.py",
        "triggered_at": datetime.datetime.now().isoformat(),
        "note": "进化在后台运行，稍后查看日志确认结果",
    }


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


@app.get("/api/status")
async def get_status():
    """返回完整状态报告"""
    state = _read_json(STATE_FILE)
    metrics = await get_metrics()

    github_status = "未配置"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode == 0:
            github_status = result.stdout.strip()
    except Exception:
        import logging
        logging.warning("Git status 检查失败")

    # 检查 Docker/Podman
    docker_available = False
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        docker_available = True
    except Exception:
        import logging
        logging.warning(f"Docker 检查失败")

    return {
        "service": "项目三：多Agent",
        "version": "1.0.0",
        "api_uptime_seconds": metrics["uptime_seconds"],
        "github_last_commit": github_status,
        "docker": "可用" if docker_available else "不可用",
        "state_step": state.get("step", "unknown"),
        "cronjob_paused": state.get("readonly_mode", False),
        "metrics": metrics,
    }


@app.get("/api/logs")
async def get_logs(lines: int = 50):
    """查看最近日志

    Args:
        lines: 返回行数（默认 50，最大 200）
    """
    lines = min(max(lines, 10), 200)
    log_file = LOGS_DIR / "self_evolve.log"

    content = _read_lines(log_file, lines)
    return {
        "log_file": str(log_file),
        "lines_returned": len(content),
        "content": "".join(content),
    }



# ── Bug 相关路由 ────────────────────────────────────────────────────────

BUGS_DIR = PROJECT_DIR / "bugs"
BUGS_DIR.mkdir(exist_ok=True)


def _bug_history_load() -> list:
    f = BUGS_DIR / "analysis_history.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _bug_history_save(data: list) -> None:
    try:
        (BUGS_DIR / "analysis_history.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        import logging
        logging.warning(f"Bug 历史保存失败: {BUGS_DIR / 'analysis_history.json'}")


def _bug_get(bug_id: str) -> dict:
    for item in _bug_history_load():
        if item.get("id") == bug_id:
            return item
    return None


def _bug_patch(bug_id: str, updates: dict) -> None:
    data = _bug_history_load()
    for item in data:
        if item.get("id") == bug_id:
            item.update(updates)
            break
    _bug_history_save(data)


def _scan_project(path: str) -> dict:
    import subprocess as sub
    proj = Path(path)
    findings = []
    py_files = list(proj.rglob("*.py"))[:50]
    for pf in py_files:
        try:
            r = sub.run(["python", "-m", "py_compile", str(pf)],
                        capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                findings.append({
                    "type": "syntax_error", "file": str(pf), "line": 0,
                    "detail": r.stderr[:200], "severity": "error",
                })
        except Exception:
            import logging
            logging.warning(f"py_compile 失败: {pf}")
    for pf in py_files[:30]:
        try:
            txt = pf.read_text(encoding="utf-8", errors="ignore")
            for i, ln in enumerate(txt.splitlines(), 1):
                if any(k in ln for k in ["TODO", "FIXME", "XXX"]):
                    findings.append({
                        "type": "todo_comment", "file": str(pf), "line": i,
                        "detail": ln.strip()[:100], "severity": "info",
                    })
                if re.search(r"except[^:]*:\s*pass", ln):
                    findings.append({
                        "type": "silent_exception", "file": str(pf), "line": i,
                        "detail": ln.strip()[:100], "severity": "warning",
                    })
        except Exception:
            import logging
            logging.warning(f"读取文件失败: {pf}")
    return {
        "project_path": path,
        "scan_time": datetime.datetime.now().isoformat(),
        "bug_count": len(findings),
        "py_files_scanned": len(py_files),
        "findings": findings[:100],
    }


@app.post("/api/bugs")
async def submit_bug(bug_data: dict):
    error_text = bug_data.get("error_text", "").strip()
    project_path = bug_data.get("project_path", "").strip()
    source_type = bug_data.get("source_type", "python")

    if not error_text and not project_path:
        raise HTTPException(status_code=400, detail="error_text 和 project_path 至少填写一个")

    if not error_text and project_path:
        if not Path(project_path).exists():
            raise HTTPException(status_code=400, detail=f"项目路径不存在: {project_path}")
        result = _scan_project(project_path)
        return {"bug_id": None, "status": "scanned", "scan_result": result,
                "message": f"已扫描项目，发现 {result['bug_count']} 个潜在问题"}

    if project_path and not Path(project_path).exists():
        raise HTTPException(status_code=400, detail=f"项目路径不存在: {project_path}")

    from src.analysis.bug_analysis_engine import analyze_bug
    result = analyze_bug(error_text, source_type)
    result["project_path"] = project_path
    result["status"] = "pending_fix" if (result.get("confidence", 0) >= 0.7 and project_path) else "analyzed"
    hist = _bug_history_load()
    for i, item in enumerate(hist):
        if item.get("id") == result["id"]:
            hist[i] = result
            break
    else:
        hist.append(result)
    _bug_history_save(hist)

    msg = "分析完成，可修复" if result["status"] == "pending_fix" else "分析完成，置信度不足，需人工处理"
    return {"bug_id": result["id"], "analysis": result, "can_fix": result["status"] == "pending_fix", "message": msg}


@app.get("/api/bugs")
async def list_bugs(limit: int = 30):
    return _bug_history_load()[-limit:]


@app.get("/api/bugs/{bug_id}")
async def get_bug(bug_id: str):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    return bug


@app.post("/api/bugs/{bug_id}/fix")
async def fix_bug(bug_id: str, background_tasks: BackgroundTasks):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    pp = bug.get("project_path", "")
    if not pp:
        raise HTTPException(status_code=400, detail="该 Bug 未关联项目路径，无法修复")

    _bug_patch(bug_id, {"status": "fixing", "fix_result": None})

    def _do():
        from src.analysis.bug_analysis_engine import execute_bug_fix
        res = execute_bug_fix(bug, pp)
        _bug_patch(bug_id, {
            "status": "fixed" if res["success"] else "failed",
            "fix_result": res,
        })

    background_tasks.add_task(_do)
    return {"bug_id": bug_id, "status": "fixing", "message": "修复已启动，请稍后刷新查看结果"}


@app.get("/api/bugs/{bug_id}/fix")
async def get_fix_result(bug_id: str):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    st = bug.get("status", "unknown")
    msgs = {
        "analyzed": "分析完成，等待修复",
        "pending_fix": "可修复，等待调用 /api/bugs/{id}/fix",
        "fixing": "修复中，请稍后刷新",
        "fixed": "修复完成",
        "failed": "修复失败，请查看 details",
        "scanned": "项目扫描完成",
    }
    return {
        "bug_id": bug_id, "status": st,
        "fix_result": bug.get("fix_result"),
        "project_path": bug.get("project_path", ""),
        "message": msgs.get(st, f"未知状态: {st}"),
    }


# ── 优化引擎路由 ────────────────────────────────────────────────────────

OPT_RUNS_DIR = PROJECT_DIR / "data" / "opt_runs"
OPT_RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _run_optimization_in_bg(target_dir: str, dimensions: list[str],
                            run_id: str, dry_run: bool) -> None:
    """后台执行优化扫描，结果写回 JSON 文件"""
    import sys as _sys
    import traceback as _tb
    # 确保 src/ 在 sys.path 中
    _SRC = PROJECT_DIR / "src"
    if str(_SRC) not in _sys.path:
        _sys.path.insert(0, str(_SRC))
    if str(PROJECT_DIR) not in _sys.path:
        _sys.path.insert(0, str(PROJECT_DIR))
    try:
        from src.analysis.optimizer_core import run_full_pipeline
        result = run_full_pipeline(target_dir, dimensions=dimensions)

        # 深度递归清理所有不可 JSON 序列化的对象
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

        # 补充路径信息
        result["target_dir"] = target_dir
        result["dry_run"] = dry_run
        result["run_id"] = run_id
        result["finished_at"] = datetime.datetime.now().isoformat()
        result["status"] = "completed"

        # 写结果文件
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
        # 清理运行中标记
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
    # 锁文件标记运行中
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


def _load_runs_from_opt_dir(limit: int, runs: list) -> None:
    """从 opt_runs 目录加载运行记录。"""
    for f in sorted(OPT_RUNS_DIR.glob("*.json"), reverse=True):
        if f.name.endswith(".running"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ent = data.get("deep_scan", {})
            runs.append({
                "run_id": data.get("run_id", f.stem),
                "target_dir": data.get("target_dir", ""),
                "type": data.get("type", "scan"),
                "status": data.get("status", "unknown"),
                "score": ent.get("score") if ent else (data.get("overall_score") or data.get("final_score")),
                "total_issues": ent.get("issue_count") if ent else (data.get("total_issues") or 0),
                "high": ent.get("by_severity", {}).get("high", 0) if ent else 0,
                "fixes": data.get("deep_fixes", {}).get("succeeded", 0) if data.get("deep_fixes") else 0,
                "started_at": data.get("started_at", ""),
                "finished_at": data.get("finished_at", ""),
                "error": data.get("error", None),
            })
        except (json.JSONDecodeError, KeyError):
            continue
        if len(runs) >= limit:
            break


def _load_runs_from_agent_logs(limit: int, runs: list) -> None:
    """从 agent_trigger 日志读取补充运行记录。"""
    log_dir = PROJECT_DIR / "logs"
    if not log_dir.exists():
        return
    for f in sorted(log_dir.glob("agent_trigger_*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            ent = d.get("deep_scan", {})
            if any(r.get("started_at","") == d.get("started_at","") for r in runs):
                continue
            runs.append({
                "run_id": f.stem,
                "target_dir": d.get("target_dir", ""),
                "type": "evolve",
                "status": d.get("status", "completed"),
                "score": ent.get("score") if ent else (d.get("score_before") or d.get("overall_score")),
                "total_issues": ent.get("issue_count") if ent else (d.get("total_issues") or 0),
                "high": ent.get("by_severity", {}).get("high", 0) if ent else 0,
                "fixes": d.get("deep_fixes", {}).get("succeeded", 0) if d.get("deep_fixes") else 0,
                "started_at": d.get("started_at", ""),
                "finished_at": d.get("finished_at", ""),
                "error": d.get("error", None),
            })
        except Exception:
            continue
        if len(runs) >= limit:
            break


def _load_running_runs(runs: list) -> None:
    """从 .running 文件加载正在进行的运行。"""
    for f in OPT_RUNS_DIR.glob("*.running"):
        run_id = f.stem
        result_file = OPT_RUNS_DIR / f"{run_id}.json"
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
                if data.get("status") == "running":
                    runs.insert(0, {
                        "run_id": run_id,
                        "target_dir": data.get("target_dir", ""),
                        "status": "running",
                        "started_at": data.get("started_at", ""),
                    })
            except Exception:
                pass


@app.get("/api/optimize/runs")
async def list_optimize_runs(limit: int = 20):
    """列出最近的优化运行记录"""
    runs = []
    _load_runs_from_opt_dir(limit, runs)
    _load_runs_from_agent_logs(limit, runs)
    runs.sort(key=lambda x: x.get("finished_at", x.get("started_at", "")), reverse=True)
    runs = runs[:limit]
    _load_running_runs(runs)
    return runs


@app.get("/api/optimize/runs/{run_id}")
async def get_optimize_run(run_id: str):
    """获取单次优化运行的详细结果"""
    result_file = OPT_RUNS_DIR / f"{run_id}.json"
    if not result_file.exists():
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在")
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
        # 检查是否仍在运行
        running_file = OPT_RUNS_DIR / f"{run_id}.running"
        if running_file.exists():
            data["status"] = "running"
        return data
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"结果文件损坏: {e}")


@app.get("/api/optimize/dimensions")
async def list_dimensions():
    """返回所有可用的优化维度"""
    from src.analysis.dims import DIMENSION_ORDER, DIMENSION_NAMES
    return {
        "dimensions": [
            {"id": d, "name": DIMENSION_NAMES.get(d, d)}
            for d in DIMENSION_ORDER
        ]
    }


# ── 持续优化循环 ─────────────────────────────────────────────────────

def _update_auto_progress(run_id: str, data: dict) -> None:
    """更新持续优化循环的进度文件"""
    progress_file = OPT_RUNS_DIR / f"{run_id}.progress"
    try:
        existing = {}
        if progress_file.exists():
            existing = json.loads(progress_file.read_text(encoding="utf-8"))
        existing.update(data)
        existing["updated_at"] = datetime.datetime.now().isoformat()
        progress_file.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _check_convergence(score_history: list) -> dict:
    """检查分数是否收敛（连续3轮变化<3分）。返回收敛信息或None。"""
    if len(score_history) < 3:
        return None
    recent = score_history[-3:]
    spread = max(recent) - min(recent)
    if spread <= 3:
        return {
            "converged": True,
            "final_score": score_history[-1],
            "total_rounds": len(score_history),
            "message": f"分数收敛于 {score_history[-1]}/100（连续3轮变化<3分），循环结束",
        }
    return {"converged": False}


def _get_score_delta(score_history: list) -> dict:
    """计算评分变化，用于反馈。"""
    if len(score_history) < 2:
        return {}
    delta = score_history[-1] - score_history[-2]
    if delta > 0:
        return {"score_delta": f"+{delta}",
                "message": f"评分上升 {delta} 分（{score_history[-2]}→{score_history[-1]}），继续监控..."}
    elif delta < 0:
        return {"score_delta": str(delta),
                "message": f"评分下降 {delta} 分（{score_history[-2]}→{score_history[-1]}），继续监控..."}
    return {}


def _extract_dimension_scores(scan_result: dict) -> dict:
    """从扫描结果提取各维度分数。"""
    dim_scores = {}
    for d_name, d_res in scan_result.get("dimensions", {}).items():
        dim_scores[d_name] = {
            "score": d_res.get("score", 0),
            "issues": d_res.get("issue_count", 0),
        }
    return dim_scores


def _json_safe(obj):
    if hasattr(obj, '__dict__'):
        return _json_safe(obj.__dict__)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(i) for i in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, (datetime.datetime,)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


def _scan_and_report_round(target_dir: str, dimensions: list[str],
                           run_id: str, round_num: int) -> dict:
    """执行一轮全维度扫描，返回结果字典。"""
    from src.analysis.optimizer_core import run_full_pipeline
    _update_auto_progress(run_id, {
        "round": round_num,
        "phase": "scanning",
        "message": f"第 {round_num} 轮：全维度扫描中...",
    })
    scan_result = run_full_pipeline(target_dir, dimensions=dimensions)
    scan_result = _json_safe(scan_result)
    overall_score = scan_result.get("overall_score", 0)
    critical = scan_result.get("critical_issues", 0)
    total = scan_result.get("total_issues", 0)
    dim_scores = _extract_dimension_scores(scan_result)
    _update_auto_progress(run_id, {
        "round": round_num,
        "phase": "scanned",
        "score": overall_score,
        "critical_remaining": critical,
        "total_issues": total,
        "dimension_scores": dim_scores,
        "message": f"评分 {overall_score}/100，发现 {total} 个问题（Critical {critical} 个）",
    })
    return {"score": overall_score, "critical": critical, "total": total,
            "dim_scores": dim_scores}


def _check_convergence(score_history: list, run_id: str,
                       overall_score: int, round_num: int) -> bool:
    """检查是否收敛（连续3轮变化<3分）。返回 True 表示应结束。"""
    if len(score_history) >= 3:
        recent = score_history[-3:]
        spread = max(recent) - min(recent)
        if spread <= 3:
            _update_auto_progress(run_id, {
                "phase": "converged",
                "message": f"分数收敛于 {overall_score}/100（连续3轮变化<3分），循环结束",
                "final_score": overall_score,
                "total_rounds": round_num,
                "score_history": score_history,
            })
            return True
    return False


def _report_score_trend(score_history: list, run_id: str) -> None:
    """根据分数变化趋势给出反馈。"""
    if len(score_history) < 2:
        return
    delta = score_history[-1] - score_history[-2]
    if delta > 0:
        _update_auto_progress(run_id, {
            "score_delta": f"+{delta}",
            "message": f"评分上升 {delta} 分（{score_history[-2]}→{score_history[-1]}），继续监控...",
        })
    elif delta < 0:
        _update_auto_progress(run_id, {
            "score_delta": str(delta),
            "message": f"评分下降 {delta} 分（{score_history[-2]}→{score_history[-1]}），继续监控...",
        })


def _finalize_auto_optimize(run_id: str, score_history: list,
                            round_num: int, target_dir: str,
                            dimensions: list[str]) -> None:
    """完成优化循环，写入最终状态和运行记录。"""
    final_score = score_history[-1] if score_history else 0
    final_state = {
        "status": "completed",
        "phase": "done",
        "final_score": final_score,
        "total_rounds": round_num,
        "score_history": score_history,
        "message": f"持续优化完成！共 {round_num} 轮，最终评分 {final_score}/100",
    }
    _update_auto_progress(run_id, final_state)
    _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
        "run_id": run_id,
        "target_dir": target_dir,
        "dimensions": dimensions,
        "type": "auto",
        "status": "completed",
        "total_rounds": round_num,
        "final_score": final_score,
        "score_history": score_history,
        "started_at": datetime.datetime.now().isoformat(),
    })


def _auto_optimize_loop(target_dir: str, dimensions: list[str], run_id: str) -> None:
    """持续优化循环：扫描 → 修复 → 重扫 → 再修复 → 直到分数稳定。

    3个阶段：
      Phase 1 — Bug修复：扫到 Critical/High 就修，修完重扫，直到无 Critical
      Phase 2 — 主动优化：选分数最低的维度优化
      Phase 3 — 收敛：分数连续3轮变化<3分 → 结束
    """
    import sys as _sys
    import time as _time

    _SRC = PROJECT_DIR / "src"
    for p in [str(_SRC), str(PROJECT_DIR)]:
        if p not in _sys.path:
            _sys.path.insert(0, p)

    MAX_ROUNDS = 15
    score_history = []

    _update_auto_progress(run_id, {
        "status": "running",
        "phase": "initializing",
        "target_dir": target_dir,
        "round": 0,
        "score_history": [],
        "message": "启动持续优化循环...",
    })

    try:
        for round_num in range(1, MAX_ROUNDS + 1):
            result = _scan_and_report_round(target_dir, dimensions, run_id, round_num)
            score_history.append(result["score"])

            if _check_convergence(score_history, run_id, result["score"], round_num):
                break

            _report_score_trend(score_history, run_id)
            _time.sleep(1)

        _finalize_auto_optimize(run_id, score_history, round_num, target_dir, dimensions)

    except Exception as e:
        _update_auto_progress(run_id, {
            "status": "failed",
            "phase": "error",
            "error": str(e),
            "traceback": _tb.format_exc(),
            "message": f"循环异常终止: {e}",
        })
        _write_json(OPT_RUNS_DIR / f"{run_id}.json", {
            "run_id": run_id,
            "target_dir": target_dir,
            "type": "auto",
            "status": "failed",
            "error": str(e),
            "total_rounds": round_num,
        })
    finally:
        run_lock = OPT_RUNS_DIR / f"{run_id}.running"
        if run_lock.exists():
            run_lock.unlink()


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
        # 回退到 run 文件
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
        # 检查是否还在运行
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


# ── 多Agent自进化入口 ────────────────────────────────────────────────


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

    # 统一为 WSL 路径（先转换，再检查存在性）
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

    # 写入目标路径文件
    target_file = PROJECT_DIR / "data" / "opt_target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(wsl_path + "\n", encoding="utf-8")

    # 恢复 cronjob（通过子进程调用 hermes CLI）
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

    # 标记持续运行
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


def _build_dim_summary_from_log(d: dict) -> dict:
    """从单个日志条目构建维度摘要。"""
    dims = d.get("dimensions", {})
    dim_summary = {}
    if dims:
        for dn, dr in list(dims.items())[:9]:
            dim_summary[dn] = {
                "score": dr.get("score", 0),
                "issues": dr.get("issue_count", 0),
                "label": dr.get("label", dn),
            }
    deep = d.get("deep_scan", {})
    if isinstance(deep, dict) and "score" in deep:
        dim_summary["_enterprise"] = {
            "score": deep.get("score", 0),
            "issues": deep.get("issue_count", 0),
            "label": "企业级深度",
            "by_severity": deep.get("by_severity", {}),
        }
    deep_fixes = d.get("deep_fixes", {})
    if deep_fixes and deep_fixes.get("succeeded", 0) > 0:
        dim_summary["_enterprise"]["deep_fixes"] = {
            "succeeded": deep_fixes.get("succeeded", 0),
            "failed": deep_fixes.get("failed", 0),
            "details": deep_fixes.get("details", [])[:30],
        }
    return dim_summary


def _parse_log_file(f: Path) -> dict:
    """解析单个日志文件为运行记录条目。"""
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        dim_summary = _build_dim_summary_from_log(d)
        return {
            "file": f.name,
            "time": d.get("finished_at", d.get("started_at", "")),
            "score_before": d.get("score_before"),
            "score_after": d.get("score_after"),
            "fixes_succeeded": d.get("fixes", {}).get("succeeded", 0),
            "total_issues": d.get("total_issues", 0),
            "critical_issues": d.get("critical_issues", 0),
            "dimensions": dim_summary,
        }
    except Exception:
        return None


def _load_recent_logs(log_dir: Path, max_count: int = 3) -> list:
    """从日志目录加载最近的运行记录。"""
    recent = []
    if not log_dir.exists():
        return recent
    for f in sorted(log_dir.glob("agent_trigger_*.json"), reverse=True)[:max_count]:
        entry = _parse_log_file(f)
        if entry:
            recent.append(entry)
    return recent


def _check_running_status() -> dict:
    """检查是否有正在运行的自进化任务。"""
    run_marker = PROJECT_DIR / "data" / ".current_run.json"
    if not run_marker.exists():
        return None
    try:
        marker = json.loads(run_marker.read_text(encoding="utf-8"))
        if marker.get("status") in ("running", "continuous"):
            return marker
    except Exception:
        pass
    return None


@app.get("/api/optimize/agent-status")
async def get_agent_status():
    """获取多Agent自进化运行状态"""
    target_file = PROJECT_DIR / "data" / "opt_target.txt"
    target = target_file.read_text(encoding="utf-8").strip() if target_file.exists() else None

    log_dir = PROJECT_DIR / "logs"
    recent = _load_recent_logs(log_dir)

    enterprise = {}
    if recent and recent[0].get("dimensions", {}).get("_enterprise"):
        ent = recent[0]["dimensions"]["_enterprise"]
        enterprise = {
            "score": ent.get("score", 0),
            "issues": ent.get("issues", 0),
            "by_severity": ent.get("by_severity", {}),
            "fixes": ent.get("deep_fixes", {}),
        }

    running = _check_running_status()

    return {
        "target": target,
        "enterprise": enterprise,
        "recent_runs": recent,
        "currently_running": running,
        "cronjob_schedule": "每30分钟",
    }


# ── 停止自进化 ────────────────────────────────────────────────────


@app.post("/api/optimize/stop-agent")
async def stop_agent():
    """停止多Agent自进化：直接暂停 cronjob"""
    # 直接修改 cron jobs JSON 配置文件
    cron_file = Path.home() / ".hermes" / "cron" / "jobs.json"
    if cron_file.exists():
        try:
            cron_data = json.loads(cron_file.read_text(encoding="utf-8"))
            for job in cron_data.get("jobs", []):
                if job.get("job_id") == "79cb9d06dc5d":
                    job["enabled"] = False
                    job["state"] = "paused"
                    job["paused_at"] = datetime.datetime.now().isoformat()
                    break
            cron_file.write_text(json.dumps(cron_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            pass

    # 写停止标记
    run_marker = PROJECT_DIR / "data" / ".current_run.json"
    run_marker.write_text(json.dumps({
        "status": "stopped",
        "phase": "paused",
        "stopped_at": datetime.datetime.now().isoformat(),
    }), encoding="utf-8")

    return {
        "status": "stopped",
        "message": "多Agent自进化已停止，cronjob 已暂停",
    }


# ── 前端 ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """返回 Web 仪表盘页面"""
    return _get_dashboard_html()


def _get_dashboard_html() -> HTMLResponse:
    """从 api/dashboard.html 文件读取仪表盘"""
    df = PROJECT_DIR / "api" / "dashboard.html"
    if df.exists():
        return HTMLResponse(content=df.read_text(encoding="utf-8"))
    fb = ("<html><body style='background:#0d1117;color:#c9d1d9;padding:40px;font-family:sans-serif'>"
          "<h1>仪表盘文件未找到</h1><p>请检查 api/dashboard.html 是否存在</p></body></html>")
    return HTMLResponse(content=fb)



# ── 入口 ────────────────────────────────────────────────────────────────

def api_entrypoint():
    """启动 FastAPI 服务

    供 docker-entrypoint.sh 调用，或直接 python src/api/api_service.py 启动。

    Why:
        用函数封装而非 __main__ 块，方便容器入口脚本调用。
    """
    import uvicorn
    uvicorn.run(
        "api_service:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    api_entrypoint()

