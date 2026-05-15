#!/bin/bash
# start_hermes_daemon.sh — 启动 Hermes Gateway 后台服务（在 tmux 中常驻）
# 用法: bash /mnt/f/项目三：多Agent/start_hermes_daemon.sh

SESSION_NAME="hermes-swarm"

# 如果会话已存在则跳过
tmux has-session -t "$SESSION_NAME" 2>/dev/null && {
    echo "Hermes 守护进程已在运行 (tmux 会话: $SESSION_NAME)"
    exit 0
}

# 创建新 tmux 会话，运行 Hermes gateway
tmux new-session -d -s "$SESSION_NAME" "cd /mnt/f/项目三：多Agent && hermes gateway run"

echo "Hermes 守护进程已启动 (tmux 会话: $SESSION_NAME)"
echo "使用 tmux attach -t $SESSION_NAME 查看日志"
echo "使用 tmux kill-session -t $SESSION_NAME 停止"
