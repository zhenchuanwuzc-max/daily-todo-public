#!/usr/bin/env python3
"""Start the project branch against a local, non-syncing copy of real data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("TODO_PREVIEW_PORT", "8797"))
PREVIEW = Path.home() / "Library/Application Support/daily-todo-preview"
URL = f"http://127.0.0.1:{PORT}"


def health():
    try:
        with urllib.request.urlopen(URL + "/health", timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def main():
    existing = health()
    if existing:
        if existing.get("channel") != "project-preview":
            raise SystemExit(f"Port {PORT} belongs to another application.")
        print(URL)
        return
    data = PREVIEW / "data"
    data.mkdir(parents=True, exist_ok=True)
    target = data / "todos.json"
    if not target.exists():
        source = Path.home() / "daily-todo-data/todos.json"
        if source.exists():
            # A copy only. It has no Git repository or synchronization script.
            shutil.copy2(source, target)
        else:
            target.write_text('{"tasks": [], "projects": [], "milestones": []}', encoding="utf-8")
    env = dict(os.environ, TODO_PORT=str(PORT), TODO_DATA_DIR=str(data),
               TODO_APP_SUPPORT_DIR=str(PREVIEW / "state"),
               TODO_DISABLE_SYNC="1", TODO_DISABLE_SCHEDULER="1",
               TODO_PREVIEW="1")
    with (PREVIEW / "server.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                                   env=env, cwd=ROOT, stdout=log, stderr=log,
                                   start_new_session=True)
    for _ in range(40):
        if health():
            print(URL)
            return
        if process.poll() is not None:
            break
        time.sleep(.15)
    raise SystemExit(f"Preview did not start. See {PREVIEW / 'server.log'}")


if __name__ == "__main__":
    main()
