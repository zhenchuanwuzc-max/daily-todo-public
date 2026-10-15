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
import fcntl
import uuid
import os
import sys
import shutil
import subprocess
import tempfile
import threading
import hashlib
import re
import unicodedata
import zipfile
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

class ThreadingHTTPServer(_ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


PORT = int(os.environ.get("TODO_PORT", "8766"))

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
APP_SUPPORT_DIR = os.path.expanduser(os.environ.get(
    "TODO_APP_SUPPORT_DIR", "~/Library/Application Support/daily-todo"))
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
RELEASE_CHANNEL = "project-preview"
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
    """公开仓库走 release 网页，避免共享出口的匿名 API 速率限制。"""
    import urllib.request
    token = read_gh_token()
    if not token:
        return fetch_public_release()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "daily-todo-app",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _ReleaseAssetsParser(HTMLParser):
    def __init__(self, tag: str):
        super().__init__()
        self.prefix = f"/{GITHUB_REPO}/releases/download/{quote(tag, safe='')}/"
        self.rows = []
        self.row = None

    def handle_starttag(self, name, attrs):
        attrs = dict(attrs)
        if name == "li":
            self.row = {"url": None, "digest": None}
        if self.row is None:
            return
        href = attrs.get("href", "")
        if name == "a" and href.startswith(self.prefix) and href.lower().endswith(".zip"):
            self.row["url"] = "https://github.com" + href
        digest = attrs.get("value", "")
        if name == "clipboard-copy" and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            self.row["digest"] = digest

    def handle_endtag(self, name):
        if name == "li" and self.row is not None:
            if self.row["url"]:
                self.rows.append(self.row)
            self.row = None


def fetch_public_release() -> dict:
    import urllib.request
    latest_url = f"https://github.com/{GITHUB_REPO}/releases/latest"
    with urllib.request.urlopen(urllib.request.Request(latest_url, headers={
        "User-Agent": "daily-todo-app",
    }), timeout=15) as resp:
        release_url = resp.geturl()
    prefix = f"/{GITHUB_REPO}/releases/tag/"
    parsed = urlsplit(release_url)
    if parsed.netloc != "github.com" or not parsed.path.startswith(prefix):
        raise RuntimeError("无法确认最新版本地址")
    tag = unquote(parsed.path[len(prefix):])
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise RuntimeError("最新版本号格式异常")

    assets_url = f"https://github.com/{GITHUB_REPO}/releases/expanded_assets/{quote(tag, safe='')}"
    with urllib.request.urlopen(urllib.request.Request(assets_url, headers={
        "User-Agent": "daily-todo-app",
    }), timeout=15) as resp:
        parser = _ReleaseAssetsParser(tag)
        parser.feed(resp.read().decode("utf-8"))
    assets = []
    for row in parser.rows:
        if not row["digest"]:
            continue  # 下载包必须带 GitHub 公布的 SHA-256
        url = row["url"]
        assets.append({"name": unquote(urlsplit(url).path.rsplit("/", 1)[-1]),
                       "url": url, "browser_download_url": url,
                       "digest": row["digest"]})
    if not assets:
        raise RuntimeError("release 中没有带 SHA-256 的 .zip 安装包")
    return {"tag_name": tag, "html_url": release_url, "assets": assets}


_progress_lock = threading.Lock()
_progress = {"status": "idle", "downloaded": 0, "total": 0, "error": None}


def _set_progress(**kwargs) -> None:
    with _progress_lock:
        _progress.update(kwargs)


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def download_asset(asset_url: str, dest_path: str, digest: str = "") -> None:
    """下载 release 附件、更新进度，并校验 GitHub 公布的 SHA-256。"""
    import urllib.request
    token = read_gh_token()
    req = urllib.request.Request(asset_url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "daily-todo-app",
    })
    if token and urlsplit(asset_url).netloc == "api.github.com":
        req.add_header("Authorization", f"Bearer {token}")
    sha = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        _set_progress(status="downloading", downloaded=0, total=total, error=None)
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                with _progress_lock:
                    _progress["downloaded"] += len(chunk)
    if digest and sha.hexdigest().lower() != digest.removeprefix("sha256:").lower():
        os.unlink(dest_path)
        raise RuntimeError("安装包 SHA-256 校验失败")
    if not zipfile.is_zipfile(dest_path):
        os.unlink(dest_path)
        raise RuntimeError("下载内容不是 ZIP 安装包")
    _set_progress(status="downloaded")


