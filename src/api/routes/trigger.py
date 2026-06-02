import os, sys, datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = PROJECT_DIR / "logs"

"""API routes — trigger"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json, os, datetime


router = APIRouter()

@router.post("/api/trigger")
async def trigger_evolution(background_tasks: BackgroundTasks) -> Any:
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
