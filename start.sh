#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# start.sh — 项目三：多Agent 一键启动脚本
# 用法：
#   ./start.sh start      # 构建并启动所有服务
#   ./start.sh stop       # 停止所有服务
#   ./start.sh restart    # 重启所有服务
#   ./start.sh logs       # 查看日志（Ctrl+C 退出）
#   ./start.sh status     # 查看服务状态
#   ./start.sh api        # 仅启动 API + Web 仪表盘（不含 swarm）
#   ./start.sh build      # 重新构建镜像
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.yml"
API_ONLY_PROFILE="--profile api-only"

# ─── 颜色 ──────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ─── 检查 Docker ────────────────────────────────────────────────────────
check_docker() {
    if ! command -v docker &>/dev/null; then
        log_error "Docker 未安装。请先安装 Docker：https://docs.docker.com/get-docker/"
        exit 1
    fi
    if ! docker compose version &>/dev/null; then
        log_error "Docker Compose V2 未安装。请安装 docker-compose-plugin。"
        exit 1
    fi
}

# ─── 服务状态 ────────────────────────────────────────────────────────────
cmd_status() {
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  项目三：多Agent — 服务状态"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    docker compose -f "$COMPOSE_FILE" ps 2>/dev/null || log_warn "服务未启动"
    echo ""
    echo "可用命令:"
    echo "  ./start.sh start     - 启动全部服务"
    echo "  ./start.sh api       - 仅启动 API + Web"
    echo "  ./start.sh stop      - 停止全部服务"
    echo "  ./start.sh logs      - 查看日志"
    echo "  ./start.sh build     - 重新构建"
    echo "  ./start.sh status    - 查看此状态"
}

# ─── 启动全部 ────────────────────────────────────────────────────────────
cmd_start() {
    check_docker
    log_info "构建并启动全部服务..."
    
    if [ ! -f .env ]; then
        log_warn ".env 文件不存在，从 .env.example 复制"
        cp .env.example .env 2>/dev/null || touch .env
        log_warn "请编辑 .env 填入 API Key 等配置"
    fi

    docker compose -f "$COMPOSE_FILE" up -d --build
    echo ""
    log_info "✅ 服务已启动！"
    echo "  - Swarm 引擎:  http://localhost:9090/health"
    echo "  - API 服务:    http://localhost:8000/docs"
    echo "  - Web 仪表盘:  http://localhost:8000"
    echo "  - Metrics:     http://localhost:9090/metrics"
    echo ""
    log_info "查看日志: ./start.sh logs"
    log_info "停止服务: ./start.sh stop"
}

# ─── 仅启动 API + Web ────────────────────────────────────────────────────
cmd_api() {
    check_docker
    log_info "启动 API + Web 仪表盘（不含 swarm 主引擎）..."

    if [ ! -f .env ]; then
        cp .env.example .env 2>/dev/null || touch .env
    fi

    docker compose -f "$COMPOSE_FILE" up -d --build api-service
    echo ""
    log_info "✅ API 服务已启动！"
    echo "  - API 文档:    http://localhost:8000/docs"
    echo "  - Web 仪表盘:  http://localhost:8000"
}

# ─── 停止 ────────────────────────────────────────────────────────────────
cmd_stop() {
    check_docker
    log_info "停止所有服务..."
    docker compose -f "$COMPOSE_FILE" down
    log_info "✅ 服务已停止"
}

# ─── 重启 ────────────────────────────────────────────────────────────────
cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

# ─── 日志 ────────────────────────────────────────────────────────────────
cmd_logs() {
    check_docker
    docker compose -f "$COMPOSE_FILE" logs -f
}

# ─── 构建 ────────────────────────────────────────────────────────────────
cmd_build() {
    check_docker
    log_info "重新构建镜像..."
    docker compose -f "$COMPOSE_FILE" build
    log_info "✅ 构建完成"
}

# ─── 主入口 ──────────────────────────────────────────────────────────────
case "${1:-help}" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    restart)  cmd_restart ;;
    logs)     cmd_logs ;;
    status)   cmd_status ;;
    api)      cmd_api ;;
    build)    cmd_build ;;
    help|--help|-h)
        echo "项目三：多Agent — 一键启动脚本"
        echo ""
        echo "用法: ./start.sh <命令>"
        echo ""
        echo "命令:"
        echo "  start     构建并启动全部服务（默认）"
        echo "  stop      停止全部服务"
        echo "  restart   重启全部服务"
        echo "  logs      查看容器日志"
        echo "  status    查看服务状态"
        echo "  api       仅启动 API + Web 仪表盘"
        echo "  build     重新构建镜像"
        echo "  help      显示此帮助"
        ;;
    *)
        log_error "未知命令: $1"
        echo "可用命令: start, stop, restart, logs, status, api, build, help"
        exit 1
        ;;
esac
