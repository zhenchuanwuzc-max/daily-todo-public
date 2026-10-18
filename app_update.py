"""Local app replacement run by an independent launchd job from a saved bundle.

This module never rewrites todos.json. A failed new runtime restores application,
service configuration and sync helpers while retaining any subsequent user edits.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

SYNC_FILES = ('sync.sh', 'sync-helper.py', 'json-merge.py')
LABELS = ('com.ocean.daily-todo-server', 'com.ocean.daily-todo-sync')


def validate_bundle(app, version):
    app = Path(app)
    if not app.is_dir() or app.is_symlink():
        raise ValueError('Invalid application bundle')
    with (app / 'Contents/Info.plist').open('rb') as handle:
        info = plistlib.load(handle)
    if info.get('CFBundleIdentifier') != 'com.ocean.dailytodo' or info.get('CFBundleShortVersionString') != version:
        raise ValueError('Unexpected application identity or version')


class NativeSystem:
    def running_labels(self):
        from runtime_upgrade import NativeSystem as RuntimeSystem
        native = RuntimeSystem()
        return {label for label in LABELS if native.job(label)}

    def verify_signature(self, app):
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True, capture_output=True)

    def stop(self, plan):
        from runtime_upgrade import NativeSystem as RuntimeSystem, read_configuration, trusted_server_arguments
        native = RuntimeSystem()
        app, home = Path(plan['app']), Path(plan['home'])
        plist = home / 'Library/LaunchAgents/com.ocean.daily-todo-server.plist'
        config = read_configuration(plist) or {}
        job = native.job(LABELS[0])
        listeners = native.listener_pids(plan['port'])
        allowed = set()
        if job:
            args = config.get('ProgramArguments', [])
            if not trusted_server_arguments(args, app, home) or job.get('arguments') != args:
                raise RuntimeError('Unknown server job; update stopped before changing files')
            if job.get('pid'):
                allowed.add(job['pid'])
        # Embedded-server app or a source server explicitly initiating this update.
        parent = int(plan.get('server_pid', -1))
        if parent > 0:
            command = subprocess.run(['ps', '-p', str(parent), '-o', 'command='], capture_output=True, text=True).stdout.strip()
            executable = str(app / 'Contents/MacOS/每日待办')
            source = plan.get('server_source', '')
            if command == executable or command == executable + ' --server-only' or (source and command.endswith(' ' + source)):
                allowed.add(parent)
        if listeners - allowed:
            raise RuntimeError('Unknown listener; update left the application unchanged')
        sync_path = home / 'Library/LaunchAgents/com.ocean.daily-todo-sync.plist'
        sync_job = native.job(LABELS[1])
        if sync_job:
            sync_config = read_configuration(sync_path) or {}
            sync_args = sync_config.get('ProgramArguments', [])
            expected = str(Path(plan['data_dir']) / 'sync.sh')
            if sync_args not in ([expected], ['/bin/bash', expected]) or sync_job.get('arguments') != sync_args:
                raise RuntimeError('Unknown sync job; update stopped before changing files')
        if job:
            native.stop(LABELS[0])
        output = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
        for line in output.splitlines():
            pid, _, command = line.strip().partition(' ')
            if command.strip() == str(app / 'Contents/MacOS/每日待办'):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if parent in listeners and not job:
            try:
                os.kill(parent, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(60):
            if not native.listener_pids(plan['port']):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Previous server did not stop')
        # Never interrupt a detached Git rebase. The new helpers are installed
        # only after the old sync process has finished its bounded operation.
        for _ in range(120):
            if not native.active_sync(Path(plan['data_dir'])):
                if sync_job:
                    native.stop(LABELS[1])
                return
            time.sleep(1)
        raise RuntimeError('Previous data sync has not finished; files unchanged')

    def open(self, app):
        env = dict(os.environ)
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONEXECUTABLE', 'RESOURCEPATH', 'ARGVZERO'):
            env.pop(key, None)
        subprocess.run(['open', '-a', str(app)], check=True, env=env)

    def health(self, port):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                return json.load(response)
        except (OSError, ValueError):
            return {}

    def sleep(self, seconds):
        time.sleep(seconds)

    def resume(self, plist):
        from runtime_upgrade import NativeSystem as RuntimeSystem, read_configuration
        config = read_configuration(plist)
        native = RuntimeSystem()
        job = native.job(config['Label'])
        if job:
            if job.get('arguments') != config.get('ProgramArguments'):
                raise RuntimeError('Another service is loaded; cannot restore previous job')
            return
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)],
                       check=True, capture_output=True)

    def assert_quiet(self, plan):
        from runtime_upgrade import NativeSystem as RuntimeSystem
        native = RuntimeSystem()
        if native.listener_pids(plan['port']) or native.active_sync(Path(plan['data_dir'])):
            raise RuntimeError('A service or sync restarted; update files remain protected')


@contextmanager
def data_locks(data, system, plan):
    handles = []
    try:
        for name in ('.sync.lock', '.data.lock'):
            handle = (data / name).open('a+')
            handles.append(handle)
            for _ in range(240):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    system.sleep(.5)
            else:
                raise RuntimeError('Local sync is still busy; update paused')
        system.assert_quiet(plan)
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


@contextmanager
def runtime_settled(home, system):
    # The GUI retains this lock through migration, receipt and rollback. The
    # server becoming healthy alone does not prove that transaction committed.
    from runtime_upgrade import _locks
    support = home / 'Library/Application Support/daily-todo'
    support.mkdir(parents=True, exist_ok=True)
    with _locks([support / '.runtime-upgrade.lock'], system, lambda _: None):
        yield


def matches_health(health, plan):
    return (health.get('ok') is True and health.get('app') == 'daily-todo'
            and health.get('version') == plan['version'] and health.get('channel') == 'stable'
            and health.get('preview') is False
            and os.path.realpath(health.get('data_file', '')) == os.path.realpath(str(Path(plan['data_dir']) / 'todos.json')))


def apply_update(plan, *, system=None, attempts=180):
    system = system or NativeSystem()
    app, staged, backup = (Path(plan[key]) for key in ('app', 'staged_app', 'backup_app'))
    validate_bundle(staged, plan['version'])
    system.verify_signature(staged)
    if not backup.is_dir() or app == backup or app == staged:
        raise ValueError('A separate saved application is required')
    home, data = Path(plan['home']), Path(plan['data_dir'])
    # Reject malformed data before stopping an otherwise working app.
    with (data / 'todos.json').open() as handle:
        content = json.load(handle)
    if not isinstance(content, dict) or not isinstance(content.get('tasks'), list):
        raise ValueError('Invalid existing task data')
    snapshot = backup.parent / 'configuration'
    snapshot.mkdir(exist_ok=True)
    paths = [home / 'Library/LaunchAgents' / (label + '.plist') for label in LABELS]
    paths += [data / name for name in SYNC_FILES]
    originals, modes = {}, {}
    index_entries = None
    loaded = system.running_labels()
    displaced = backup.parent / 'previous-installed.app'
    replaced = False
    stopping_started = False
    try:
        stopping_started = True
        system.stop(plan)
        with data_locks(data, system, plan):
            originals = {str(path): path.read_bytes() if path.exists() else None for path in paths}
            modes = {str(path): path.stat().st_mode & 0o777 for path in paths if path.exists()}
            for index, (path, body) in enumerate(originals.items()):
                if body is not None:
                    (snapshot / str(index)).write_bytes(body)
            (snapshot / 'manifest.json').write_text(json.dumps({p: b is not None for p, b in originals.items()}))
            (snapshot / 'todos.json').write_bytes((data / 'todos.json').read_bytes())
            if (data / '.git').exists():
                index_entries = subprocess.check_output(['git', '-C', str(data), 'ls-files', '--stage', '-z', '--', *SYNC_FILES])
                (snapshot / 'sync-index.entries').write_bytes(index_entries)
            if app.exists():
                app.rename(displaced)
            staged.rename(app)
            replaced = True
        system.open(app)
        for _ in range(attempts):
            health = system.health(plan['port'])
            if matches_health(health, plan):
                with runtime_settled(home, system):
                    health = system.health(plan['port'])
                    if not matches_health(health, plan):
                        raise RuntimeError('Runtime migration did not commit successfully')
                    if LABELS[1] in loaded and paths[1].exists():
                        system.resume(paths[1])
                    receipt = {'status': 'installed', 'version': plan['version'], 'backup': str(backup), 'health': health}
                    (backup.parent / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
                    return receipt
            system.sleep(.5)
        raise RuntimeError('New application did not become healthy')
    except Exception as exc:
        rollback_errors = []
        try:
            # Unknown listeners are preserved even when this prevents rollback.
            if replaced:
                system.stop(plan)
            with data_locks(data, system, plan):
                if replaced and app.exists():
                    app.rename(backup.parent / 'failed-new.app')
                if displaced.exists():
                    displaced.rename(app)
                elif not app.exists():
                    shutil.copytree(backup, app, symlinks=True)
                for path_string, body in (originals.items() if replaced else ()):
                    path = Path(path_string)
                    if body is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(body)
                        path.chmod(modes[path_string])
                if replaced and index_entries is not None:
                    subprocess.run(['git', '-C', str(data), 'update-index', '--force-remove', '--', *SYNC_FILES], check=True)
                    subprocess.run(['git', '-C', str(data), 'update-index', '-z', '--index-info'], input=index_entries, check=True)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        for label, path in zip(LABELS, paths[:2]):
            if label in loaded and path.exists():
                try:
                    system.resume(path)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
        if stopping_started and app.exists() and not rollback_errors:
            try:
                system.open(app)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        message = (f'Update failed; rollback needs attention: {rollback_errors}; original error: {exc}'
                   if rollback_errors else f'Update failed and application/configuration rolled back: {exc}')
        try:
            (backup.parent / 'error.txt').write_text(message)
        except OSError:
            pass
        raise RuntimeError(message) from exc


def run_plan(plan_path):
    plan_path = Path(plan_path)
    try:
        plan = json.loads(plan_path.read_text())
        (plan_path.parent / 'started.json').write_text(json.dumps({'pid': os.getpid()}))
        result = apply_update(plan)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        (plan_path.parent / 'error.txt').write_text(str(exc))
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(run_plan(sys.argv[1]))
