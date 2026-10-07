#!/bin/bash
# daily-todo 上线脚本（2026-10-07 起的新流程）：
#   测试 → 打版本标签 → GitHub Actions 打包发布 → 装到本机 → 验证线上 → 不过就退回旧版
#
#   scripts/release.sh               完整上线（会推送到公开仓库 + 发 GitHub Release + 替换本机 App）
#   scripts/release.sh --no-restart  只跑测试，不发版、不碰线上
#   scripts/release.sh --install [版本] 只把已发布的版本（默认最新）装到本机，其他电脑也用这个
#
# 线上 = ~/Applications/每日待办.app（py2app 桌面壳，App 内线程跑 server.py，127.0.0.1:8766）。
# App 里带的是打包时的代码副本，所以「上线」必须重新打包；打包统一走 .github/workflows/release.yml
# （CI 里会再跑一遍全部测试，并校验版本号/双架构/签名），其他电脑通过 App 内「立即更新」拿到同一个包。
#
# 测试不含 tests/browser_reorder.py：依赖 playwright + 浏览器，且不是 unittest 用例。
#
# 可选环境变量：
#   DAILY_TODO_LIVE_PORT  线上端口（默认 8766）
#   DAILY_TODO_POLL_SECS  重启后轮询秒数（默认 15）
#   DAILY_TODO_CI_MINUTES 等 GitHub 打包的上限分钟数（默认 20）
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${HOME}/Applications/每日待办.app"
APP_BUNDLE_ID="com.ocean.dailytodo"
GITHUB_REPO="zhenchuanwuzc-max/daily-todo-public"
LIVE_PORT="${DAILY_TODO_LIVE_PORT:-8766}"
LIVE_URL="http://127.0.0.1:${LIVE_PORT}"
POLL_SECS="${DAILY_TODO_POLL_SECS:-15}"
CI_MINUTES="${DAILY_TODO_CI_MINUTES:-20}"
BACKUP_DIR="${DIR}/.release-backup"

notify() {
    osascript -e "display notification \"$1\" with title \"daily-todo release\"" >/dev/null 2>&1 || true
}

on_unexpected_exit() {
    local rc=$? line="$1"
    trap - ERR
    echo "✗ 脚本在第 ${line} 行意外退出（rc=${rc}）" >&2
    [ "${INSTALLING:-0}" = 1 ] && rollback_local || true
    notify "上线脚本第 ${line} 行意外退出，$([ "${INSTALLING:-0}" = 1 ] && echo 已退回旧版 || echo 本机 App 未改动)"
    exit "$rc"
}

