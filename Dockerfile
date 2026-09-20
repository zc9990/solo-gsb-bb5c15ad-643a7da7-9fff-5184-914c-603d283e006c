# 板材直切排样 API 镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 仅先装依赖，利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码与启动脚本
COPY app ./app
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh && mkdir -p /app/data

# SQLite 数据库文件所在目录（命名卷挂载点）
ENV NESTING_DB_PATH=/app/data/nesting.db
VOLUME ["/app/data"]

EXPOSE 8000

# 一条启动命令：数据库初始化 + 启动服务
ENTRYPOINT ["./entrypoint.sh"]
