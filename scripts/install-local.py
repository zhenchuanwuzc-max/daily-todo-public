#!/usr/bin/env python3
"""Install a tested local bundle and matching private-data sync helpers, with backup.

No release is published. Real todos.json is copied to the backup, never rewritten.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
USER_DIR = Path.home()
DATA = USER_DIR / "daily-todo-data"
APP = USER_DIR / "Applications/每日待办.app"
AGENTS = USER_DIR / "Library/LaunchAgents"
SERVER_PLIST = AGENTS / "com.ocean.daily-todo-server.plist"
SYNC_PLIST = AGENTS / "com.ocean.daily-todo-sync.plist"
DOMAIN = f"gui/{os.getuid()}"


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def stop_services():
    for name in ("com.ocean.daily-todo-server", "com.ocean.daily-todo-sync"):
        run("launchctl", "bootout", f"{DOMAIN}/{name}", check=False)
    # Only stop the known daily-todo native executable, not unrelated Python apps.
    for line in run("ps", "-axo", "pid=,command=").stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if command.strip() == str(APP / "Contents/MacOS/每日待办"):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    for _ in range(40):
        result = run("lsof", "-nP", "-iTCP:8766", "-sTCP:LISTEN", "-t", check=False)
        if not result.stdout.strip():
            break
        time.sleep(.15)
    else:
        raise RuntimeError("Port 8766 remains occupied; installation stopped.")
    # The old service launches detached, unlocked sync jobs. Let them finish
    # rather than interrupting a possible Git rebase or taking a racing backup.
    for _ in range(90):
        active = []
        for line in run("ps", "-axo", "pid=,comm=,args=").stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            pid, executable, command = parts
            if str(DATA / "sync.sh") in command or str(DATA / "sync-helper.py") in command:
                active.append(pid)
            elif Path(executable).name in ("git", "ssh"):
                cwd = run("lsof", "-a", "-p", pid, "-d", "cwd", "-Fn", check=False).stdout
                if "n" + str(DATA) in cwd.splitlines():
                    active.append(pid)
        if not active:
            return
        time.sleep(1)
    raise RuntimeError("Existing data sync has not finished; original installation retained.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", help="Perform the local installation")
    args = parser.parse_args()
    bundle = ROOT / "dist/每日待办.app"
    required = [bundle / "Contents/Info.plist", DATA / "todos.json"]
    sources = list((ROOT / "sync").glob("*.py")) + list((ROOT / "sync").glob("*.sh"))
    if not sources or any(not path.exists() for path in required):
        raise SystemExit("Build the native bundle and verify the data directory first.")
    run("codesign", "--verify", "--deep", "--strict", str(bundle))
    with (bundle / "Contents/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != "com.ocean.dailytodo":
        raise SystemExit("Unexpected bundle identifier.")
    plan = {"bundle": str(bundle), "destination": str(APP),
            "version": info.get("CFBundleShortVersionString"),
            "data": str(DATA), "sync_files": [p.name for p in sources]}
    if not args.install:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    backup = USER_DIR / "Library/Application Support/daily-todo/deploy-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True)
    original = (DATA / "todos.json").read_bytes()
    (backup / "todos.json").write_bytes(original)
    for plist in (SERVER_PLIST, SYNC_PLIST):
        if plist.exists():
            shutil.copy2(plist, backup / plist.name)
    for source in sources:
        target = DATA / source.name
        if target.exists():
            shutil.copy2(target, backup / source.name)
    staged = APP.with_name("每日待办-new.app")
    if staged.exists():
        raise SystemExit(f"A staged bundle already exists: {staged}")
    shutil.copytree(bundle, staged, symlinks=True)
    installed = False
    index_changed = False
    originals = {source.name: (DATA / source.name).exists() for source in sources}
    plist_existed = {plist.name: plist.exists() for plist in (SERVER_PLIST, SYNC_PLIST)}
    index_entries = subprocess.check_output(["git", "-C", str(DATA), "ls-files", "--stage", "-z", "--", *(source.name for source in sources)])
    (backup / "sync-index.entries").write_bytes(index_entries)
    (backup / "git-before.txt").write_text(run("git", "-C", str(DATA), "rev-parse", "HEAD").stdout + run("git", "-C", str(DATA), "status", "--short").stdout)
    try:
        stop_services()
        # Take a final snapshot after the previous writers have stopped.
        original = (DATA / "todos.json").read_bytes()
        (backup / "todos.json").write_bytes(original)
        for source in sources:
            target = DATA / source.name
            originals[source.name] = target.exists()
            if target.exists():
                shutil.copy2(target, backup / source.name)
        index_entries = subprocess.check_output(["git", "-C", str(DATA), "ls-files", "--stage", "-z", "--", *(source.name for source in sources)])
        (backup / "sync-index.entries").write_bytes(index_entries)
        installed = True
        if APP.exists():
            APP.rename(backup / APP.name)
        staged.rename(APP)
        for source in sources:
            target = DATA / source.name
            shutil.copy2(source, target)
            target.chmod(0o755)
        # The next normal private-data sync must include the new helper as well
        # as updates to the two existing scripts; do not stage unrelated files.
        index_changed = True
        run("git", "-C", str(DATA), "add", "--", *(source.name for source in sources))
        exclude = DATA / ".git/info/exclude"
        current = exclude.read_text() if exclude.exists() else ""
        exclude.parent.mkdir(parents=True, exist_ok=True)
        for pattern in (".data.lock", ".sync.lock"):
            if pattern not in current.splitlines():
                current += "\n" + pattern + "\n"
        exclude.write_text(current)
        if SERVER_PLIST.exists():
            with SERVER_PLIST.open("rb") as handle:
                configuration = plistlib.load(handle)
        else:
            configuration = {"Label": "com.ocean.daily-todo-server", "RunAtLoad": True,
                             "KeepAlive": {"SuccessfulExit": False}}
        configuration["ProgramArguments"] = ["/usr/bin/python3", str(ROOT / "server.py")]
        environment = configuration.setdefault("EnvironmentVariables", {})
        environment.update(TODO_DATA_DIR=str(DATA), PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        configuration["StandardOutPath"] = str(USER_DIR / "Library/Logs/daily-todo.out.log")
        configuration["StandardErrorPath"] = str(USER_DIR / "Library/Logs/daily-todo.err.log")
        with SERVER_PLIST.open("wb") as handle:
            plistlib.dump(configuration, handle)
        run("launchctl", "bootstrap", DOMAIN, str(SERVER_PLIST))
        for _ in range(50):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8766/health", timeout=1) as response:
                    health = json.load(response)
                if (health.get("channel") == "project-preview"
                        and health.get("version") == plan["version"]
                        and health.get("data_file") == str(DATA / "todos.json")
                        and health.get("preview") is False):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        else:
            raise RuntimeError("New service did not become healthy.")
        if (DATA / "todos.json").read_bytes() != original:
            # Scheduler may legitimately update recurring instances: retain both
            # snapshots and require inspection instead of overwriting live data.
            print("Data changed after startup; original snapshot retained for comparison.")
        if SYNC_PLIST.exists():
            run("launchctl", "bootstrap", DOMAIN, str(SYNC_PLIST))
        run("open", "-a", str(APP))
        receipt = dict(plan, backup=str(backup), before_sha256=hashlib.sha256(original).hexdigest(), health=health)
        (backup / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    except Exception:
        try:
            stop_services()
        except Exception as exc:
            print(f"Rollback cleanup: {exc}")
        if (backup / APP.name).exists():
            if APP.exists():
                APP.rename(backup / "failed-new.app")
            (backup / APP.name).rename(APP)
        if installed:
            for source in sources:
                saved = backup / source.name
                if originals[source.name]:
                    shutil.copy2(saved, DATA / source.name)
                else:
                    (DATA / source.name).unlink(missing_ok=True)
            if index_changed:
                run("git", "-C", str(DATA), "update-index", "--force-remove", "--", *(source.name for source in sources))
                subprocess.run(["git", "-C", str(DATA), "update-index", "-z", "--index-info"], input=index_entries, check=True)
        for plist in (SERVER_PLIST, SYNC_PLIST):
            saved = backup / plist.name
            if saved.exists():
                shutil.copy2(saved, plist)
                run("launchctl", "bootstrap", DOMAIN, str(plist), check=False)
            elif installed and not plist_existed[plist.name]:
                plist.unlink(missing_ok=True)
        # Never roll back todos.json automatically: users may have edited it.
        run("open", "-a", str(APP), check=False)
        if staged.exists():
            shutil.rmtree(staged)
        raise


if __name__ == "__main__":
    main()
