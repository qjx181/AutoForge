#!/usr/bin/env bash
# setup.sh — 项目三：多Agent 一键安装脚本
# 用法: bash setup.sh [target-project-dir]

set -e

P3_DIR="$(cd "$(dirname "$0")" && pwd)"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     项目三：多Agent 安装向导        ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# ── 1. Python 版本检查 ──
echo -e "${YELLOW}[1/5] 检查 Python 环境...${NC}"
if command -v python3 &>/dev/null; then
    py_ver=$(python3 --version 2>&1)
    echo "  ✅ $py_ver"
else
    echo -e "${RED}  ❌ 未找到 python3${NC}"
    exit 1
fi

# ── 2. 安装依赖 ──
echo -e "${YELLOW}[2/5] 安装 Python 依赖...${NC}"
if [ -f "$P3_DIR/requirements.txt" ]; then
    pip install -r "$P3_DIR/requirements.txt" -q 2>&1 | tail -1 || true
    echo "  ✅ 依赖已安装"
else
    echo "  ⚠️ 未找到 requirements.txt，跳过"
fi

# ── 3. 注册 p3 CLI ──
echo -e "${YELLOW}[3/5] 注册 p3 命令...${NC}"
chmod +x "$P3_DIR/p3.sh" "$P3_DIR/p3.py"

# 检测是否已有别名
if grep -q "alias p3=" ~/.bashrc 2>/dev/null; then
    echo "  ⚠️ p3 别名已存在，跳过"
else
    echo "alias p3='$P3_DIR/p3.sh'" >> ~/.bashrc
    echo "  ✅ 已添加到 ~/.bashrc"
    echo "  💡 运行 source ~/.bashrc 立即生效"
fi

# ── 4. 注册优化目标 ──
echo -e "${YELLOW}[4/5] 配置优化目标...${NC}"
TARGET_DIR="${1:-}"
if [ -n "$TARGET_DIR" ]; then
    if [ -d "$TARGET_DIR" ]; then
        python3 "$P3_DIR/p3.py" setup "$TARGET_DIR"
        echo "  ✅ 目标已注册: $TARGET_DIR"
        # Also write to ~/.bashrc
        echo "export PROJECT1_DIR=\"$TARGET_DIR\"" >> ~/.bashrc
    else
        echo -e "${RED}  ❌ 目标目录不存在: $TARGET_DIR${NC}"
        echo "  你可以稍后用 p3 setup <target-dir> 注册"
    fi
else
    echo "  ⚠️ 未指定目标，跳过"
    echo "  稍后运行: p3 setup <你的项目路径>"
fi

# ── 5. 启动 cronjob ──
echo -e "${YELLOW}[5/5] 系统状态检查...${NC}"
python3 "$P3_DIR/p3.py" status

echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  项目三安装完成！${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""
echo "  常用命令:"
echo "    p3 status        查看系统状态"
echo "    p3 cost          查看成本报告"
echo "    p3 scan <目录>   手动扫描一个项目"
echo "    p3 setup <目录>  注册优化目标"
echo "    p3 cron on       开启自动循环"
echo "    p3 cron off      暂停自动循环"
echo ""
echo "  提示: source ~/.bashrc 让 p3 命令立即生效"
