"""Offline, reversible first-run migration from source to the installed bundle.

Only process/launchd boundaries live in NativeSystem. File backups, locking and
Git index updates remain real so the same transaction is testable in a temp home.
No network sync, remote configuration or todos rewrite belongs in this module.
"""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import urllib.request
import uuid


SERVER_LABEL = "com.ocean.daily-todo-server"
SYNC_LABEL = "com.ocean.daily-todo-sync"
HELPERS = ("sync.sh", "sync-helper.py", "json-merge.py")


class NativeSystem:
    def __init__(self):
        self.domain = f"gui/{os.getuid()}"

    @staticmethod
    def _run(*args):
        return subprocess.run(args, capture_output=True, text=True, timeout=15)

    def job(self, label):
        result = self._run("/bin/launchctl", "print", f"{self.domain}/{label}")
        if result.returncode:
            if "Could not find service" in result.stderr or "Could not find specified service" in result.stderr:
                return None
            raise RuntimeError("无法核验本机待办服务，请稍后重试。")
        match = re.search(r"^\s*arguments = \{\n(.*?)^\s*\}", result.stdout, re.M | re.S)
        arguments = [line.strip() for line in match.group(1).splitlines()] if match else []
        pid = re.search(r"^\s*pid = (\d+)$", result.stdout, re.M)
        return {"pid": int(pid.group(1)) if pid else None, "arguments": arguments}

    def listener_pids(self, port):
        result = self._run("/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t")
        if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr.strip()):
            raise RuntimeError("无法核验本地端口，升级已暂停。")
        return {int(line) for line in result.stdout.splitlines() if line.isdigit()}

    def health(self, port):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                return json.load(response)
        except (OSError, ValueError):
            return None

    def stop(self, label):
        result = self._run("/bin/launchctl", "bootout", f"{self.domain}/{label}")
        if result.returncode and self.job(label) is not None:
            raise RuntimeError("旧待办服务未能停止，未继续升级。")

    def start(self, path):
        result = self._run("/bin/launchctl", "bootstrap", self.domain, str(path))
        if result.returncode:
            raise RuntimeError("待办后台服务未能启动，请检查本机服务日志。")

    def active_sync(self, data):
        # Older releases detached sync/git without locks. Wait, never kill a
        # possible rebase. Unknown processes are not signalled by this module.
        result = self._run("/bin/ps", "-axo", "pid=,comm=,args=")
        if result.returncode:
            raise RuntimeError("无法确认旧同步已退出，升级已暂停。")
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            pid, executable, command = parts
            if any(str(data / name) in command for name in HELPERS):
                return True
            if Path(executable).name in ("git", "ssh"):
                cwd = self._run("/usr/sbin/lsof", "-a", "-p", pid, "-d", "cwd", "-Fn")
                if "n" + str(data) in cwd.stdout.splitlines():
                    return True
        return False

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)


def read_configuration(path):
    if path.is_symlink():
        raise RuntimeError("服务配置是符号链接，无法安全自动升级。")
    if not path.exists():
        return None
    with path.open("rb") as handle:
        value = plistlib.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError("待办服务配置格式无效。")
    args = value.get("ProgramArguments", [])
    if value.get("Program") and (not args or Path(value["Program"]).resolve() != Path(args[0]).resolve()):
        raise RuntimeError("服务的实际程序与参数不一致，未修改配置。")
    return value


def _inside(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def installed_app(app_path, home=None):
    """Reject development/dist/preview bundles and unrelated applications."""
    home = Path(home or Path.home()).resolve()
    app = Path(app_path).resolve()
    if app not in ((home / "Applications/每日待办.app").resolve(), Path("/Applications/每日待办.app")):
        return False
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        return info.get("CFBundleIdentifier") == "com.ocean.dailytodo"
    except (OSError, ValueError):
        return False


def _binary(app):
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    name = info.get("CFBundleExecutable", "每日待办")
    if not isinstance(name, str) or Path(name).name != name:
        raise RuntimeError("App 可执行文件信息无效。")
    binary = app / "Contents/MacOS" / name
    if not binary.is_file():
        raise RuntimeError("App 可执行文件缺失。")
    return binary


def trusted_server_arguments(arguments, app_path, home):
    """Require the installed executable or an owned, recognizable source file."""
    if arguments == [str(_binary(Path(app_path))), "--server-only"]:
        return True
    if (not isinstance(arguments, list) or len(arguments) != 2
            or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(arguments[0]).name)):
        return False
    source = Path(arguments[1]).expanduser()
    if (source.name != "server.py" or not source.is_absolute() or not _inside(source, Path(home))
            or not source.is_file() or source.stat().st_uid != os.getuid()
            or source.stat().st_mode & 0o022):
        return False
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return all(token in text for token in ("daily-todo", "def serve_forever", "class Handler", "DATA_FILE"))


