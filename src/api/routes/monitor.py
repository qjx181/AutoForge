"""API routes — monitor"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json, os, datetime
from src.api.backend.core import _scan_project

router = APIRouter()

@router.get("/api/metrics")
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


@router.get("/api/status")
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


@router.get("/api/logs")
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
