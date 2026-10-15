#!/bin/bash
# daily-todo 跨机同步：沿用现有远端与多线路重试，不更改 remote URL。
# .sync.lock 串行整个同步；.data.lock 只保护本地提交/rebase/校验。
# fetch/push 在数据写锁外，弱网时仍可编辑事项。
set -e
unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
export TODO_DATA_DIR="${TODO_DATA_DIR:-$SCRIPT_DIR}"
if [ "${1:-}" != "--lock-held" ]; then
    exec "$PY" "$SCRIPT_DIR/sync-helper.py" locked "$SCRIPT_DIR/sync.sh"
fi
cd "$TODO_DATA_DIR"
LOG="${TODO_SYNC_LOG_FILE:-/tmp/daily-todo-sync.log}"
STATUS="${TODO_SYNC_STATUS_FILE:-/tmp/daily-todo-sync.status}"
log() { echo "[sync $(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null || true; }
status() { printf '%s\n' "$1" > "$STATUS"; }
trap 'status "fail: 同步中止（本地数据已保留）"; log "sync stopped before completion"' EXIT
status "busy: 正在同步"
log "start"

if [ -z "$(git config user.name)" ]; then git config user.name "daily-todo"; fi
if [ -z "$(git config user.email)" ]; then git config user.email "daily-todo@localhost"; fi
# shlex.quote preserves spaces/apostrophes in a user's workspace path.
DRIVER="$("$PY" -c 'import shlex,sys; print(shlex.quote(sys.executable)+" "+shlex.quote(sys.argv[1])+" %O %A %B")' "$SCRIPT_DIR/json-merge.py")"
git config merge.todos-union.driver "$DRIVER"
git config merge.todos-union.name "todos.json field-aware three-way merge"
if ! git remote get-url origin > /dev/null 2>&1; then
    status "fail: 未配置远端，同步未完成"
    trap - EXIT
    exit 1
fi

# Retain the existing 12s fetch / 20s push budgets, remembered working route,
# SSH 22 -> GitHub SSH 443 -> system HTTPS proxy, and HTTPS proxy fallback.
FETCH_TIMEOUT=12
PUSH_TIMEOUT=20
ROUTE_FILE="$(git rev-parse --git-path sync-route)"
BRANCH="${TODO_SYNC_BRANCH:-main}"
if [ "$(git symbolic-ref --quiet --short HEAD)" != "$BRANCH" ]; then
    status "fail: 当前分支与同步分支不符"
    trap - EXIT
    exit 1
fi
SSH_BASE="ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new"
run_to() { "$PY" "$SCRIPT_DIR/sync-helper.py" timeout "$@"; }
PROXY=""
if [ "$(scutil --proxy 2>/dev/null | awk '/HTTPSEnable/{print $3}')" = "1" ]; then
    PH="$(scutil --proxy | awk '/HTTPSProxy/{print $3}')"
    PP="$(scutil --proxy | awk '/HTTPSPort/{print $3}')"
    if [ -n "$PH" ] && [ -n "$PP" ]; then PROXY="$PH:$PP"; fi
fi
GIT_CFG=()
use_route() {
    GIT_CFG=()
    case "$1" in
        ssh22) export GIT_SSH_COMMAND="$SSH_BASE" ;;
        ssh443) export GIT_SSH_COMMAND="$SSH_BASE -o Hostname=ssh.github.com -p 443" ;;
        ssh443-proxy) [ -n "$PROXY" ] || return 1
            export GIT_SSH_COMMAND="$SSH_BASE -o Hostname=ssh.github.com -p 443 -o ProxyCommand='nc -X connect -x $PROXY %h %p'" ;;
        https|local) ;;
        https-proxy) [ -n "$PROXY" ] || return 1; GIT_CFG=(-c "http.proxy=http://$PROXY") ;;
        *) return 1 ;;
    esac
}
REMOTE="$(git remote get-url origin)"
case "$REMOTE" in
    https://*) ALL_ROUTES="https https-proxy" ;;
    /*|./*|../*|file://*) ALL_ROUTES="local" ;;
    *) ALL_ROUTES="ssh22"
       # Only GitHub uses ssh.github.com. Resolve SSH aliases without connecting.
       REMOTE_HOST="$("$PY" -c 'import sys,urllib.parse; u=sys.argv[1]; print((urllib.parse.urlsplit(u).hostname or "") if "://" in u else u.split(":",1)[0].split("@")[-1])' "$REMOTE")"
       RESOLVED_HOST="$(ssh -G "$REMOTE_HOST" 2>/dev/null | awk '$1 == "hostname" {print $2; exit}')"
       case "$REMOTE_HOST $RESOLVED_HOST" in *github.com*) ALL_ROUTES="ssh22 ssh443 ssh443-proxy" ;; esac ;;
esac
LAST="$(cat "$ROUTE_FILE" 2>/dev/null || true)"
ROUTES="${SYNC_ROUTES:-$LAST $ALL_ROUTES}"
ROUTE=""; TRIED=""
for r in $ROUTES; do
    case " $TRIED " in *" $r "*) continue ;; esac
    # A remembered route must still be valid after the remote is reconfigured.
    case " $ALL_ROUTES " in *" $r "*) ;; *) continue ;; esac
    TRIED="$TRIED $r"
    use_route "$r" || continue
    if run_to "$FETCH_TIMEOUT" git "${GIT_CFG[@]}" fetch -q origin "$BRANCH" >> "$LOG" 2>&1; then
        ROUTE="$r"; echo "$r" > "$ROUTE_FILE"; break
    else
        log "fetch via $r failed (rc=$?)"
    fi
done
if [ -z "$ROUTE" ]; then
    status "fail: 无法连接远端（已保留本地，等下次）"
    trap - EXIT
    exit 1
fi
log "fetch ok via $ROUTE"

if ! "$PY" "$SCRIPT_DIR/sync-helper.py" local "origin/$BRANCH" >> "$LOG" 2>&1; then
    status "fail: 本地合并或校验失败（已保留本地，请查看同步日志）"
    trap - EXIT
    exit 1
fi
log "local commit/rebase/validation ok"
# No .data.lock here: edits made while pushing remain dirty for the next run.
AHEAD="$(git rev-list --count "origin/$BRANCH..HEAD")"
if [ "$AHEAD" -gt 0 ]; then
    if run_to "$PUSH_TIMEOUT" git "${GIT_CFG[@]}" push -q origin "$BRANCH" >> "$LOG" 2>&1; then
        log "pushed $AHEAD commit(s) via $ROUTE"
    else
        status "fail: 推送失败（已本地提交，下次自动补推）"
        trap - EXIT
        exit 1
    fi
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    status "pending: 已同步快照，新修改等待下次同步"
else
    status "ok: 已是最新"
fi
trap - EXIT
