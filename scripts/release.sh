#!/bin/bash
# daily-todo 上线脚本：测试全过 → 重启线上服务 → 验证线上可用
#
#   scripts/release.sh               跑全部测试，通过后重启线上服务并验证
#   scripts/release.sh --no-restart  只跑测试，不重启（不碰线上）
#
# 线上服务的真实运行方式（2026-10-07 核实）：
#   ~/Applications/每日待办.app（py2app 桌面壳，bundle id com.ocean.dailytodo）。
#   launchd com.ocean.daily-todo 只在每天 09:00 `open -a` 这个 App，本身不常驻；
#   App 内线程跑 server.py，监听 127.0.0.1:8766。
#   ⚠️ App 里带的是打包时那份 server.py/index.html 副本；重启只会重新拉起已安装的 App，
#      不会把本仓库的新代码装进去——要让新代码生效需重新打包（setup-on-this-mac.sh / GitHub release 走 App 内更新）。
#
# 测试不含 tests/browser_reorder.py：它依赖 playwright + 浏览器，本机 venv/系统 python 均未安装，
# 且不是 unittest 用例（文件名不是 test_*.py），不能稳定跑，故不纳入 release 门禁。
#
# 可选环境变量：
#   DAILY_TODO_LIVE_PORT  线上端口（默认 8766）
#   DAILY_TODO_POLL_SECS  重启后轮询秒数（默认 10）
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${HOME}/Applications/每日待办.app"
APP_BUNDLE_ID="com.ocean.dailytodo"
LIVE_PORT="${DAILY_TODO_LIVE_PORT:-8766}"
LIVE_URL="http://127.0.0.1:${LIVE_PORT}"
POLL_SECS="${DAILY_TODO_POLL_SECS:-10}"

notify() {
    osascript -e "display notification \"$1\" with title \"daily-todo release\"" >/dev/null 2>&1 || true
}

fail() {
    echo "✗ $1" >&2
    notify "$1"
    exit 1
}

# ---------- 1. 测试 ----------
run_tests() {
    local py="${DIR}/venv/bin/python"
    [ -x "$py" ] || py="python3"
    # 整个测试进程用临时 HOME / 数据目录兜底，即使某个用例漏打补丁也碰不到真实数据与本机 state
    local sandbox out rc=0
    sandbox="$(mktemp -d "${TMPDIR:-/tmp}/daily-todo-release.XXXXXX")"
    mkdir -p "${sandbox}/home" "${sandbox}/data"
    echo "==> 跑测试（${py}）"
    out="$(cd "$DIR" && HOME="${sandbox}/home" TODO_DATA_DIR="${sandbox}/data" \
        "$py" -m unittest discover -s tests 2>&1)" || rc=$?
    rm -rf "$sandbox"
    if [ "$rc" -ne 0 ]; then
        echo "$out" >&2
        fail "测试未通过（exit ${rc}），已中止，未重启线上服务"
    fi
    echo "$out" | grep -E '^(Ran |OK|FAILED)' || true
    TEST_COUNT="$(echo "$out" | sed -n 's/^Ran \([0-9][0-9]*\) test.*/\1/p' | tail -1)"
    [ -n "$TEST_COUNT" ] || fail "无法从输出解析测试数量，按失败处理"
}

# ---------- 2. 重启线上服务 ----------
listener_pids() {
    lsof -nP -iTCP:"${LIVE_PORT}" -sTCP:LISTEN -t 2>/dev/null || true
}

app_pids() {
    pgrep -f "Applications/每日待办.app/Contents/MacOS" 2>/dev/null || true
}

wait_port_free() {  # $1=秒
    local i
    for ((i = 0; i < $1 * 5; i++)); do
        [ -z "$(listener_pids)" ] && return 0
        sleep 0.2
    done
    return 1
}

stop_live() {
    local pid cmd
    # 端口上的监听者必须看起来是 daily-todo，才允许杀（避免误杀别的服务）
    for pid in $(listener_pids); do
        cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
        case "$cmd" in
            *每日待办*|*daily-todo*|*server.py*|*desktop_app.py*) ;;
            *) fail "端口 ${LIVE_PORT} 被非 daily-todo 进程占用（pid ${pid}: ${cmd}），未重启" ;;
        esac
    done
    if [ -n "$(app_pids)" ]; then
        # 先温和退出（仅当 App 确实在跑，避免 AppleScript 反而把它拉起来）
        osascript -e "tell application id \"${APP_BUNDLE_ID}\" to quit" >/dev/null 2>&1 || true
        wait_port_free 6 || true
    fi
    if [ -n "$(listener_pids)$(app_pids)" ]; then
        # shellcheck disable=SC2046
        kill -TERM $(listener_pids) $(app_pids) 2>/dev/null || true
        wait_port_free 3 || true
    fi
    if [ -n "$(listener_pids)" ]; then
        # shellcheck disable=SC2046
        kill -KILL $(listener_pids) 2>/dev/null || true
        wait_port_free 3 || fail "旧服务无法停止，端口 ${LIVE_PORT} 仍被占用"
    fi
}

start_live() {
    if [ -d "$APP" ]; then
        open -a "$APP"      # 与 launchd com.ocean.daily-todo 09:00 触发的命令一致
    else
        echo "  （未找到 ${APP}，退化为 launch.sh）"
        bash "${DIR}/launch.sh"
    fi
}

restart_live() {
    echo "==> 重启线上服务（${APP}，端口 ${LIVE_PORT}）"
    stop_live
    start_live
}

# ---------- 3. 验证线上 ----------
poll_live() {
    local deadline=$((SECONDS + POLL_SECS)) ok_home=0 ok_todos=0
    while [ "$SECONDS" -lt "$deadline" ]; do
        curl -fsS --max-time 2 -o /dev/null "${LIVE_URL}/" 2>/dev/null && ok_home=1 || ok_home=0
        curl -fsS --max-time 2 -o /dev/null "${LIVE_URL}/todos" 2>/dev/null && ok_todos=1 || ok_todos=0
        if [ "$ok_home" = 1 ] && [ "$ok_todos" = 1 ]; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

main() {
    local restart=1 arg
    for arg in "$@"; do
        case "$arg" in
            --no-restart) restart=0 ;;
            -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
            *) echo "未知参数：$arg（支持 --no-restart）" >&2; exit 2 ;;
        esac
    done

    SECONDS=0
    TEST_COUNT=0
    run_tests

    if [ "$restart" = 1 ]; then
        restart_live
        echo "==> 验证线上（${POLL_SECS}s 内轮询 / 与 /todos）"
        poll_live || fail "重启后 ${POLL_SECS}s 内线上 ${LIVE_URL} 的首页或 /todos 不通"
        echo "  线上首页与 /todos 均 200"
    else
        echo "==> --no-restart：跳过重启与线上验证"
    fi

    echo "✓ ${TEST_COUNT} 项全过 · 耗时 ${SECONDS}s"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
