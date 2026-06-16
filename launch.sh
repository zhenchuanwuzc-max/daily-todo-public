#!/bin/bash
# daily-todo 启动器（桌面 App 模式）
# - 用 venv python 跑 desktop_app.py（原生 WKWebView 窗口）
# - desktop_app.py 自己负责确保 server 在跑 + 弹窗口
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${DIR}/venv/bin/python"

if [ ! -x "${PY}" ]; then
    # venv 没建（首次或损坏）→ 退化到浏览器模式
    nohup python3 "${DIR}/server.py" > /tmp/daily-todo.log 2>&1 &
    disown
    sleep 1
    open "http://localhost:8766"
    exit 0
fi

# 桌面 App 模式：detach 让 .app 立即返回
nohup "${PY}" "${DIR}/desktop_app.py" > /tmp/daily-todo.log 2>&1 &
disown
