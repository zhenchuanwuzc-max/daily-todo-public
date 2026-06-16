#!/bin/bash
# 启动 daily-todo 本地服务（默认 http://localhost:8765）
cd "$(dirname "$0")"
exec python3 server.py
