import logging
from pathlib import Path
from src.api.backend.core import _write_json
from src.api.backend.core import OPT_RUNS_DIR

logger = logging.getLogger(__name__)

"""API routes — optimize"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json, os, datetime
from src.api.backend.core import _auto_optimize_loop
from src.api.backend.core import _build_dim_summary_from_log
from src.api.backend.core import _check_convergence
from src.api.backend.core import _extract_dimension_scores
from src.api.backend.core import _finalize_auto_optimize
from src.api.backend.core import _get_score_delta
from src.api.backend.core import _json_safe
from src.api.backend.core import _load_recent_logs
from src.api.backend.core import _load_running_runs
from src.api.backend.core import _load_runs_from_agent_logs
from src.api.backend.core import _load_runs_from_opt_dir
from src.api.backend.core import _parse_log_file
from src.api.backend.core import _report_score_trend
from src.api.backend.core import _run_evolution_task
from src.api.backend.core import _scan_and_report_round
from src.api.backend.core import _update_auto_progress

router = APIRouter()

@router.post("/api/optimize")
async def start_optimization(body: dict) -> Any:
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
                logger.exception("读取运行状态文件失败: %s", result_file)


@router.get("/api/optimize/runs")
async def list_optimize_runs(limit: int = 20) -> Any:
    """获取优化运行列表

    Returns:
        list[dict]: 运行记录列表，每条包含 run_id, target_dir, status, score 等字段
    """
    runs: list = []
    _load_runs_from_opt_dir(limit, runs)
    _load_runs_from_agent_logs(limit, runs)
    _load_running_runs(runs)
    return runs[:limit]