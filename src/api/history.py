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
