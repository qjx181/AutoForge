#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# docker-entrypoint.sh — 项目三：多Agent 容器初始化入口
# ═══════════════════════════════════════════════════════════════════════
# 职责：
#   1. 生成 .env（如果不存在）
#   2. 初始化 git 配置
#   3. 创建运行时目录
#   4. 根据 CMD 启动对应服务
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

APP_DIR="/app"
cd "$APP_DIR"

# ─── 1. 初始化环境变量 ─────────────────────────────────────────────────
if [ ! -f .env ] && [ -f .env.example ]; then
    echo "[entrypoint] .env 不存在，从 .env.example 生成"
    cp .env.example .env
    echo "[entrypoint] ⚠️  请编辑 .env 填入真实密钥值"
fi

# ─── 2. 创建运行时目录 ─────────────────────────────────────────────────
mkdir -p logs heartbeats tmp_agent metrics

# ─── 3. 初始化 Git 用户 ────────────────────────────────────────────────
git config --global user.name  "${GIT_USER_NAME:-swarm-agent}"
git config --global user.email "${GIT_USER_EMAIL:-swarm@agent.local}"
git config --global safe.directory "$APP_DIR"

# ─── 4. 启动前自检 ─────────────────────────────────────────────────────
echo "[entrypoint] 运行启动自检..."
python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from secrets import self_check
    warnings = self_check()
    for w in warnings:
        print(f'  ⚠ {w}')
    if not warnings:
        print('  ✅ 全部通过')
except ImportError:
    print('  ⚠ secrets.py 未加载（开发模式）')
"

# ─── 5. 根据 CMD 启动 ─────────────────────────────────────────────────
MODE="${1:-swarm}"

case "$MODE" in
    swarm)
        echo "[entrypoint] 启动 Swarm 自进化引擎 (每 ${SWARM_ROUND_INTERVAL:-30} 分钟)"
        echo "[entrypoint] + Prometheus /metrics @ :${PROMETHEUS_PORT:-9090}"
        echo "[entrypoint] + 审计日志 → logs/audit.jsonl"
        # 启动 metrics HTTP 服务 + 主循环
        exec python3 -c "
import sys, time, threading
sys.path.insert(0, '.')
from prometheus_metrics import start_metrics_server, record_round_completed
from audit_trail import audit_log

# 启动 metrics HTTP 端点
port = int(os.environ.get('PROMETHEUS_PORT', 9090))
start_metrics_server(port=port)
print(f'[metrics] 端点已启动 @ :{port}/metrics')

# 主循环（调用 self_evolve_round）
import subprocess
while True:
    round_interval = int(os.environ.get('SWARM_ROUND_INTERVAL', 30))
    try:
        result = subprocess.run(
            ['python3', 'self_evolve_round.py'],
            capture_output=True, text=True, timeout=600
        )
        audit_log('swarm_round', 'self_evolve_round.py',
                  f'rc={result.returncode}', source='entrypoint')
        record_round_completed(result.returncode == 0)
    except Exception as e:
        audit_log('swarm_error', 'self_evolve_round.py', str(e)[:100],
                  success=False, source='entrypoint')
        record_round_completed(False)
    time.sleep(round_interval * 60)
"
        ;;
    metrics)
        echo "[entrypoint] 启动 Metrics Exporter (只读模式 @ :${PROMETHEUS_PORT:-9091})"
        exec python3 -c "
import os, sys
sys.path.insert(0, '.')
from prometheus_metrics import start_metrics_server
port = int(os.environ.get('PROMETHEUS_PORT', 9091))
start_metrics_server(port=port, readonly=True)
print(f'[metrics] 只读端点 @ :{port}/metrics')
while True: time.sleep(3600)
"
        ;;
    test)
        echo "[entrypoint] 运行测试套件..."
        exec python3 -m pytest tests/ -v --tb=short
        ;;
    shell)
        echo "[entrypoint] 进入交互式 Shell..."
        exec /bin/bash
        ;;
    api)
        echo "[entrypoint] 启动 API 服务 + Web 仪表盘 @ :${API_PORT:-8000}"
        echo "[entrypoint] Swagger docs → http://localhost:${API_PORT:-8000}/docs"
        exec python3 -c "
import os, sys
sys.path.insert(0, '.')
os.environ['API_PORT'] = os.environ.get('API_PORT', '8000')
from api_service import api_entrypoint
api_entrypoint()
"
        ;;
    *)

        echo "[entrypoint] 未知命令: $MODE"
        echo "可用命令: swarm (默认) | metrics | api | test | shell"
        exit 1
        ;;
esac
