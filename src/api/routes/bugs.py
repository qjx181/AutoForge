"""API routes — bugs"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json, os, datetime
from src.api.backend.core import _run_optimization_in_bg

router = APIRouter()

@router.post("/api/bugs")
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


@router.get("/api/bugs")
async def list_bugs(limit: int = 30):
    return _bug_history_load()[-limit:]


@router.get("/api/bugs/{bug_id}")
async def get_bug(bug_id: str):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    return bug


@router.post("/api/bugs/{bug_id}/fix")
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


@router.get("/api/bugs/{bug_id}/fix")
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



OPT_RUNS_DIR = PROJECT_DIR / "data" / "opt_runs"
OPT_RUNS_DIR.mkdir(parents=True, exist_ok=True)


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
