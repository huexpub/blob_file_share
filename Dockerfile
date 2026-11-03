# syntax=docker/dockerfile:1

# BFS 是「单进程 + 一个数据目录」的服务：前端是仓库里手写的静态文件，
# 没有 node 构建阶段，镜像就是 python-slim + 依赖。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BFS_HOST=0.0.0.0 \
    BFS_PORT=8000 \
    BFS_DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bfs ./bfs

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["python", "-m", "bfs", "serve", "--host", "0.0.0.0", "--port", "8000"]
