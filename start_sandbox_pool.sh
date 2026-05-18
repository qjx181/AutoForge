#!/bin/bash
# sandbox_pool.sh — 预热 Docker 容器池
# 用途：启动常驻 sandbox 容器，避免每次 docker run 开销
# 用法：./start_sandbox_pool.sh [start|stop|restart|status]
# 协调者：在首次 cronjob 运行前手动执行一次 start 即可

set -e
POOL_NAME="sandbox-pool"
IMAGE="python:3.11-slim"
MEMORY="128m"
CPUS="0.5"
PIDS_LIMIT="50"

case "${1:-status}" in
  start)
    # 检查 Docker 是否可用
    if ! docker info >/dev/null 2>&1; then
      echo "❌ Docker 不可用，跳过预热容器"
      exit 1
    fi
    # 检查是否已运行
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${POOL_NAME}$"; then
      echo "✅ 预热容器 ${POOL_NAME} 已在运行"
      exit 0
    fi
    # 清理旧的停止容器
    docker rm -f "${POOL_NAME}" 2>/dev/null || true
    # 启动预热容器
    echo "🚀 启动预热容器 ${POOL_NAME}..."
    docker run -d \
      --name "${POOL_NAME}" \
      --rm \
      --read-only \
      --cpus "${CPUS}" \
      --memory "${MEMORY}" \
      --pids-limit "${PIDS_LIMIT}" \
      "${IMAGE}" \
      tail -f /dev/null
    echo "✅ 预热容器 ${POOL_NAME} 已启动 (cpus=${CPUS}, mem=${MEMORY}, pids=${PIDS_LIMIT})"
    ;;
  stop)
    echo "🛑 停止预热容器 ${POOL_NAME}..."
    docker rm -f "${POOL_NAME}" 2>/dev/null || echo "  容器不存在"
    echo "✅ 已停止"
    ;;
  restart)
    $0 stop
    $0 start
    ;;
  status)
    if docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -q "^${POOL_NAME}"; then
      docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Memory}}' --filter "name=${POOL_NAME}"
      echo "✅ 预热容器运行中"
    else
      echo "⏹️  预热容器未运行"
      echo "   运行 ./start_sandbox_pool.sh start 启动"
    fi
    ;;
  exec)
    # sandbox_exec.sh 的快速通道：直接在预热容器中执行代码
    shift
    FILE="$1"
    if [ -z "$FILE" ]; then
      echo "❌ 用法: $0 exec <文件路径>"
      exit 1
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${POOL_NAME}$"; then
      echo "❌ 预热容器未运行，运行 $0 start 启动"
      exit 1
    fi
    BASENAME=$(basename "$FILE")
    docker cp "$FILE" "${POOL_NAME}:/tmp/${BASENAME}"
    docker exec "${POOL_NAME}" python3 -c "
import sys
sys.path.insert(0, '/tmp')
try:
    exec(open('/tmp/${BASENAME}').read())
    print('✅ sandbox exec OK')
except Exception as e:
    print(f'❌ SANDBOX EXCEPTION: {type(e).__name__}: {e}')
    sys.exit(1)
"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|exec <file>}"
    exit 1
    ;;
esac
