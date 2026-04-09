#!/bin/bash
set -e

# 检查配置文件
if [ ! -f "/app/config/config.yaml" ]; then
    echo "❌ 配置文件缺失: /app/config/config.yaml"
    echo "   请确认 volume 挂载正确：- ./config:/app/config:ro"
    exit 1
fi

echo "🤖 调度执行器: agent-only"

case "${RUN_MODE:-all}" in

"web")
    echo "🌐 Web UI 模式: 0.0.0.0:${WEB_PORT:-8080}"
    cd /app && exec python -m src.web_main
    ;;

"all")
    echo "🚀 All-in-one 模式: web + worker + internal scheduler (port ${WEB_PORT:-8080})"

    # 立即执行一次（如果配置了）
    if [ "${IMMEDIATE_RUN:-false}" = "true" ]; then
        echo "▶ 立即入队一次: ${SCHEDULE_NAME:-（使用第一个 schedule）}"
        cd /app && python -m src.main --schedule-name "${SCHEDULE_NAME:-}" || true
    fi

    # 启动 Web UI 后台进程
    cd /app && python -m src.web_main &
    WEB_PID=$!
    echo "🌐 Web UI 已启动 (pid=$WEB_PID, port=${WEB_PORT:-8080})"

    # 前台运行 worker + 内置 scheduler
    exec python -m src.main --all-in-one
    ;;

"once")
    echo "🔄 单次执行模式: ${SCHEDULE_NAME:-（使用第一个 schedule）}"
    cd /app && exec python -m src.main --schedule-name "${SCHEDULE_NAME:-}"
    ;;

"worker")
    echo "⚙️ Worker 模式：internal scheduler + worker"
    if [ "${IMMEDIATE_RUN:-false}" = "true" ]; then
        echo "▶ 立即入队一次: ${SCHEDULE_NAME:-（使用第一个 schedule）}"
        cd /app && python -m src.main --schedule-name "${SCHEDULE_NAME:-}" || true
    fi
    exec python -m src.main --all-in-one
    ;;

*)
    exec "$@"
    ;;
esac
