#!/usr/bin/env python3
"""Portable fcntl locking and local-only Git transaction for daily-todo sync.

The outer .sync.lock serializes sync jobs; .data.lock is held ONLY while local
files can change. Fetch and push run without the writer lock.
"""
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
from datetime import datetime


class SyncError(Exception):
    pass


def _merge_module():
    spec = importlib.util.spec_from_file_location("todo_json_merge", Path(__file__).with_name("json-merge.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _data_dir():
    return Path(os.environ.get("TODO_DATA_DIR", Path(__file__).resolve().parent)).expanduser().resolve()


def _run(command, *, timeout=30, capture=False):
    """Kill the entire process group on timeout, not only Git's parent process."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.PIPE if capture else None, start_new_session=True)
    try:
        output, _error = process.communicate(timeout=timeout)
        return process.returncode, output or b""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        except ProcessLookupError:
            pass
        return 124, b""


def _git(*args, capture=True):
    code, output = _run(["git", *args], capture=capture)
    if code:
        raise SyncError("local Git operation failed")
    return output.decode("utf-8", errors="replace").strip()


def local_stage(remote_ref):
    """Snapshot local edits, rebase the snapshot, validate. Never uses network."""
    directory = _data_dir()
    os.chdir(directory)
    validator = _merge_module()
    with open(directory / ".data.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # Do not abort or overwrite an unrelated human Git operation.
        for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
            if Path(_git("rev-parse", "--git-path", name)).exists():
                raise SyncError("unfinished Git operation; manual resolution required")
        if _git("diff", "--name-only", "--diff-filter=U"):
            raise SyncError("unresolved Git conflicts; local data retained")
        if not _git("symbolic-ref", "--quiet", "--short", "HEAD"):
            raise SyncError("detached HEAD cannot be synced")
        try:
            validator.load(directory / "todos.json")
        except (ValueError, OSError, TypeError) as exc:
            raise SyncError("invalid local JSON; sync stopped") from exc
        # Commit dirty data BEFORE rebase, rather than autostash it. A conflicted
        # autostash can otherwise leave conflict markers after rebase reports 0.
        if _git("status", "--porcelain", "--untracked-files=no"):
            _git("add", "-u")
            _git("commit", "-m", "sync: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        snapshot_head = _git("rev-parse", "HEAD")
        code, _ = _run(["git", "rebase", remote_ref], capture=True)
        if code:
            abort_code, _ = _run(["git", "rebase", "--abort"], capture=True)
            if abort_code:
                raise SyncError("rebase failed; manual resolution required")
            raise SyncError("rebase failed; local snapshot retained")
        try:
            if _git("diff", "--name-only", "--diff-filter=U"):
                raise ValueError("unmerged paths")
            validator.load(directory / "todos.json")
        except (ValueError, OSError, TypeError, SyncError) as exc:
            # No concurrent writer can run under .data.lock. Restore the exact
            # pre-rebase committed snapshot if an invalid remote fast-forwarded.
            _git("reset", "--hard", snapshot_head)
            raise SyncError("invalid merged data; pre-sync snapshot restored") from exc


def locked_run(script):
    directory = _data_dir()
    if not directory.is_dir():
        raise SyncError("data directory does not exist")
    lock = open(directory / ".sync.lock", "a", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    os.chdir(directory)
    excluded = (".sync.lock", ".data.lock", "__pycache__/")
    if _git("ls-files", "--", ".sync.lock", ".data.lock"):
        raise SyncError("lock files must not be tracked by Git")
    exclude_path = Path(_git("rev-parse", "--git-path", "info/exclude"))
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    missing = [name for name in excluded if name not in existing.splitlines()]
    if missing:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude_path, "a", encoding="utf-8") as handle:
            handle.write("\n" + "\n".join(missing) + "\n")
    # exec replaces Python, and the inherited descriptor owns the lock until
    # Bash and its children finish, including when the launcher is interrupted.
    os.set_inheritable(lock.fileno(), True)
    path = Path(script).resolve()
    source = path.read_text(encoding="utf-8")
    # Load the entire shell program now; rebase can update sync.sh on disk.
    os.execv("/bin/bash", ["/bin/bash", "-c", source, str(path), "--lock-held"])


def main():
    args = sys.argv[1:]
    try:
        if len(args) == 2 and args[0] == "locked":
            locked_run(args[1])
        elif len(args) == 2 and args[0] == "local":
            local_stage(args[1])
        elif len(args) >= 3 and args[0] == "timeout":
            code, _ = _run(args[2:], timeout=float(args[1]))
            return code if code >= 0 else 128 - code
        else:
            raise SyncError("invalid sync helper arguments")
        return 0
    except (SyncError, OSError) as exc:
        # SyncError text is controlled; OS errors can contain private paths.
        message = str(exc) if isinstance(exc, SyncError) else "local sync operation failed"
        print(message, file=sys.stderr)
        if args and args[0] == "locked":
            try:
                Path(os.environ.get("TODO_SYNC_STATUS_FILE", "/tmp/daily-todo-sync.status")).write_text(
                    "fail: 同步准备失败（本地数据已保留）\n", encoding="utf-8")
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
