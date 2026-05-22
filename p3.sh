#!/bin/bash
# p3 — 项目三 CLI 包装脚本
# 安装: echo 'alias p3="/mnt/f/项目三：多Agent/p3.sh"' >> ~/.bashrc && source ~/.bashrc

P3_DIR="/mnt/f/项目三：多Agent"

if [ $# -eq 0 ]; then
    python3 "$P3_DIR/p3.py" --help
else
    python3 "$P3_DIR/p3.py" "$@"
fi
