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
