#!/bin/bash
# daily-todo 一键卸载
# - 撤 launchd
# - 删 .app
# - 杀 server 进程
# - todos.json 数据保留
PLIST_DST="${HOME}/Library/LaunchAgents/com.ocean.daily-todo.plist"
APP_PATH="${HOME}/Applications/每日待办.app"

echo "==> 1/3 卸载 launchd"
launchctl unload "${PLIST_DST}" 2>/dev/null || true
rm -f "${PLIST_DST}"

echo "==> 2/3 删 .app"
rm -rf "${APP_PATH}"

echo "==> 3/3 停止 server"
pkill -f "daily-todo/server.py" 2>/dev/null || true

echo "✅ 已卸载。todos.json 数据保留。"
