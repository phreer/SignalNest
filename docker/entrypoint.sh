#!/bin/bash
set -e

# 检查配置文件
if [ ! -f "/app/config/config.yaml" ]; then
    echo "❌ 配置文件缺失: /app/config/config.yaml"
    echo "   请确认 volume 挂载正确：- ./config:/app/config:ro"
    exit 1
fi

echo "🤖 调度执行器: agent-only"

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if [ "${IMMEDIATE_RUN:-false}" = "true" ]; then
    echo "▶ 立即入队一次: ${SCHEDULE_NAME:-（使用第一个 schedule）}"
    cd /app && python -m src.main --schedule-name "${SCHEDULE_NAME:-}" || true
fi

echo "🌐 启动单入口服务: Web UI + embedded worker + scheduler (port ${WEB_PORT:-8080})"
cd /app && exec python -m src.main
