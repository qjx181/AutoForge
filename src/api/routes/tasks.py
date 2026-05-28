"""API routes — tasks"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json, os, datetime


router = APIRouter()

@router.get("/api/tasks")
async def list_tasks():
    """列出所有任务

    Returns:
        list[dict]: 任务列表（来自 TODO.md + state.json）
    """
    return _parse_tasks_from_todo()


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """查看单个任务详情"""
    tasks = _parse_tasks_from_todo()
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise HTTPException(status_code=404, detail=f"任务 {task_id} 未找到")


@router.post("/api/tasks")
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


@router.delete("/api/tasks/{task_id}")
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