def install_update() -> dict:
    """异步触发：开线程跑下载 + updater，主请求立即返回"""
    if RELEASE_CHANNEL != "stable" or os.environ.get("TODO_DISABLE_UPDATE") == "1":
        raise ValueError("项目跟进版本不使用公开版本更新，请通过本地开发入口升级")
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
    download_asset(asset["url"], zip_path, asset.get("digest", ""))

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
    """Return a compatible view without migrating or rewriting legacy records."""
    with _write_lock:
        if not os.path.exists(DATA_FILE):
            return {"updated": None, "tasks": [], "projects": [],
                    "milestones": [], "people": [], "weekly_report_groups": [],
                    "revision": 0}
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("tasks", [])
        data.setdefault("projects", [])
        data.setdefault("milestones", [])
        data.setdefault("people", [])
        data.setdefault("weekly_report_groups", [])
        data.setdefault("revision", 0)
        # Include file mtime at microsecond precision for out-of-process writers.
        data["updated"] = datetime.fromtimestamp(
            os.path.getmtime(DATA_FILE), timezone.utc).isoformat(timespec="microseconds")
        return data


_sync_timer = None
_sync_lock = threading.Lock()


def schedule_sync(delay: float = 5.0) -> None:
    """防抖触发 git sync：5 秒内多次写入只跑一次"""
    global _sync_timer
    if os.environ.get("TODO_DISABLE_SYNC") == "1":
        return
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


class _DataLock:
    """Serialize threads and local sync processes; recursive entry flocks only once."""
    def __init__(self):
        self.thread_lock = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.thread_lock.acquire()
        depth = getattr(self.local, "depth", 0)
        try:
            if depth == 0:
                directory = os.path.dirname(DATA_FILE)
                os.makedirs(directory, exist_ok=True)
                self.local.handle = open(os.path.join(directory, ".data.lock"), "a+")
                fcntl.flock(self.local.handle.fileno(), fcntl.LOCK_EX)
            self.local.depth = depth + 1
        except Exception:
            if depth == 0 and getattr(self.local, "handle", None):
                self.local.handle.close()
            self.thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.local.depth -= 1
            if self.local.depth == 0:
                fcntl.flock(self.local.handle.fileno(), fcntl.LOCK_UN)
                self.local.handle.close()
        finally:
            self.thread_lock.release()


_write_lock = _DataLock()
_timestamp_lock = threading.Lock()
_last_timestamp = datetime.min.replace(tzinfo=timezone.utc)


def timestamp():
    global _last_timestamp
    with _timestamp_lock:
        now = datetime.now(timezone.utc)
        if now <= _last_timestamp:
            now = _last_timestamp + timedelta(microseconds=1)
        _last_timestamp = now
        return now.isoformat(timespec="microseconds")


def stamp_fields(entity, fields, stamp=None):
    if not fields:
        return
    stamp = stamp or timestamp()
    entity["updated_at"] = stamp
    entity.setdefault("_field_updated", {}).update({field: stamp for field in fields})


class ConflictError(ValueError):
    pass


class RequestOriginError(ValueError):
    pass


def _atomic_write_todos(data) -> None:
    """假定调用方已持有 _write_lock。备份 → tmp + fsync + rename"""
    directory = os.path.dirname(DATA_FILE)
    os.makedirs(directory, exist_ok=True)
    existing_revision = 0
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as current:
            existing_revision = json.load(current).get("revision", 0)
    data["revision"] = max(existing_revision, data.get("revision", 0)) + 1
    data["updated"] = timestamp()
    backup_todos_json()
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
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


def reorder_tasks(item):
    """Persist only ordering, against current tasks under the existing write lock."""
    ids = item.get("ids") if isinstance(item, dict) else None
    if (not isinstance(ids, list) or len(ids) < 2
            or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("ids must contain at least two unique task IDs")
    with _write_lock:
        data = read_data()
        by_id = {t["id"]: t for t in data.get("tasks", [])}
        selected = [by_id.get(i) for i in ids]
        for task in selected:
            if (task is None or task.get("deleted_at") or task.get("done")
                    or task.get("source") == "claude-suggest"
                    or (task.get("recurring") in ("daily", "weekly", "monthly")
                        and not task.get("recur_source"))):
                raise ValueError("列表已变化，请刷新后重新排序")
        def group(task):
            priority = task.get("priority")
            # Legacy P3 is displayed with P2; keep its stored value untouched.
            if priority == "P3":
                return "P2"
            return priority if priority in ("P0", "P1", "P2") else "PN"
        if len({group(t) for t in selected}) != 1:
            raise ValueError("请在同一优先级内排序")
        for position, task in enumerate(selected):
            task["sort_order"] = position
            stamp_fields(task, ["sort_order"])
        _atomic_write_todos(data)
    schedule_sync()
    return {"ok": True, "updated": data["updated"], "revision": data["revision"]}


def required_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} required")
    return value.strip()


