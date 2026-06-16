#!/usr/bin/env python3
"""
daily-todo 本地 HTTP 服务
- GET  /              → 返回 index.html
- GET  /todos         → 返回 todos.json
- POST /todos         → 写回 todos.json
- 数据源：~/daily-todo/todos.json（外部，Claude 和页面共享）
- 资源（index.html）：跟脚本同目录 / .app bundle Resources
"""
import json
import os
import sys
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

PORT = 8766

# 数据目录走 TODO_DATA_DIR env，回退 ~/daily-todo-data 再回退 ~/daily-todo（代码在脚本所在处，数据分仓）
DATA_DIR = os.environ.get("TODO_DATA_DIR", "")
if not DATA_DIR:
    for _cand in ("~/daily-todo-data", "~/daily-todo"):
        _p = os.path.expanduser(_cand)
        if os.path.isdir(_p):
            DATA_DIR = _p
            break
    else:
        DATA_DIR = "~/daily-todo-data"
DATA_DIR = os.path.expanduser(DATA_DIR)
DATA_FILE = os.path.join(DATA_DIR, "todos.json")

# 本机状态（不进 iCloud 同步），存"每日任务"上次膨胀日期 + 备份
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/daily-todo")
STATE_FILE = os.path.join(APP_SUPPORT_DIR, "state.json")
BACKUP_DIR = os.path.join(APP_SUPPORT_DIR, "backups")
BACKUP_KEEP = 7


def today_str() -> str:
    """本地日（强制 Asia/Shanghai，避免跨时区差一天）"""
    now = datetime.now(TZ) if TZ else datetime.now()
    return now.strftime("%Y-%m-%d")


def read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_generated": {}}


def write_state(state: dict) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=APP_SUPPORT_DIR, delete=False, suffix=".tmp"
    )
    try:
        json.dump(state, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise


def backup_todos_json() -> None:
    """写 todos.json 前做时间戳备份，保留最近 BACKUP_KEEP 份"""
    if not os.path.exists(DATA_FILE):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DATA_FILE, os.path.join(BACKUP_DIR, f"todos-{ts}.json"))
        # cleanup
        backups = sorted(
            f for f in os.listdir(BACKUP_DIR)
            if f.startswith("todos-") and f.endswith(".json")
        )
        for old in backups[:-BACKUP_KEEP]:
            try:
                os.unlink(os.path.join(BACKUP_DIR, old))
            except Exception:
                pass
    except Exception:
        pass  # 备份失败不阻断主流程


def get_resource_path(name: str) -> str:
    """找 index.html：优先 .app bundle Resources，回退到 __file__ 同目录"""
    # 1. py2app .app bundle
    try:
        from Foundation import NSBundle  # type: ignore
        rp = NSBundle.mainBundle().resourcePath()
        if rp:
            full = os.path.join(str(rp), name)
            if os.path.exists(full):
                return full
    except Exception:
        pass
    # 2. 脚本同目录
    here = os.path.dirname(os.path.abspath(__file__))
    full = os.path.join(here, name)
    if os.path.exists(full):
        return full
    # 3. ~/daily-todo/（开发目录）
    fallback = os.path.join(DATA_DIR, name)
    return fallback


HTML_FILE = get_resource_path("index.html")


