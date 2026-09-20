#!/bin/sh
# 容器启动入口：
#   1. 初始化 SQLite 数据库（建表幂等，已存在则跳过）
#   2. 启动 FastAPI (uvicorn)
# 依赖安装在镜像构建阶段已完成，因此“启动一条命令”即包含
# 依赖 + 数据库初始化 + 服务运行。
set -e

echo "[entrypoint] 初始化数据库: ${NESTING_DB_PATH:-/app/data/nesting.db}"
python -m app.init_db

echo "[entrypoint] 启动 uvicorn，监听 0.0.0.0:8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