def followup_text(value, field, *, allow_empty=True, allow_spaces=False):
    """Normalize configured identifiers without turning numeric IDs into numbers."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise ValueError(f"{field} cannot contain control characters")
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError(f"{field} required")
    if len(value) > 256 or (not allow_spaces and any(char.isspace() for char in value)):
        raise ValueError(f"{field} must be at most 256 characters without whitespace")
    return value


def followup_id(kind, value):
    return f"{kind}-{uuid.uuid5(uuid.NAMESPACE_URL, 'daily-todo:' + kind + ':' + value).hex}"


def upsert_people(item):
    """Apply only supplied rows, rejecting stale employee IDs before any write."""
    rows = item.get("people")
    if not isinstance(rows, list):
        raise ValueError("people must be an array")
    normalized, names = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each person must be an object")
        name = followup_text(row.get("name"), "name", allow_empty=False, allow_spaces=True)
        identity = name.casefold()
        if identity in names:
            raise ValueError("同一批人员姓名不能重复")
        names.add(identity)
        employee_id = followup_text(row.get("employee_id"), "employee_id")
        base = followup_text(row.get("base_employee_id"), "base_employee_id")
        normalized.append((name, identity, employee_id, base))
    changed = False
    with _write_lock:
        data = read_data()
        existing = {person["name"].strip().casefold(): person for person in data["people"]}
        for name, identity, employee_id, base in normalized:
            current = existing.get(identity, {}).get("employee_id", "")
            if current != base and current != employee_id:
                raise ConflictError(f"{name} 的工号已更新，请刷新后再修改")
        for name, identity, employee_id, _base in normalized:
            person = existing.get(identity)
            if person is None:
                stamp = timestamp()
                person = {"id": followup_id("person", identity), "name": name,
                          "employee_id": employee_id, "created": stamp}
                stamp_fields(person, list(person), stamp)
                data["people"].append(person)
                existing[identity] = person
                changed = True
            elif person.get("employee_id", "") != employee_id:
                person["employee_id"] = employee_id
                stamp_fields(person, ["employee_id"])
                changed = True
        if changed:
            _atomic_write_todos(data)
        result = {"ok": True, "people": data["people"], "revision": data["revision"]}
    if changed:
        schedule_sync()
    return result


def followup_group_ids(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    groups = list(dict.fromkeys(followup_text(group, field, allow_empty=False) for group in value))
    if len(groups) > 100:
        raise ValueError(f"{field} must contain at most 100 groups")
    return groups


def update_weekly_report_groups(item):
    """Merge the caller's additions/removals, retaining other clients' new groups."""
    group_ids = followup_group_ids(item.get("group_ids"), "group_ids")
    base_ids = followup_group_ids(item.get("base_group_ids"), "base_group_ids")
    additions = [group for group in group_ids if group not in base_ids]
    removals = [group for group in base_ids if group not in group_ids]
    changed = False
    with _write_lock:
        data = read_data()
        by_group = {group["group_id"]: group for group in data["weekly_report_groups"]}
        active = {group_id for group_id, group in by_group.items() if not group.get("deleted_at")}
        if len((active | set(additions)) - set(removals)) > 100:
            raise ValueError("周报接收群最多 100 个，请刷新后调整")
        for group_id, deleted in [(group, False) for group in additions] + [(group, True) for group in removals]:
            group = by_group.get(group_id)
            if group is None:
                stamp = timestamp()
                group = {"id": followup_id("weekly-group", group_id), "group_id": group_id,
                         "deleted_at": stamp if deleted else None, "created": stamp}
                stamp_fields(group, list(group), stamp)
                data["weekly_report_groups"].append(group)
                by_group[group_id] = group
                changed = True
            elif bool(group.get("deleted_at")) != deleted:
                stamp = timestamp()
                group["deleted_at"] = stamp if deleted else None
                stamp_fields(group, ["deleted_at"], stamp)
                changed = True
        if changed:
            _atomic_write_todos(data)
        result = {"ok": True, "weekly_report_groups": data["weekly_report_groups"],
                  "group_ids": [group["group_id"] for group in data["weekly_report_groups"]
                                if not group.get("deleted_at")], "revision": data["revision"]}
    if changed:
        schedule_sync()
    return result


def valid_date(value, field, required=False):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a real YYYY-MM-DD date") from None
    return value


def entity_by_id(data, collection, entity_id, include_deleted=False):
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError("id required")
    target = next((e for e in data.get(collection, []) if e.get("id") == entity_id), None)
    if target is None or (target.get("deleted_at") and not include_deleted):
        raise KeyError(f"{collection} {entity_id} not found")
    return target


def validate_task_links(data, task, previous=None):
    project_id, milestone_id = task.get("project_id"), task.get("milestone_id")
    for value in (project_id, milestone_id):
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError("project_id and milestone_id must be an ID or null")
    if (task.get("recurring") or task.get("recur_source")) and (project_id or milestone_id):
        raise ValueError("循环事项暂不关联项目或里程碑")
    # A title/completion edit must still work while the linked entity is deleted.
    if previous is not None and all(task.get(k) == previous.get(k)
                                    for k in ("project_id", "milestone_id")):
        return
    try:
        if project_id:
            entity_by_id(data, "projects", project_id)
        if milestone_id:
            milestone = entity_by_id(data, "milestones", milestone_id)
            if not project_id or milestone.get("project_id") != project_id:
                raise ValueError("里程碑必须属于所选项目")
    except KeyError:
        raise ValueError("项目或里程碑已删除，请重新选择") from None


def add_single_task(item: dict):
    """Append a fresh task; same wording can legitimately belong to many projects."""
    text = required_text(item.get("text"), "text")
    owner = required_text(item.get("owner", "Ocean"), "owner")
    deadline = valid_date(item.get("deadline"), "deadline", required=True)
    with _write_lock:
        data = read_data()
        stamp = timestamp()
        task = {
            "id": f"task-{uuid.uuid4().hex}", "text": text,
            "owner": owner, "deadline": deadline, "done": False,
            "created": stamp, "done_at": None,
            "source": item.get("source", "user"), "tag": item.get("tag", "work"),
            "priority": item.get("priority", "P2"),
            "due": valid_date(item.get("due"), "due"),
            "project_id": item.get("project_id"), "milestone_id": item.get("milestone_id"),
        }
        if item.get("recurring") or item.get("recur_source"):
            raise ValueError("使用循环事项入口创建循环任务")
        if item.get("note"):
            task["note"] = str(item["note"])
        validate_task_links(data, task)
        stamp_fields(task, list(task), stamp)
        data["tasks"].append(task)
        _atomic_write_todos(data)
    schedule_sync()
    return task, False


def add_recurring_seed(item: dict):
    """Validate and atomically add a daily, weekly, or monthly seed."""
    text = (item.get("text") or "").strip()
    if not text:
        raise ValueError("text required")

    recurring = item.get("recurring")
    if recurring not in {"daily", "weekly", "monthly"}:
        raise ValueError("recurring must be daily, weekly, or monthly")

    schedule = {}
    if recurring == "weekly":
        weekday = item.get("recur_weekday")
        if (
            isinstance(weekday, bool)
            or not isinstance(weekday, int)
            or not 1 <= weekday <= 7
        ):
            raise ValueError("recur_weekday must be an integer from 1 to 7")
        schedule["recur_weekday"] = weekday
    elif recurring == "monthly":
        monthday = item.get("recur_monthday")
        if (
            isinstance(monthday, bool)
            or not isinstance(monthday, int)
            or not 1 <= monthday <= 31
        ):
            raise ValueError("recur_monthday must be an integer from 1 to 31")
        schedule["recur_monthday"] = monthday

    import uuid
    with _write_lock:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"updated": None, "tasks": []}
        tasks = data.get("tasks", [])
        norm = text.replace(" ", "").lower()
        for existing in tasks:
            if existing.get("deleted_at") or existing.get("recur_source"):
                continue
            if existing.get("recurring") != recurring:
                continue
            if existing.get("text", "").replace(" ", "").lower() != norm:
                continue
            if any(existing.get(key) != value for key, value in schedule.items()):
                continue
            return existing, True

        stamp = timestamp()
        seed = {
            "id": f"seed_{uuid.uuid4().hex[:12]}",
            "text": text,
            "done": False,
            "created": stamp,
            "done_at": None,
            "source": item.get("source", "user"),
            "tag": item.get("tag", "personal"),
            "priority": item.get("priority", "P1"),
            "recurring": recurring,
            "owner": required_text(item.get("owner", "Ocean"), "owner"),
            **schedule,
        }
        if item.get("project_id") or item.get("milestone_id"):
            raise ValueError("循环事项暂不关联项目或里程碑")
        stamp_fields(seed, list(seed), stamp)
        tasks.append(seed)
        data["tasks"] = tasks
        _atomic_write_todos(data)
    schedule_sync()
    return seed, False


TASK_FIELDS = {"done", "done_at", "tag", "due", "priority", "text", "owner",
               "deadline", "project_id", "milestone_id", "source", "note", "sort_order"}


def apply_task_patch(data, target, patch):
    changes = {key: value for key, value in patch.items() if key in TASK_FIELDS}
    for field in ("text", "owner"):
        if field in changes:
            changes[field] = required_text(changes[field], field)
    if "deadline" in changes:
        # Legacy tasks may stay undated, but a deadline once set cannot be emptied.
        changes["deadline"] = valid_date(changes["deadline"], "deadline",
                                         required=bool(target.get("deadline")))
    if "due" in changes:
        changes["due"] = valid_date(changes["due"], "due")
    if "done" in changes and not isinstance(changes["done"], bool):
        raise ValueError("done must be boolean")
    if "sort_order" in changes and (isinstance(changes["sort_order"], bool)
                                    or not isinstance(changes["sort_order"], (int, float))):
        raise ValueError("sort_order must be numeric")
    candidate = {**target, **changes}
    validate_task_links(data, candidate, previous=target)
    stamp = timestamp()
    if "done" in changes and changes["done"] != bool(target.get("done")):
        changes["done_at"] = stamp if changes["done"] else None
        if target.get("auto_skipped"):
            changes["auto_skipped"] = False
    changed = [key for key, value in changes.items() if target.get(key) != value]
    target.update(changes)
    stamp_fields(target, changed, stamp)


def patch_task(task_id: str, patch: dict) -> dict:
    with _write_lock:
        data = read_data()
        target = entity_by_id(data, "tasks", task_id)
        apply_task_patch(data, target, patch)
        _atomic_write_todos(data)
    schedule_sync()
    return target


def add_project(item):
    name = required_text(item.get("name"), "name")
    owner = required_text(item.get("owner", "Ocean"), "owner")
    if not isinstance(item.get("core", False), bool):
        raise ValueError("core must be boolean")
    if not isinstance(item.get("goal", ""), str):
        raise ValueError("goal must be text")
    group_id = (followup_text(item["dingtalk_group_id"], "dingtalk_group_id")
                if "dingtalk_group_id" in item else None)
    with _write_lock:
        data = read_data()
        stamp = timestamp()
        project = {"id": f"project-{uuid.uuid4().hex}", "name": name, "owner": owner,
                   "goal": item.get("goal", "").strip(), "core": item.get("core", False),
                   "archived": False, "deleted_at": None, "created": stamp}
        if group_id is not None:
            project["dingtalk_group_id"] = group_id
        stamp_fields(project, list(project), stamp)
        data["projects"].append(project)
        _atomic_write_todos(data)
    schedule_sync()
    return project


def add_milestone(item):
    name = required_text(item.get("name"), "name")
    due = valid_date(item.get("due"), "due")
    with _write_lock:
        data = read_data()
        project = entity_by_id(data, "projects", item.get("project_id"))
        owner = required_text(item.get("owner", project.get("owner") or "Ocean"), "owner")
        siblings = [m for m in data["milestones"] if m.get("project_id") == project["id"]]
        stamp = timestamp()
        milestone = {"id": f"milestone-{uuid.uuid4().hex}", "name": name, "owner": owner,
                     "project_id": project["id"], "due": due, "deleted_at": None,
                     "sort_order": max((m.get("sort_order", -1) for m in siblings), default=-1) + 1,
                     "created": stamp}
        stamp_fields(milestone, list(milestone), stamp)
        data["milestones"].append(milestone)
        _atomic_write_todos(data)
    schedule_sync()
    return milestone


def patch_entity(collection, entity_id, patch):
    allowed = {"name", "owner", "goal", "core", "archived", "dingtalk_group_id"} if collection == "projects" else {
        "name", "owner", "due"}
    with _write_lock:
        data = read_data()
        target = entity_by_id(data, collection, entity_id)
        changes = {key: value for key, value in patch.items() if key in allowed}
        for field in ("name", "owner"):
            if field in changes:
                changes[field] = required_text(changes[field], field)
        for field in ("core", "archived"):
            if field in changes and not isinstance(changes[field], bool):
                raise ValueError(f"{field} must be boolean")
        if "goal" in changes:
            if not isinstance(changes["goal"], str):
                raise ValueError("goal must be text")
            changes["goal"] = changes["goal"].strip()
        if "dingtalk_group_id" in changes:
            changes["dingtalk_group_id"] = followup_text(changes["dingtalk_group_id"], "dingtalk_group_id")
        if "due" in changes:
            changes["due"] = valid_date(changes["due"], "due")
        fields = [key for key, value in changes.items() if target.get(key) != value]
        target.update(changes)
        stamp_fields(target, fields)
        _atomic_write_todos(data)
    schedule_sync()
    return target


def set_deleted(collection, entity_id, deleted=True):
    """Keep association IDs intact so restoring never overwrites later reassignment."""
    with _write_lock:
        data = read_data()
        target = entity_by_id(data, collection, entity_id, include_deleted=True)
        if collection == "milestones" and not deleted:
            entity_by_id(data, "projects", target["project_id"])
        stamp = timestamp()
        target["deleted_at"] = stamp if deleted else None
        stamp_fields(target, ["deleted_at"], stamp)
        _atomic_write_todos(data)
    schedule_sync()
    return target


def reorder_milestones(item):
    ids = item.get("ids")
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids))):
        raise ValueError("ids must contain unique milestone IDs")
    with _write_lock:
        data = read_data()
        project = entity_by_id(data, "projects", item.get("project_id"))
        selected = [entity_by_id(data, "milestones", i) for i in ids]
        if any(m["project_id"] != project["id"] for m in selected):
            raise ValueError("只能在同一项目内调整里程碑顺序")
        # Preserve a concurrently-added sibling after the caller's known items.
        remaining = sorted((m for m in data["milestones"]
                            if m["project_id"] == project["id"] and not m.get("deleted_at")
                            and m["id"] not in ids), key=lambda m: m.get("sort_order", 0))
        stamp = timestamp()
        for order, milestone in enumerate(selected + remaining):
            milestone["sort_order"] = order
            stamp_fields(milestone, ["sort_order"], stamp)
        _atomic_write_todos(data)
    schedule_sync()
    return {"ok": True, "revision": data["revision"]}


def replace_snapshot(payload):
    """Versioned compatibility endpoint; metadata can only change via typed APIs."""
    with _write_lock:
        current = read_data()
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != current["revision"]:
            raise ConflictError("数据已更新，请刷新后重试；旧版整份保存已停用")
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("tasks must be an array")
        for collection in ("projects", "milestones"):
            if collection in payload and payload[collection] != current[collection]:
                raise ValueError("项目和里程碑请使用单条保存接口")
        for collection in ("people", "weekly_report_groups"):
            if collection in payload and payload[collection] != current[collection]:
                raise ValueError("人员和周报接收群请使用配置接口")
        seen = set()
        by_id = {t["id"]: t for t in current["tasks"]}
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                raise ValueError("each task requires a string ID")
            task_id = task["id"]
            if task_id in seen:
                raise ValueError("duplicate task ID")
            seen.add(task_id)
            if task_id not in by_id:
                raise ValueError("新增事项请使用单条保存接口")
            target = by_id[task_id]
            if not target.get("deleted_at"):
                apply_task_patch(current, target, task)
        # Old list removal is translated to tombstones, never physical deletion.
        for task_id, target in by_id.items():
            if task_id not in seen and not target.get("deleted_at"):
                target["deleted_at"] = timestamp()
                stamp_fields(target, ["deleted_at"], target["deleted_at"])
        _atomic_write_todos(current)
    schedule_sync()
    return {"ok": True, "updated": current["updated"], "revision": current["revision"]}


# ============== 重复任务 ==============
# 数据模型：
#   种子 task   = 普通 task 加 `recurring: "daily"` 字段（不参与 done 状态）
#   今日实例    = task.id = f"recur:{seed.id}:{YYYY-MM-DD}"，带 `recur_source: seed.id`
# 触发：
#   1) server 启动时跑一次
#   2) APScheduler 每天 00:01 (Asia/Shanghai) 跑一次
# 幂等：state.last_generated[seed.id] + tasks 里同 id 二次去重，挡 iCloud 并发


def schedule_matches(seed: dict, run_date: date) -> bool:
    """Return whether a recurring seed should generate on run_date."""
    recurring = seed.get("recurring")
    if recurring == "daily":
        return True
    if recurring == "weekly":
        value = seed.get("recur_weekday")
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value == run_date.isoweekday()
        )
    if recurring == "monthly":
        value = seed.get("recur_monthday")
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value == run_date.day
        )
    return False


def ensure_recurring_today() -> bool:
    """检查所有种子，生成今日缺失的实例 + 自动 skip 过期未完成实例。返回是否有写入。"""
    today = today_str()
    run_date = date.fromisoformat(today)
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
        now_iso = timestamp()

        for t in tasks:
            if t.get("deleted_at") or not t.get("recur_source") or t.get("done"):
                continue
            inst_date = (t.get("id") or "").rsplit(":", 1)[-1]
            if len(inst_date) == 10 and inst_date < today:
                t["done"] = True
                t["done_at"] = now_iso
                t["auto_skipped"] = True
                t["skipped_for"] = inst_date
                stamp_fields(t, ["done", "done_at", "auto_skipped", "skipped_for"], now_iso)
                skipped += 1

        seeds = [
            t for t in tasks
            if t.get("recurring") in {"daily", "weekly", "monthly"}
            and not t.get("deleted_at") and not t.get("recur_source")
        ]
        if not seeds and skipped == 0:
            write_state(state)
            return False

        existing_ids = {t.get("id") for t in tasks}

        for seed in seeds:
            if not schedule_matches(seed, run_date):
                continue
            seed_id = seed.get("id")
            if not seed_id:
                continue
            instance_id = f"recur:{seed_id}:{today}"
            if last_gen.get(seed_id) == today and instance_id in existing_ids:
                continue
            if instance_id in existing_ids:
                last_gen[seed_id] = today
                continue
            instance = {
                "id": instance_id,
                "text": seed.get("text", ""),
                "done": False,
                "created": now_iso,
                "done_at": None,
                "source": "recur",
                "tag": seed.get("tag", "personal"),
                "priority": seed.get("priority", "P1"),
                "recur_source": seed_id,
                "owner": seed.get("owner") or "Ocean",
            }
            stamp_fields(instance, list(instance), now_iso)
            tasks.append(instance)
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
    if os.environ.get("TODO_DISABLE_SCHEDULER") == "1":
        return
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
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def _json(self, body, code=200):
        self._send(code, json.dumps(body, ensure_ascii=False))

    def _validate_request(self, mutating=False):
        # Native webview and browser pages are both served from this loopback port.
        # Reject DNS-rebinding Host values, foreign browser origins, and simple
        # cross-origin text/plain POSTs; command-line JSON clients need no Origin.
        hosts = {"localhost", "127.0.0.1", "::1"}
        try:
            host = urlsplit("//" + self.headers.get("Host", ""))
            if (host.hostname not in hosts or host.port != self.server.server_port
                    or host.username or host.password or host.path or host.query or host.fragment):
                raise RequestOriginError("请求 Host 必须是当前本地服务")
            origin = self.headers.get("Origin")
            if origin is not None:
                parsed = urlsplit(origin)
                if (parsed.scheme != "http" or parsed.hostname not in hosts
                        or parsed.port != self.server.server_port or parsed.username
                        or parsed.password or parsed.path or parsed.query or parsed.fragment):
                    raise RequestOriginError("不允许其他网页访问本地待办服务")
        except ValueError as exc:
            if isinstance(exc, RequestOriginError):
                raise
            raise RequestOriginError("无效的本地请求来源") from None
        if mutating:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                raise ValueError("无效的 Content-Length") from None
            if length < 0 or self.headers.get("Transfer-Encoding"):
                raise ValueError("不支持此请求正文格式")
            if length and self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("写入请求正文必须是 application/json")

    def _payload(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 2_000_000:
                raise ValueError("request body too large")
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求需要有效 JSON 对象") from None
        if not isinstance(payload, dict):
            raise ValueError("请求需要 JSON 对象")
        return payload

    def _entity_response(self, collection, entity):
        self._json({"ok": True, {"projects": "project", "milestones": "milestone",
                                "tasks": "task"}[collection]: entity,
                    "revision": read_data()["revision"]})

    def _error(self, exc):
        code = 403 if isinstance(exc, RequestOriginError) else (409 if isinstance(exc, ConflictError)
               else (400 if isinstance(exc, ValueError) else (404 if isinstance(exc, KeyError) else 500)))
        self._json({"error": str(exc)}, code)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            self._validate_request()
            if path in ("/", "/index.html"):
                with open(HTML_FILE, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html")
            elif path in ("/project-ui.js", "/project-ui.css"):
                with open(get_resource_path(path[1:]), encoding="utf-8") as f:
                    self._send(200, f.read(), "application/javascript" if path.endswith(".js") else "text/css")
            elif path == "/todos":
                self._json(read_data())
            elif path == "/health":
                self._json({"ok": True, "app": "daily-todo", "version": VERSION,
                            "schema": 2, "channel": RELEASE_CHANNEL,
                            "preview": os.environ.get("TODO_PREVIEW") == "1",
                            "data_file": os.path.abspath(DATA_FILE),
                            "revision": read_data()["revision"],
                            "sync_enabled": os.environ.get("TODO_DISABLE_SYNC") != "1"})
            elif path == "/version":
                self._json({"version": VERSION, "repo": GITHUB_REPO,
                            "channel": RELEASE_CHANNEL,
                            "preview": os.environ.get("TODO_PREVIEW") == "1"})
            elif path in ("/projects", "/milestones"):
                data = read_data()
                self._json({path[1:]: data[path[1:]], "revision": data["revision"]})
            elif re.fullmatch(r"/(projects|milestones)/[^/]+", path):
                collection, entity_id = path.strip("/").split("/")
                self._entity_response(collection, entity_by_id(
                    read_data(), collection, unquote(entity_id), include_deleted=True))
            elif path == "/update-progress":
                self._json(get_progress())
            elif path == "/check-update":
                if RELEASE_CHANNEL != "stable" or os.environ.get("TODO_DISABLE_UPDATE") == "1":
                    self._json({"current": VERSION, "channel": RELEASE_CHANNEL,
                                "error": "项目跟进版本不使用公开版本更新，请通过本地开发入口升级"})
                    return
                try:
                    rel = fetch_latest_release()
                    self._json({"current": VERSION,
                                "latest": (rel.get("tag_name") or "").lstrip("v"),
                                "html_url": rel.get("html_url"),
                                "assets": [{"name": a.get("name"), "url": a.get("browser_download_url")}
                                           for a in rel.get("assets", [])],
                                "has_token": bool(read_gh_token())})
                except Exception as exc:
                    self._json({"current": VERSION, "error": str(exc),
                                "has_token": bool(read_gh_token())})
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError:
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            self._validate_request(mutating=True)
            if path == "/todos/reorder":
                self._json(reorder_tasks(self._payload()))
            elif path == "/todos":
                self._json(replace_snapshot(self._payload()))
            elif path == "/todos/add":
                task, deduped = add_single_task(self._payload())
                self._json({"ok": True, "deduped": deduped, "task": task,
                            "revision": read_data()["revision"]})
            elif path == "/projects/add":
                self._entity_response("projects", add_project(self._payload()))
            elif path == "/people/upsert":
                self._json(upsert_people(self._payload()))
            elif path == "/weekly-report/groups":
                self._json(update_weekly_report_groups(self._payload()))
            elif path == "/milestones/add":
                self._entity_response("milestones", add_milestone(self._payload()))
            elif path == "/milestones/reorder":
                self._json(reorder_milestones(self._payload()))
            elif re.fullmatch(r"/(projects|milestones)/[^/]+/restore", path):
                collection, entity_id, _ = path.strip("/").split("/")
                self._entity_response(collection, set_deleted(collection, unquote(entity_id), False))
            elif path == "/recurring/add":
                task, deduped = add_recurring_seed(self._payload())
                self._json({"ok": True, "deduped": deduped, "task": task,
                            "revision": read_data()["revision"]})
            elif path == "/ensure-recurring":
                added = ensure_recurring_today()
                self._json({"ok": True, "added": added, "revision": read_data()["revision"]})
            elif path == "/install-update":
                self._json(install_update())
            elif path == "/sync-now":
                if os.environ.get("TODO_DISABLE_SYNC") == "1":
                    self._json({"ok": False, "status": "当前隔离预览未启用同步"})
                    return
                sync_sh = os.path.join(DATA_DIR, "sync.sh")
                status_file = os.environ.get("TODO_SYNC_STATUS_FILE", "/tmp/daily-todo-sync.status")
                if not os.path.exists(sync_sh):
                    self._json({"ok": False, "status": "sync.sh 不存在"})
                    return
                try:
                    os.remove(status_file)
                except OSError:
                    pass
                try:
                    result = subprocess.run(["/bin/bash", sync_sh], cwd=DATA_DIR, timeout=120,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    status = ""
                    try:
                        with open(status_file, encoding="utf-8") as f:
                            status = f.read().strip()
                    except OSError:
                        pass
                    self._json({"ok": result.returncode == 0 and status.startswith("ok"),
                                "status": status or f"同步未返回状态（退出码 {result.returncode}）"})
                except subprocess.TimeoutExpired:
                    self._json({"ok": False, "status": "同步超时（120 秒），请稍后重试"})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._error(exc)

    def do_PATCH(self):
        path = urlsplit(self.path).path
        try:
            self._validate_request(mutating=True)
            if re.fullmatch(r"/(todos|projects|milestones)/[^/]+", path):
                collection, entity_id = path.strip("/").split("/")
                collection = "tasks" if collection == "todos" else collection
                patch = self._payload()
                entity = (patch_task(unquote(entity_id), patch) if collection == "tasks" else
                          patch_entity(collection, unquote(entity_id), patch))
                self._entity_response(collection, entity)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._error(exc)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        try:
            self._validate_request(mutating=True)
            if re.fullmatch(r"/(todos|projects|milestones)/[^/]+", path):
                collection, entity_id = path.strip("/").split("/")
                collection = "tasks" if collection == "todos" else collection
                self._entity_response(collection, set_deleted(collection, unquote(entity_id)))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._error(exc)

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
