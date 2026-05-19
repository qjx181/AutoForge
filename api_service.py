#!/usr/bin/env python3
"""api_service.py — FastAPI 服务 + Web 仪表盘

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

import asyncio
import json
import os
import subprocess
import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse


# ── 路径 ────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.resolve()
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
            done = task_match.group(1) == "x"
            current_task = {
                "id": task_match.group(2),
                "status": "completed" if done else "pending",
                "description": "",
                "category": "debug",
                "depends": [],
            }
        elif current_task and "描述:" in line:
            current_task["description"] = line.split("描述:", 1)[1].strip()
        elif current_task and "依赖:" in line:
            dep_text = line.split("依赖:", 1)[1].strip()
            if dep_text and dep_text != "无":
                current_task["depends"] = [d.strip() for d in dep_text.split(",")]
        elif current_task and "类别:" in line:
            current_task["category"] = line.split("类别:", 1)[1].strip()

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
    evolve_script = PROJECT_DIR / "self_evolve_round.py"
    if not evolve_script.exists():
        raise HTTPException(status_code=500, detail="self_evolve_round.py 不存在")

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
        pass

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
        pass

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


# ── 前端 ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """返回 Web 仪表盘页面"""
    from fastapi.responses import HTMLResponse
    return _get_dashboard_html()


def _get_dashboard_html() -> str:
    """返回内嵌仪表盘 HTML"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>项目三：多Agent 仪表盘</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --radius: 8px;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 20px;
    line-height: 1.5;
  }
  .container { max-width: 1200px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 8px; color: #f0f6fc; }
  .subtitle { color: var(--text-dim); margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
  }
  .card .label { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .card .value { font-size: 28px; font-weight: 600; }
  .card .value.green { color: var(--green); }
  .card .value.yellow { color: var(--yellow); }
  .card .value.red { color: var(--red); }
  .status-bar { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
  .status-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 20px; padding: 4px 14px;
    font-size: 13px;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; }
  .status-dot.green { background: var(--green); }
  .status-dot.yellow { background: var(--yellow); }
  .status-dot.red { background: var(--red); }

  table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; }
  th { color: var(--text-dim); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .task-pending { color: var(--yellow); }
  .task-completed { color: var(--green); }

  button, .btn {
    background: #21262d; color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 16px; font-size: 14px; cursor: pointer;
    transition: background 0.2s;
  }
  button:hover { background: #30363d; }
  button.primary { background: #238636; border-color: #2ea043; color: #fff; }
  button.primary:hover { background: #2ea043; }
  button.danger { background: #da3633; border-color: #f85149; color: #fff; }
  button.danger:hover { background: #f85149; }

  form { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; align-items: end; }
  form label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 4px; }
  form input { background: #0d1117; border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; color: var(--text); font-size: 14px; width: 100%; }
  .form-group { flex: 1; min-width: 180px; }
  .form-actions { display: flex; gap: 8px; }

  .section { margin-bottom: 32px; }
  .section h2 { font-size: 18px; margin-bottom: 12px; color: #f0f6fc; border-bottom: 1px solid var(--border); padding-bottom: 8px; }

  .toast {
    position: fixed; bottom: 20px; right: 20px;
    background: var(--card); border: 1px solid var(--green);
    border-radius: var(--radius); padding: 12px 20px;
    font-size: 14px; display: none;
    animation: slideIn 0.3s ease;
  }
  .toast.error { border-color: var(--red); }
  @keyframes slideIn { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

  .loading { opacity: 0.5; pointer-events: none; }
  @media (max-width: 768px) { form { flex-direction: column; } .form-group { min-width: 100%; } }
</style>
</head>
<body>
<div class="container">
  <h1>⚡ 项目三：多Agent 仪表盘</h1>
  <div class="subtitle">Swarm 自进化引擎 — 实时控制面板</div>

  <div class="status-bar" id="statusBar">
    <span class="status-badge"><span class="status-dot green" id="healthDot"></span><span id="healthText">正在连接...</span></span>
    <span class="status-badge">🔄 轮次 <span id="currentRound">-</span></span>
    <span class="status-badge">📊 成功率 <span id="successRate">-</span>%</span>
    <span class="status-badge">💰 今日 $<span id="dollarSpent">-</span> / $<span id="dollarLimit">-</span></span>
  </div>

  <div class="grid" id="metricsGrid">
    <div class="card"><div class="label">完成任务</div><div class="value green" id="completedTasks">-</div></div>
    <div class="card"><div class="label">待处理任务</div><div class="value yellow" id="pendingTasks">-</div></div>
    <div class="card"><div class="label">总轮次数</div><div class="value" id="totalRounds">-</div></div>
    <div class="card"><div class="label">运行时长</div><div class="value" id="uptime">-</div></div>
  </div>

  <div class="section">
    <h2>📋 任务列表</h2>
    <div style="overflow-x:auto"><table id="taskTable">
      <thead><tr><th>ID</th><th>描述</th><th>类别</th><th>状态</th><th></th></tr></thead>
      <tbody id="taskBody"></tbody>
    </table></div>
  </div>

  <div class="section">
    <h2>➕ 提交新任务</h2>
    <form id="taskForm">
      <div class="form-group">
        <label>任务ID</label>
        <input type="text" id="newTaskId" placeholder="例如: add_login_api" required>
      </div>
      <div class="form-group">
        <label>描述</label>
        <input type="text" id="newDesc" placeholder="实现用户登录 API">
      </div>
      <div class="form-group">
        <label>类别</label>
        <input type="text" id="newCategory" placeholder="feature / test / debug" value="feature">
      </div>
      <div class="form-group">
        <label>依赖 (逗号分隔)</label>
        <input type="text" id="newDeps" placeholder="例如: add_auth">
      </div>
      <div class="form-actions">
        <button type="submit" class="primary">提交任务</button>
      </div>
    </form>
  </div>

  <div class="section">
    <h2>🎮 控制</h2>
    <button class="primary" id="triggerBtn">▶ 触发一轮进化</button>
    <button id="refreshBtn">🔄 刷新数据</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = '';
let loading = false;

function showToast(msg, isError) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (isError ? ' error' : '');
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

async function loadMetrics() {
  try {
    const m = await fetchJSON('/api/metrics');
    document.getElementById('currentRound').textContent = m.current_round || '-';
    document.getElementById('completedTasks').textContent = m.completed_tasks ?? '-';
    document.getElementById('pendingTasks').textContent = m.pending_tasks ?? '-';
    document.getElementById('totalRounds').textContent = m.rounds_total ?? '-';
    document.getElementById('successRate').textContent = m.success_rate ?? '-';
    document.getElementById('dollarSpent').textContent = (m.dollar_spent_today ?? 0).toFixed(2);
    document.getElementById('dollarLimit').textContent = (m.dollar_limit ?? 5).toFixed(2);
    const uptime = m.uptime_seconds ?? 0;
    const h = Math.floor(uptime / 3600), min = Math.floor((uptime % 3600) / 60);
    document.getElementById('uptime').textContent = h + 'h ' + min + 'm';
    document.getElementById('healthText').textContent = '运行中';
    document.getElementById('healthDot').className = 'status-dot green';
  } catch(e) {
    document.getElementById('healthText').textContent = '连接失败';
    document.getElementById('healthDot').className = 'status-dot red';
  }
}

async function loadTasks() {
  try {
    const tasks = await fetchJSON('/api/tasks');
    const tbody = document.getElementById('taskBody');
    if (tasks.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-dim);text-align:center">暂无任务</td></tr>';
      return;
    }
    tbody.innerHTML = tasks.map(t => {
      const statusClass = t.status === 'completed' ? 'task-completed' : 'task-pending';
      return `<tr>
        <td><code>${t.id || '-'}</code></td>
        <td>${(t.description || '-').substring(0, 60)}</td>
        <td>${t.category || '-'}</td>
        <td class="${statusClass}">${t.status === 'completed' ? '✅ 已完成' : '⏳ 待处理'}</td>
        <td><button class="danger" onclick="deleteTask('${t.id}')">删除</button></td>
      </tr>`;
    }).join('');
  } catch(e) {
    console.error('load tasks error:', e);
  }
}

async function deleteTask(id) {
  if (!confirm('确定删除任务 "' + id + '" 吗？')) return;
  try {
    const r = await fetch('/api/tasks/' + id, { method: 'DELETE' });
    if (!r.ok) throw new Error((await r.json()).detail || '删除失败');
    showToast('✅ 任务 ' + id + ' 已删除');
    loadTasks();
  } catch(e) {
    showToast('❌ ' + e.message, true);
  }
}

async function refreshAll() {
  if (loading) return;
  loading = true;
  document.body.classList.add('loading');
  await Promise.all([loadMetrics(), loadTasks()]);
  document.body.classList.remove('loading');
  loading = false;
}

document.getElementById('taskForm').onsubmit = async function(e) {
  e.preventDefault();
  const body = {
    task_id: document.getElementById('newTaskId').value.trim(),
    description: document.getElementById('newDesc').value.trim(),
    category: document.getElementById('newCategory').value.trim() || 'debug',
    depends: document.getElementById('newDeps').value.trim() || '',
  };
  if (!body.task_id) { showToast('❌ 任务ID 不能为空', true); return; }
  try {
    const r = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json()).detail || '提交失败');
    showToast('✅ 任务 ' + body.task_id + ' 已创建');
    this.reset();
    loadTasks();
  } catch(e) {
    showToast('❌ ' + e.message, true);
  }
};

document.getElementById('triggerBtn').onclick = async function() {
  this.disabled = true;
  this.textContent = '⏳ 正在触发...';
  try {
    const r = await fetch('/api/trigger', { method: 'POST' });
    if (!r.ok) throw new Error((await r.json()).detail || '触发失败');
    const data = await r.json();
    showToast('✅ ' + data.message);
  } catch(e) {
    showToast('❌ ' + e.message, true);
  }
  this.disabled = false;
  this.textContent = '▶ 触发一轮进化';
};

document.getElementById('refreshBtn').onclick = refreshAll;

// 初次加载 + 自动刷新
refreshAll();
setInterval(refreshAll, 30000);
</script>
</body>
</html>"""


# ── 入口 ────────────────────────────────────────────────────────────────

def api_entrypoint():
    """启动 FastAPI 服务

    供 docker-entrypoint.sh 调用，或直接 python api_service.py 启动。

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
