# ═══════════════════════════════════════════════════════════════════════
# MoreAgent — 多Agent自进化代码质量优化工具
# ═══════════════════════════════════════════════════════════════════════
# 构建: docker compose build
# 使用: docker compose run moreagent scan /workspace/目标目录
# ═══════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# 系统依赖 + 代码分析工具（分层扫描 Layer 0/1 需要）
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir ruff mypy

# 复制项目代码
COPY . /app

# 确保 data 目录存在
RUN mkdir -p /app/data

# 默认入口：显示帮助
ENTRYPOINT ["python3", "moreagent.py"]
CMD ["--help"]