def get_version() -> str:
    """优先从我们自己的 .app Info.plist 读；venv/源码模式下 NSBundle 指向 Python.app 会读到 3.12.x，
    所以必须校验 bundleIdentifier 是 com.ocean.dailytodo 才采用，否则回退 VERSION 文件。"""
    try:
        from Foundation import NSBundle  # type: ignore
        bundle = NSBundle.mainBundle()
        bid = bundle.bundleIdentifier()
        if bid and str(bid) == "com.ocean.dailytodo":
            v = bundle.infoDictionary().get("CFBundleShortVersionString")
            if v:
                return str(v)
    except Exception:
        pass
    for path in [
        get_resource_path("VERSION"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
        os.path.join(DATA_DIR, "VERSION"),
    ]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read().strip()
            except Exception:
                pass
    return "0.0.0"


VERSION = get_version()
GITHUB_REPO = "zhenchuanwuzc-max/daily-todo-public"
GH_TOKEN_FILE = os.path.expanduser("~/daily-todo/.gh-token")


def read_gh_token() -> str:
    """读 ~/daily-todo/.gh-token，没有就返回空串"""
    try:
        if os.path.exists(GH_TOKEN_FILE):
            with open(GH_TOKEN_FILE) as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def fetch_latest_release() -> dict:
    """用 token 调 GitHub API 拿最新 release"""
    import urllib.request
    token = read_gh_token()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-todo-app",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


_progress_lock = threading.Lock()
_progress = {"status": "idle", "downloaded": 0, "total": 0, "error": None}


def _set_progress(**kwargs) -> None:
    with _progress_lock:
        _progress.update(kwargs)


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def download_asset(asset_api_url: str, dest_path: str) -> None:
    """下载 release asset 二进制（私仓需要 token + 特殊 Accept），并实时更新进度"""
    import urllib.request
    token = read_gh_token()
    req = urllib.request.Request(asset_api_url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "daily-todo-app",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        _set_progress(status="downloading", downloaded=0, total=total, error=None)
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                with _progress_lock:
                    _progress["downloaded"] += len(chunk)
    _set_progress(status="downloaded")


def install_update() -> dict:
    """异步触发：开线程跑下载 + updater，主请求立即返回"""
    rel = fetch_latest_release()
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").endswith(".zip")), None)
    if not asset:
        raise RuntimeError("release 里没找到 .zip 附件")

    def _worker():
        try:
            _do_install_update(rel, asset)
        except Exception as e:
            _set_progress(status="error", error=str(e))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    return {"ok": True, "started": True, "version": (rel.get("tag_name") or "").lstrip("v")}


def _do_install_update(rel: dict, asset: dict) -> None:
    """实际下载 + 写 updater + spawn + 自杀"""
    import shutil
    update_dir = os.path.expanduser("~/daily-todo/.update")
    if os.path.exists(update_dir):
        shutil.rmtree(update_dir)
    os.makedirs(update_dir, exist_ok=True)

    zip_path = os.path.join(update_dir, asset["name"])
    download_asset(asset["url"], zip_path)

    _set_progress(status="installing")

    target_app = os.path.expanduser("~/Applications/每日待办.app")
    log_path = "/tmp/daily-todo-updater.log"
    updater_sh = os.path.join(update_dir, "updater.sh")
    script = f"""#!/bin/bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
exec > "{log_path}" 2>&1
set -e
echo "[updater] start at $(date)"

# 等当前 App 进程退出（最多 30 秒）
for i in $(seq 1 60); do
    if ! pgrep -f "Applications/每日待办.app/Contents/MacOS" > /dev/null; then
        break
    fi
    sleep 0.5
done
# 兜底强杀
pkill -9 -f "Applications/每日待办.app/Contents/MacOS" 2>/dev/null || true
sleep 1

cd "{update_dir}"
mkdir -p extracted
ditto -x -k "{zip_path}" extracted/
NEW_APP=$(find extracted -maxdepth 2 -name "*.app" -type d | head -1)
if [ -z "$NEW_APP" ]; then
    echo "[updater] new .app not found in zip"
    exit 1
fi
echo "[updater] new app: $NEW_APP"

# 去 quarantine（zip 下载的会被打 quarantine 标，导致首次 Gatekeeper 拦截）
xattr -dr com.apple.quarantine "$NEW_APP" 2>/dev/null || true

# 替换
rm -rf "{target_app}"
mv "$NEW_APP" "{target_app}"
echo "[updater] replaced"

# 重新注册到 LaunchServices
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "{target_app}"
sleep 1

# 重启
open -a "{target_app}"
echo "[updater] reopened"

# 清理
cd /tmp
rm -rf "{update_dir}/extracted"
"""
    with open(updater_sh, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(updater_sh, 0o755)

    # detach 跑 updater（强制注入 UTF-8 locale，避免中文路径挂）
    import subprocess
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    subprocess.Popen(
        ["/bin/bash", updater_sh],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    _set_progress(status="restarting")

    # 1.5 秒后 server 自杀（让 webview 退出 + .app 进程结束）
    threading.Timer(1.5, lambda: os._exit(0)).start()


def read_data():
    if not os.path.exists(DATA_FILE):
        return {"updated": datetime.now().isoformat(timespec="seconds"), "tasks": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 用文件 mtime 覆盖 updated → Claude/外部直接改文件也能被 App polling 感知
    mtime = os.path.getmtime(DATA_FILE)
    data["updated"] = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
    return data


_sync_timer = None
_sync_lock = threading.Lock()


def schedule_sync(delay: float = 5.0) -> None:
    """防抖触发 git sync：5 秒内多次写入只跑一次"""
    global _sync_timer
    sync_sh = os.path.join(DATA_DIR, "sync.sh")
    if not os.path.exists(sync_sh):
        return
    with _sync_lock:
        if _sync_timer is not None:
            _sync_timer.cancel()
        _sync_timer = threading.Timer(delay, _run_sync, args=[sync_sh])
        _sync_timer.daemon = True
        _sync_timer.start()


def _run_sync(sync_sh: str) -> None:
    try:
        subprocess.Popen(
            ["/bin/bash", sync_sh],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=DATA_DIR,
            start_new_session=True,
        )
    except Exception:
        pass


_write_lock = threading.RLock()


def _atomic_write_todos(data) -> None:
    """假定调用方已持有 _write_lock。备份 → tmp + fsync + rename"""
    os.makedirs(DATA_DIR, exist_ok=True)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    backup_todos_json()
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=DATA_DIR, delete=False, suffix=".tmp"
    )
    try:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise


def write_data(data):
    with _write_lock:
        _atomic_write_todos(data)
    schedule_sync()


def add_single_task(item: dict) -> dict:
    """锁内 read-append-write 单条普通 task（Claude 打通用）。
    防 recur 污染 + 同 text 未完成幂等去重 + 跨机 UUID id。
    复用 _atomic_write_todos（已含写前备份 + 原子写 + 刷新 updated）。"""
    text = (item.get("text") or "").strip()
    if not text:
        raise ValueError("text required")
    import uuid
    with _write_lock:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"updated": None, "tasks": []}
        tasks = data.get("tasks", [])
        norm = text.replace(" ", "").lower()
        for t in tasks:
            if (not t.get("done") and not t.get("recur_source")
                    and t.get("text", "").replace(" ", "").lower() == norm):
                return t, True  # 已存在未完成 → 幂等返回，不重复加
        task = {
            "id": f"claude-{uuid.uuid4().hex[:12]}",
            "text": text,
            "done": False,
            "created": datetime.now().isoformat(timespec="seconds"),
            "done_at": None,
            "source": item.get("source", "claude"),
            "tag": item.get("tag", "work"),
            "priority": item.get("priority", "P2"),
            "due": item.get("due"),
        }
        if item.get("note"):
            task["note"] = str(item["note"])
        tasks.append(task)
        data["tasks"] = tasks
        _atomic_write_todos(data)
    schedule_sync()
    return task, False


def patch_task(task_id: str, patch: dict) -> dict:
    """锁内 read-modify-write 单条 task 字段（Claude 标完成/取消等远程更新打通用）。
    支持字段：done / done_at / tag / due / priority / text。
    done=True 自动盖 done_at = now；done=False 自动清 done_at = None（除非 patch 显式给）。
    返回更新后的 task；找不到 id raises KeyError。"""
    if not task_id:
        raise ValueError("task_id required")
    ALLOWED = {"done", "done_at", "tag", "due", "priority", "text"}
    with _write_lock:
        if not os.path.exists(DATA_FILE):
            raise KeyError(f"data file missing")
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks", [])
        target = None
        for t in tasks:
            if t.get("id") == task_id:
                target = t
                break
        if target is None:
            raise KeyError(f"task {task_id} not found")
        for k, v in patch.items():
            if k not in ALLOWED:
                continue
            target[k] = v
        # done 字段语义：True 时自动盖 done_at；False 时自动清 done_at（除非 patch 显式给 done_at）
        if "done" in patch and "done_at" not in patch:
            if patch["done"]:
                target["done_at"] = datetime.now().isoformat(timespec="seconds")
            else:
                target["done_at"] = None
        data["tasks"] = tasks
        _atomic_write_todos(data)
    schedule_sync()
    return target


# ============== 每日重复任务 ==============
# 数据模型：
#   种子 task   = 普通 task 加 `recurring: "daily"` 字段（不参与 done 状态）
#   今日实例    = task.id = f"recur:{seed.id}:{YYYY-MM-DD}"，带 `recur_source: seed.id`
# 触发：
#   1) server 启动时跑一次
#   2) APScheduler 每天 00:01 (Asia/Shanghai) 跑一次
# 幂等：state.last_generated[seed.id] + tasks 里同 id 二次去重，挡 iCloud 并发


def ensure_recurring_today() -> bool:
    """检查所有种子，生成今日缺失的实例 + 自动 skip 过期未完成实例。返回是否有写入。"""
    today = today_str()
    state = read_state()
    last_gen = state.get("last_generated") or {}
    state["last_generated"] = last_gen

    added = 0
    skipped = 0
    with _write_lock:
        if not os.path.exists(DATA_FILE):
            return False
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        tasks = data.get("tasks", [])
        now_iso = datetime.now().isoformat(timespec="seconds")

        for t in tasks:
            if not t.get("recur_source") or t.get("done"):
                continue
            inst_date = (t.get("id") or "").rsplit(":", 1)[-1]
            if len(inst_date) == 10 and inst_date < today:
                t["done"] = True
                t["done_at"] = now_iso
                t["auto_skipped"] = True
                t["skipped_for"] = inst_date
                skipped += 1

        seeds = [t for t in tasks if t.get("recurring") == "daily" and not t.get("recur_source")]
        if not seeds and skipped == 0:
            write_state(state)
            return False

        existing_ids = {t.get("id") for t in tasks}

        for seed in seeds:
            seed_id = seed.get("id")
            if not seed_id:
                continue
            instance_id = f"recur:{seed_id}:{today}"
            if last_gen.get(seed_id) == today and instance_id in existing_ids:
                continue
            if instance_id in existing_ids:
                last_gen[seed_id] = today
                continue
            tasks.append({
                "id": instance_id,
                "text": seed.get("text", ""),
                "done": False,
                "created": now_iso,
                "done_at": None,
                "source": "recur",
                "tag": seed.get("tag", "personal"),
                "priority": seed.get("priority", "P1"),
                "recur_source": seed_id,
            })
            existing_ids.add(instance_id)
            last_gen[seed_id] = today
            added += 1

        if added > 0 or skipped > 0:
            data["tasks"] = tasks
            _atomic_write_todos(data)

    write_state(state)
    if added > 0 or skipped > 0:
        schedule_sync()
        print(f"[recur] generated {added} / skipped {skipped} for {today}")
    return added > 0 or skipped > 0


_scheduler = None


def start_scheduler() -> None:
    """APScheduler：每天 00:01 Asia/Shanghai 触发膨胀；启动时立即跑一次"""
    global _scheduler
    try:
        ensure_recurring_today()
    except Exception as e:
        print(f"[recur] startup ensure failed: {e}")

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        sched = BackgroundScheduler(daemon=True)
        trigger = CronTrigger(hour=0, minute=1, timezone="Asia/Shanghai")
        sched.add_job(_scheduled_ensure, trigger, id="ensure_recurring_daily", replace_existing=True)
        sched.start()
        _scheduler = sched
        print("[recur] APScheduler started (daily 00:01 Asia/Shanghai)")
    except Exception as e:
        print(f"[recur] APScheduler unavailable, falling back to threading.Timer: {e}")
        _start_fallback_timer()


def _scheduled_ensure() -> None:
    try:
        ensure_recurring_today()
    except Exception as e:
        print(f"[recur] scheduled ensure failed: {e}")


def _start_fallback_timer() -> None:
    """APScheduler 不可用时的兜底：threading.Timer 自调度"""
    def _seconds_to_next_midnight_plus_one():
        now = datetime.now(TZ) if TZ else datetime.now()
        # 下一个 00:01
        next_run = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if next_run <= now:
            from datetime import timedelta
            next_run = next_run + timedelta(days=1)
        return max(60.0, (next_run - now).total_seconds())

    def _tick():
        try:
            ensure_recurring_today()
        except Exception as e:
            print(f"[recur] timer ensure failed: {e}")
        t = threading.Timer(_seconds_to_next_midnight_plus_one(), _tick)
        t.daemon = True
        t.start()

    t = threading.Timer(_seconds_to_next_midnight_plus_one(), _tick)
    t.daemon = True
    t.start()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            with open(HTML_FILE, "r", encoding="utf-8") as f:
                self._send(200, f.read(), "text/html")
        elif self.path == "/todos":
            self._send(200, json.dumps(read_data(), ensure_ascii=False))
        elif self.path == "/version":
            self._send(200, json.dumps({"version": VERSION, "repo": GITHUB_REPO}))
        elif self.path == "/update-progress":
            self._send(200, json.dumps(get_progress()))
        elif self.path == "/check-update":
            try:
                rel = fetch_latest_release()
                self._send(200, json.dumps({
                    "current": VERSION,
                    "latest": (rel.get("tag_name") or "").lstrip("v"),
                    "html_url": rel.get("html_url"),
                    "assets": [{"name": a.get("name"), "url": a.get("browser_download_url")}
                               for a in rel.get("assets", [])],
                    "has_token": bool(read_gh_token()),
                }))
            except Exception as e:
                # 区分 401/404（token 失效或私仓没 token）vs 网络错误
                msg = str(e)
                self._send(200, json.dumps({
                    "current": VERSION,
                    "error": msg,
                    "has_token": bool(read_gh_token()),
                }))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/todos":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            write_data(payload)
            self._send(200, json.dumps({"ok": True, "updated": payload.get("updated")}))
            return
        if self.path == "/install-update":
            try:
                result = install_update()
                self._send(200, json.dumps(result))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if self.path == "/sync-now":
            # 手动同步：跑 sync.sh（pull+push），读它写的状态文件回给前端
            sync_sh = os.path.join(DATA_DIR, "sync.sh")
            status_file = "/tmp/daily-todo-sync.status"
            if not os.path.exists(sync_sh):
                self._send(200, json.dumps({"ok": False, "status": "sync.sh 不存在"}, ensure_ascii=False))
                return
            try:
                os.remove(status_file)
            except OSError:
                pass
            try:
                subprocess.run(["/bin/bash", sync_sh], cwd=DATA_DIR, timeout=45,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status = ""
                try:
                    with open(status_file, encoding="utf-8") as f:
                        status = f.read().strip()
                except OSError:
                    pass
                self._send(200, json.dumps({"ok": status.startswith("ok"), "status": status or "未知"}, ensure_ascii=False))
            except subprocess.TimeoutExpired:
                self._send(200, json.dumps({"ok": False, "status": "同步超时（45s），可能网络慢，稍后再试"}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "status": str(e)}, ensure_ascii=False))
            return
        if self.path == "/ensure-recurring":
            try:
                added = ensure_recurring_today()
                self._send(200, json.dumps({"ok": True, "added": added}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if self.path == "/todos/add":
            length = int(self.headers.get("Content-Length", 0))
            try:
                item = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            try:
                task, deduped = add_single_task(item)
                self._send(200, json.dumps({"ok": True, "deduped": deduped, "task": task}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, '{"error":"not found"}')

    def do_PATCH(self):
        # PATCH /todos/{id} body {done|tag|due|priority|text|done_at: ...}
        if self.path.startswith("/todos/") and self.path.count("/") == 2:
            task_id = self.path.split("/", 2)[2]
            if not task_id:
                self._send(400, '{"error":"task_id required"}')
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                patch = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            try:
                task = patch_task(task_id, patch)
                self._send(200, json.dumps({"ok": True, "task": task}, ensure_ascii=False))
            except KeyError as e:
                self._send(404, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        return


def serve_forever():
    """供 desktop_app 在线程中调用"""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno in (48, 98):
            print(f"daily-todo already running on {PORT}, skip.")
            return
        raise
    print(f"daily-todo running at http://localhost:{PORT}")
    print(f"data: {DATA_FILE}")
    print(f"html: {HTML_FILE}")
    start_scheduler()
    srv.serve_forever()


if __name__ == "__main__":
    serve_forever()