fail() {
    echo "✗ $1" >&2
    if [ "${INSTALLING:-0}" = 1 ]; then INSTALLING=0; rollback_local || true; fi
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
        # ps 会把中文路径转义，按「是不是待办 App 进程」判断，不靠匹配中文名
        if app_pids | grep -qx "$pid"; then continue; fi
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


# ---------- 0. 发版前检查 ----------
preflight() {
    cd "$DIR"
    command -v gh >/dev/null || fail "没有 gh 命令，无法发版"
    gh auth status >/dev/null 2>&1 || fail "gh 未登录，无法发版"
    [ -z "$(git status --porcelain)" ] || fail "工作区有未提交改动，先提交再上线"
    [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || fail "只能在 main 分支上线"
    # 标签以公开仓库为准（本地可能残留旧历史的同名标签）
    git fetch -q --force origin main --tags || fail "git fetch 失败（网络？）"
    [ "$(git rev-list --count HEAD..origin/main)" = 0 ] || fail "本地落后公开仓库，先 git pull --rebase 再上线"
}

# 下一个版本号 = max(VERSION 文件, 公开仓库所有 v* 标签) 的补丁号 +1（避开历史遗留的空标签）
next_version() {
    { cat "${DIR}/VERSION"; git -C "$DIR" ls-remote --tags origin 'v*' | sed -n 's#.*refs/tags/v\([^^]*\)$#\1#p'; } \
        | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1 \
        | awk -F. '{printf "%d.%d.%d", $1, $2, $3 + 1}'
}

# ---------- 2. 打标签 → 等 GitHub 打包 ----------
publish() {
    NEW_VERSION="$(next_version)"
    [ -n "$NEW_VERSION" ] || fail "算不出新版本号"
    local tag="v${NEW_VERSION}"
    echo "==> 发版 ${tag}"
    echo "$NEW_VERSION" > "${DIR}/VERSION"
    git -C "$DIR" commit -q -m "release: ${tag}" -- VERSION
    git -C "$DIR" push -q origin main || fail "推送 main 失败"
    git -C "$DIR" tag "$tag"
    git -C "$DIR" push -q origin "$tag" || fail "推送标签 ${tag} 失败"

    echo "==> 等 GitHub 打包（上限 ${CI_MINUTES} 分钟）"
    local run_id="" i
    for ((i = 0; i < 30; i++)); do
        run_id="$(gh run list -R "$GITHUB_REPO" --workflow release.yml --branch "$tag" \
            --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
        [ -n "$run_id" ] && break
        sleep 2
    done
    [ -n "$run_id" ] || fail "60 秒内没等到 ${tag} 的打包任务；本机 App 未改动"
    if ! timeout_run $((CI_MINUTES * 60)) gh run watch "$run_id" -R "$GITHUB_REPO" --exit-status --interval 10 >/dev/null; then
        fail "GitHub 打包失败或超时（gh run view ${run_id} -R ${GITHUB_REPO} --log-failed）；Release 未发布，本机 App 未改动"
    fi
    gh release view "$tag" -R "$GITHUB_REPO" --json assets -q '.assets[].name' | grep -q '\.zip$' \
        || fail "${tag} 的 Release 里没有安装包"
    echo "  ${tag} 已发布"
}

timeout_run() {  # $1=秒，其余为命令；macOS 没有 timeout，用后台计时兜底
    local secs="$1"; shift
    "$@" & local pid=$!
    sleep "$secs" </dev/null >/dev/null 2>&1 & local sleeper=$!
    ( wait "$sleeper" 2>/dev/null; kill "$pid" 2>/dev/null ) </dev/null >/dev/null 2>&1 &
    local rc=0
    wait "$pid" || rc=$?
    kill "$sleeper" 2>/dev/null || true
    return "$rc"
}

# ---------- 3. 装到本机（先备份旧版，失败可退回） ----------
install_local() {
    local tag="v${NEW_VERSION}" work
    work="$(mktemp -d "${TMPDIR:-/tmp}/daily-todo-install.XXXXXX")"
    echo "==> 下载并安装 ${tag}"
    gh release download "$tag" -R "$GITHUB_REPO" -p '*.zip' -D "$work" >/dev/null || fail "下载 ${tag} 安装包失败；本机 App 未改动"
    ditto -x -k "$work"/*.zip "$work/extracted" || fail "解压安装包失败；本机 App 未改动"
    NEW_APP_PATH="$(find "$work/extracted" -maxdepth 2 -name '*.app' -type d -print -quit)"
    [ -n "$NEW_APP_PATH" ] || fail "安装包里没有 .app；本机 App 未改动"
    xattr -dr com.apple.quarantine "$NEW_APP_PATH" 2>/dev/null || true

    INSTALLING=1
    stop_live
    rm -rf "$BACKUP_DIR"; mkdir -p "$BACKUP_DIR"
    if [ -d "$APP" ]; then
        mv "$APP" "$BACKUP_DIR/"
        HAS_BACKUP=1
    fi
    mkdir -p "$(dirname "$APP")"
    mv "$NEW_APP_PATH" "$APP"
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" || true
    rm -rf "$work"
    start_live
}

rollback_local() {
    [ "${HAS_BACKUP:-0}" = 1 ] || return 0
    echo "==> 退回旧版 App" >&2
    stop_live || true
    rm -rf "$APP"
    mv "$BACKUP_DIR/$(basename "$APP")" "$APP"
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" || true
    start_live
}

# ---------- 4. 验证线上 ----------
poll_live() {
    local deadline=$((SECONDS + POLL_SECS)) ok_home=0 ok_todos=0
    while [ "$SECONDS" -lt "$deadline" ]; do
        curl -fsS --max-time 2 -o /dev/null "${LIVE_URL}/" 2>/dev/null && ok_home=1 || ok_home=0
        curl -fsS --max-time 2 -o /dev/null "${LIVE_URL}/todos" 2>/dev/null && ok_todos=1 || ok_todos=0
        if [ "$ok_home" = 1 ] && [ "$ok_todos" = 1 ]; then
            [ -z "${NEW_VERSION:-}" ] && return 0
            curl -fsS --max-time 2 "${LIVE_URL}/version" 2>/dev/null | grep -q "\"${NEW_VERSION}\"" && return 0
        fi
        sleep 0.5
    done
    return 1
}

main() {
    local restart=1 arg mode=release
    trap 'on_unexpected_exit $LINENO' ERR
    for arg in "$@"; do
        case "$arg" in
            --no-restart) restart=0 ;;
            --install) mode=install ;;
            v[0-9]*|[0-9]*) NEW_VERSION="${arg#v}" ;;
            -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
            *) echo "未知参数：$arg（支持 --no-restart / --install [版本]）" >&2; exit 2 ;;
        esac
    done

    SECONDS=0
    TEST_COUNT=0
    if [ "$mode" = install ]; then  # 只安装已发布的版本（默认最新），不跑测试、不发版
        [ -n "${NEW_VERSION:-}" ] || NEW_VERSION="$(gh release view -R "$GITHUB_REPO" --json tagName -q .tagName | sed 's/^v//')"
        [ -n "$NEW_VERSION" ] || fail "查不到已发布的版本"
        install_local
        poll_live || fail "v${NEW_VERSION} 装上后 ${POLL_SECS}s 内线上不通或版本不对"
        INSTALLING=0
        echo "✓ 本机已装 v${NEW_VERSION}，首页与 /todos 正常 · 耗时 ${SECONDS}s"
        return 0
    fi
    if [ "$restart" = 1 ]; then preflight; fi
    run_tests

    if [ "$restart" = 1 ]; then
        publish
        install_local
        echo "==> 验证线上（${POLL_SECS}s 内：首页、/todos、/version = ${NEW_VERSION}）"
        if ! poll_live; then
            fail "v${NEW_VERSION} 装上后 ${POLL_SECS}s 内线上不通或版本不对，已退回旧版 App（GitHub Release 保留，需修复后发下一版）"
        fi
        INSTALLING=0
        echo "  线上已是 v${NEW_VERSION}，首页与 /todos 正常"
    else
        echo "==> --no-restart：只跑测试，不发版、不碰线上"
    fi

    echo "✓ ${TEST_COUNT} 项全过 · 耗时 ${SECONDS}s"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