def _expanded(value, home):
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    return Path(value).resolve()


def select_data_dir(server_config, sync_config, default_data_dir, home):
    for config in (server_config, sync_config):
        env = (config or {}).get("EnvironmentVariables", {})
        value = env.get("TODO_DATA_DIR")
        if value:
            return _expanded(value, home)
    return _expanded(str(default_data_dir), home)


def _healthy(health, data, version, channel):
    return (isinstance(health, dict) and health.get("ok") is True and health.get("app") == "daily-todo"
            and health.get("version") == version and health.get("channel") == channel
            and health.get("preview") is False and isinstance(health.get("data_file"), str)
            and Path(health["data_file"]).resolve() == data / "todos.json")


def _git(data, *args, input=None):
    result = subprocess.run(["/usr/bin/git", "-C", str(data), *args], input=input,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("本地数据仓库操作失败，升级未完成。")
    return result.stdout


def _validate_data(data):
    try:
        existing = json.loads((data / "todos.json").read_text(encoding="utf-8"))
        if (not isinstance(existing, dict) or not isinstance(existing.get("tasks"), list)
                or any(name in existing and not isinstance(existing[name], list)
                       for name in ("projects", "milestones", "project_templates"))):
            raise ValueError("invalid task collections")
    except (ValueError, OSError) as error:
        raise RuntimeError("原有待办数据格式无效，未安装新版组件；原数据已保留。") from error


def _atomic_bytes(path, content, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".daily-todo-upgrade-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _locks(paths, system, progress):
    handles = []
    try:
        for path in paths:
            handle = path.open("a+")
            handles.append(handle)
            for _ in range(180):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    progress("等待本机同步或编辑完成，数据会保留…")
                    system.sleep(.5)
            else:
                raise RuntimeError("本机同步仍在进行，请稍后重新打开每日待办。")
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


def _wait_quiet(system, data, port, progress):
    progress("正在等待旧版服务与同步完成…")
    for _ in range(90):
        if not system.listener_pids(port) and not system.active_sync(data):
            return
        system.sleep(1)
    raise RuntimeError("旧服务或同步尚未退出，升级已暂停；原数据保留。")


def _wait_health(system, data, version, channel, port):
    for _ in range(60):
        health = system.health(port)
        if _healthy(health, data, version, channel):
            return health
        system.sleep(.2)
    raise RuntimeError("新版服务健康验证失败，正在恢复原有配置。")


def _validate_jobs(system, configs, app, home, data, port):
    jobs = {}
    for label, config in configs.items():
        job = system.job(label)
        if config is None:
            if job is not None:
                raise RuntimeError("存在无法核验配置的本机服务，升级已暂停。")
            continue
        args = config.get("ProgramArguments")
        trusted = (trusted_server_arguments(args, app, home) if label == SERVER_LABEL else
                   isinstance(args, list) and len(args) in (1, 2)
                   and (len(args) == 1 or args[0] == "/bin/bash")
                   and Path(args[-1]).is_absolute() and Path(args[-1]).resolve() == data / "sync.sh")
        if config.get("Label") != label or not trusted:
            raise RuntimeError("现有服务路径无法确认属于每日待办，未修改配置。")
        if job is not None and job.get("arguments") != args:
            raise RuntimeError("运行中的服务与配置不一致，未停止该服务。")
        if job is not None:
            jobs[label] = job
    listeners = system.listener_pids(port)
    known_pid = jobs.get(SERVER_LABEL, {}).get("pid")
    if listeners and listeners != {known_pid}:
        raise RuntimeError("本地端口被无法确认的服务占用；未停止进程或修改配置。")
    return jobs


def prepare_runtime(*, app_path, default_data_dir, version, channel="stable", port=8766,
                    home=None, progress=None, system=None):
    """Return data_dir/health/migrated/backup_dir after an offline transaction.

    Call from a worker, never the AppKit thread. Only an installed stable bundle
    can install launchd. The old application itself is not recoverable here;
    the updater owns bundle backup/replacement and its separate rollback.
    """
    home = Path(home or Path.home()).resolve()
    app = Path(app_path).resolve()
    system = system or NativeSystem()
    callback = progress
    def progress(message):
        # Closing a native progress view must not interrupt a transaction or
        # prevent its rollback; progress is informational, never a commit gate.
        if callback:
            try:
                callback(message)
            except Exception:
                pass
    if channel != "stable" or port != 8766 or not installed_app(app, home):
        raise RuntimeError("只有已安装的正式版 App 可以迁移本机后台服务。")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleShortVersionString") != version:
        raise RuntimeError("App 版本信息不一致，未开始升级。")
    support = home / "Library/Application Support/daily-todo"
    support.mkdir(parents=True, exist_ok=True)
    with _locks([support / ".runtime-upgrade.lock"], system, progress):
        return _prepare_locked(app, home, Path(default_data_dir), version, channel, port, system, progress, support)


def _prepare_locked(app, home, default_data, version, channel, port, system, progress, support):
    paths = {label: home / "Library/LaunchAgents" / (label + ".plist")
             for label in (SERVER_LABEL, SYNC_LABEL)}
    configs = {label: read_configuration(path) for label, path in paths.items()}
    data = select_data_dir(configs[SERVER_LABEL], configs[SYNC_LABEL], default_data, home)
    if not (data / "todos.json").is_file():
        raise RuntimeError("原有待办数据目录不存在，未创建或覆盖数据。")
    _validate_data(data)
    sources = {name: app / "Contents/Resources/sync" / name for name in HELPERS}
    if any(not path.is_file() for path in sources.values()):
        raise RuntimeError("App 缺少同步组件，请重新安装正式版。")
    if any((data / name).is_symlink() for name in (*HELPERS, ".sync.lock", ".data.lock")):
        raise RuntimeError("同步组件或锁文件是符号链接，无法安全自动升级。")
    for config in configs.values():
        env = (config or {}).get("EnvironmentVariables", {})
        if env.get("TODO_PREVIEW") == "1" or str(env.get("TODO_PORT", "8766")) != "8766":
            raise RuntimeError("现有配置属于预览服务，未自动迁移。")
    jobs = _validate_jobs(system, configs, app, home, data, port)
    config = configs[SERVER_LABEL] or {}
    wanted_args = [str(_binary(app)), "--server-only"]
    current = (config.get("ProgramArguments") == wanted_args and config.get("DailyTodoRuntimeVersion") == version
               and all((data / name).is_file() for name in HELPERS))
    if current and _healthy(system.health(port), data, version, channel):
        return SimpleNamespace(data_dir=data, health=system.health(port), migrated=False, backup_dir=None)
    if current and SERVER_LABEL not in jobs and not system.listener_pids(port):
        progress("正在启动每日待办后台服务…")
        system.start(paths[SERVER_LABEL])
        return SimpleNamespace(data_dir=data, health=_wait_health(system, data, version, channel, port),
                               migrated=False, backup_dir=None)
    # Refuse unfinished user Git transactions before stopping any writer.
    git_dir = Path(_git(data, "rev-parse", "--absolute-git-dir").decode().strip())
    if any((git_dir / name).exists() for name in ("index.lock", "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD")):
        raise RuntimeError("数据仓库有未完成的 Git 操作，请完成后再升级。")
    if _git(data, "diff", "--name-only", "--diff-filter=U") or _git(data, "ls-files", "--", ".data.lock", ".sync.lock"):
        raise RuntimeError("数据仓库有冲突或已跟踪锁文件，升级已暂停。")
    stopped, backup, changed = [], None, False
    originals = {}
    index_entries = b""
    exclude = git_dir / "info/exclude"
    exclude_before = exclude.read_bytes() if exclude.exists() else None
    plist_bytes = {label: paths[label].read_bytes() if paths[label].exists() else None for label in paths}
    try:
        progress("正在准备本机升级，原有待办保持不变…")
        if SERVER_LABEL in jobs:
            stopped.append(SERVER_LABEL)
            system.stop(SERVER_LABEL)
        # bootout can terminate a sync job's Git children. Stop the producer
        # first, let its detached sync and scheduled sync finish, then unload
        # the now-idle sync job before taking the replacement locks.
        _wait_quiet(system, data, port, progress)
        if SYNC_LABEL in jobs:
            stopped.append(SYNC_LABEL)
            system.stop(SYNC_LABEL)
        _wait_quiet(system, data, port, progress)
        with _locks([data / ".sync.lock", data / ".data.lock"], system, progress):
            # Recheck port and detached old jobs after taking locks: an older
            # unlocked sync may have been launched while launchd was stopping.
            if system.listener_pids(port) or system.active_sync(data):
                raise RuntimeError("仍有本地写入进程，未开始替换同步组件。")
            _validate_data(data)
            backup = support / "runtime-backups" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
            backup.mkdir(parents=True)
            shutil.copy2(data / "todos.json", backup / "todos.json")
            for label, contents in plist_bytes.items():
                if contents is not None:
                    (backup / (label + ".plist")).write_bytes(contents)
            for name in HELPERS:
                originals[name] = (data / name).exists()
                if originals[name]:
                    shutil.copy2(data / name, backup / name)
            index_entries = _git(data, "ls-files", "--stage", "-z", "--", *HELPERS)
            (backup / "sync-index.entries").write_bytes(index_entries)
            if (git_dir / "index").exists():
                shutil.copy2(git_dir / "index", backup / "git-index.backup")
            if exclude_before is not None:
                (backup / "git-exclude.backup").write_bytes(exclude_before)
            progress("正在安装项目跟进组件…")
            changed = True
            for name, source in sources.items():
                _atomic_bytes(data / name, source.read_bytes(), 0o755)
            _git(data, "add", "--", *HELPERS)
            exclusions = (exclude_before or b"").decode("utf-8")
            missing = [name for name in (".data.lock", ".sync.lock", "__pycache__/") if name not in exclusions.splitlines()]
            if missing:
                _atomic_bytes(exclude, (exclusions + "\n" + "\n".join(missing) + "\n").encode())
            new_config = dict(config) if config else {"Label": SERVER_LABEL, "RunAtLoad": True,
                                                     "KeepAlive": {"SuccessfulExit": False}}
            new_config["ProgramArguments"] = wanted_args
            new_config.pop("Program", None)
            new_config["DailyTodoRuntimeVersion"] = version
            environment = dict(new_config.get("EnvironmentVariables", {}))
            environment["TODO_DATA_DIR"] = str(data)
            environment.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
            new_config["EnvironmentVariables"] = environment
            logs = home / "Library/Logs"
            logs.mkdir(parents=True, exist_ok=True)
            new_config.setdefault("StandardOutPath", str(logs / "daily-todo.out.log"))
            new_config.setdefault("StandardErrorPath", str(logs / "daily-todo.err.log"))
            _atomic_bytes(paths[SERVER_LABEL], plistlib.dumps(new_config), 0o644)
        progress("正在验证新版后台服务…")
        system.start(paths[SERVER_LABEL])
        health = _wait_health(system, data, version, channel, port)
        if SYNC_LABEL in stopped:
            system.start(paths[SYNC_LABEL])
        (backup / "receipt.json").write_text(json.dumps({"version": version, "channel": channel,
            "data_dir": str(data), "app_path": str(app), "health": health,
            "app_backup_available": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        return SimpleNamespace(data_dir=data, health=health, migrated=True, backup_dir=backup)
    except Exception as error:
        progress("升级未完成，正在恢复原有配置和同步组件…")
        rollback_errors = []
        # Only stop our newly configured job when its loaded arguments match.
        # An unrelated listener is never killed, including during rollback.
        if changed:
            try:
                job = system.job(SERVER_LABEL)
                if job and job.get("arguments") == wanted_args:
                    system.stop(SERVER_LABEL)
                _wait_quiet(system, data, port, progress)
                # A successful bootstrap followed by a receipt write failure
                # may already have restarted the periodic sync job.
                sync_job = system.job(SYNC_LABEL)
                if (SYNC_LABEL in stopped and sync_job
                        and sync_job.get("arguments") == configs[SYNC_LABEL].get("ProgramArguments")):
                    system.stop(SYNC_LABEL)
                    _wait_quiet(system, data, port, progress)
                with _locks([data / ".sync.lock", data / ".data.lock"], system, progress):
                    for name in HELPERS:
                        if originals[name]:
                            shutil.copy2(backup / name, data / name)
                        else:
                            (data / name).unlink(missing_ok=True)
                    _git(data, "update-index", "--force-remove", "--", *HELPERS)
                    _git(data, "update-index", "-z", "--index-info", input=index_entries)
                    if exclude_before is None:
                        exclude.unlink(missing_ok=True)
                    else:
                        _atomic_bytes(exclude, exclude_before)
                    for label, contents in plist_bytes.items():
                        if contents is None:
                            paths[label].unlink(missing_ok=True)
                        else:
                            _atomic_bytes(paths[label], contents, 0o644)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        for label in (SERVER_LABEL, SYNC_LABEL):
            # The previous updater may already have booted out the source
            # daemon, so restore its trusted source configuration as well.
            restore_source = (label == SERVER_LABEL and configs[label] is not None
                              and configs[label].get("ProgramArguments") != wanted_args)
            if (label in stopped or restore_source) and paths[label].exists():
                try:
                    if system.job(label) is None:
                        system.start(paths[label])
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
        if backup:
            try:
                (backup / "failure.json").write_text(json.dumps({"error": str(error), "rollback_errors": rollback_errors,
                    "todos_restored": False, "app_restored": False}, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        if rollback_errors:
            raise RuntimeError("升级未完成，部分配置恢复失败；待办数据和备份已保留。请检查本机日志。") from error
        raise
