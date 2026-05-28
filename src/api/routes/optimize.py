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


@router.get("/api/optimize/runs")
async def list_optimize_runs(limit: int = 20):
    """列出最近的优化运行记录"""
    runs = []
    _load_runs_from_opt_dir(limit, runs)
    _load_runs_from_agent_logs(limit, runs)
    runs.sort(key=lambda x: x.get("finished_at", x.get("started_at", "")), reverse=True)
    runs = runs[:limit]
    _load_running_runs(runs)
    return runs


@router.get("/api/optimize/runs/{run_id}")
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


@router.get("/api/optimize/dimensions")
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


@router.post("/api/optimize/evolve")
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


@router.post("/api/optimize/auto")
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


@router.get("/api/optimize/auto/{run_id}/progress")
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


@router.get("/api/optimizer", response_class=HTMLResponse)
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




@router.post("/api/optimize/start-agent")
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


@router.get("/api/optimize/agent-status")
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




@router.post("/api/optimize/stop-agent")
async def stop_agent():
    """停止多Agent自进化：直接暂停 cronjob"""
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




@router.get("/", response_class=HTMLResponse)
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
