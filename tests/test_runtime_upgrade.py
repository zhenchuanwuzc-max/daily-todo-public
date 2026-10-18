"""Real temporary file/Git migrations; only macOS process boundaries are fake."""
import importlib.util
import inspect
import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import desktop_app


ROOT = Path(__file__).resolve().parents[1]
SERVER = "com.ocean.daily-todo-server"
SYNC = "com.ocean.daily-todo-sync"
HELPERS = ("sync.sh", "sync-helper.py", "json-merge.py")


class NativeProcessEncodingTests(unittest.TestCase):
    def test_non_utf8_process_output_does_not_abort_upgrade(self):
        import sys
        import runtime_upgrade
        result = runtime_upgrade.NativeSystem._run(sys.executable, '-c',
            "import os; os.write(1, b'123 python sync\\xff\\n')")
        self.assertEqual(result.returncode, 0)
        self.assertIn('123 python sync', result.stdout)


class FakeMac:
    def __init__(self, data):
        self.data = data
        self.jobs = {}
        self.listeners = set()
        self.health_value = None
        self.stops = []
        self.starts = []
        self.pending_sync = 0
        self.on_wait = None
        self.fail_new = False
        self.write_on_failure = False

    def job(self, label):
        return self.jobs.get(label)

    def listener_pids(self, port):
        return set(self.listeners)

    def health(self, port):
        return self.health_value

    def stop(self, label):
        self.stops.append(label)
        job = self.jobs.pop(label, {})
        self.listeners.discard(job.get("pid"))
        if label == SERVER:
            self.health_value = None

    def start(self, path):
        config = plistlib.loads(path.read_bytes())
        self.starts.append(config)
        label = config["Label"]
        self.jobs[label] = {"pid": 100, "arguments": config["ProgramArguments"]}
        if label == SERVER:
            self.listeners = {100}
            is_new = "--server-only" in config["ProgramArguments"]
            self.health_value = {"ok": True, "app": "daily-todo", "version": "1.2.0" if is_new else "1.1.4",
                                 "channel": "stable" if is_new else "project-preview", "preview": False,
                                 "data_file": str(self.data / "todos.json")}
            if is_new and self.fail_new:
                self.health_value["version"] = "wrong"
                if self.write_on_failure:
                    (self.data / "todos.json").write_text('{"tasks":[{"id":"new-edit"}]}')

    def active_sync(self, data):
        if self.pending_sync:
            self.pending_sync -= 1
            return True
        return False

    def sleep(self, seconds):
        if self.on_wait:
            self.on_wait()


class RuntimeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "user"
        self.data = self.home / "custom data"
        self.app = self.home / "Applications/每日待办.app"
        self.agents = self.home / "Library/LaunchAgents"
        self.source = self.home / "Developer/Ocean/daily-todo-project/server.py"
        for directory in (self.data, self.app / "Contents/Resources/sync", self.app / "Contents/MacOS",
                          self.agents, self.source.parent):
            directory.mkdir(parents=True)
        self.source.write_text('# daily-todo\nclass Handler: pass\ndef serve_forever(): pass\nDATA_FILE="todos.json"\n')
        self.binary = self.app / "Contents/MacOS/每日待办"
        self.binary.write_text("test executable")
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.ocean.dailytodo", "CFBundleExecutable": "每日待办",
            "CFBundleShortVersionString": "1.2.0"}))
        for name in HELPERS:
            (self.app / "Contents/Resources/sync" / name).write_text("new " + name)
        (self.data / "todos.json").write_text('{"tasks":[{"id":"original"}]}')
        for name in ("sync.sh", "json-merge.py", "unrelated.txt"):
            (self.data / name).write_text("old " + name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.git("remote", "add", "origin", "ssh://private.invalid/private/data.git")
        for name in ("sync.sh", "unrelated.txt"):
            (self.data / name).write_text("staged " + name)
            self.git("add", "--", name)
            (self.data / name).write_text("working " + name)
        self.before_index = self.git("ls-files", "--stage", "-z")
        self.before_files = {p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file()}
        self.mac = FakeMac(self.data)
        self.server_path = self.agents / (SERVER + ".plist")
        self.sync_path = self.agents / (SYNC + ".plist")
        self.old_config = {"Label": SERVER, "ProgramArguments": ["/usr/bin/python3", str(self.source)],
                           "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
                           "EnvironmentVariables": {"TODO_DATA_DIR": str(self.data), "TODO_SYNC_BRANCH": "personal",
                                                    "TODO_SYNC_STATUS_FILE": str(self.home / "status")}}
        self.server_path.write_bytes(plistlib.dumps(self.old_config))
        self.sync_path.write_bytes(plistlib.dumps({"Label": SYNC,
            "ProgramArguments": ["/bin/bash", str(self.data / "sync.sh")], "StartInterval": 300}))
        self.mac.jobs = {SERVER: {"pid": 55, "arguments": self.old_config["ProgramArguments"]},
                         SYNC: {"pid": None, "arguments": ["/bin/bash", str(self.data / "sync.sh")]}}
        self.mac.listeners = {55}
        self.mac.health_value = {"app": "daily-todo", "version": "1.1.4", "channel": "project-preview",
                                 "data_file": str(self.data / "todos.json"), "preview": False}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.data), *args], stderr=subprocess.PIPE)

    def module(self):
        path = ROOT / "runtime_upgrade.py"
        self.assertTrue(path.exists(), "The bundle runtime migration implementation is missing")
        spec = importlib.util.spec_from_file_location("runtime_upgrade", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def prepare(self, **kwargs):
        return self.module().prepare_runtime(app_path=self.app, default_data_dir=self.home / "wrong-default",
            version="1.2.0", home=self.home, system=self.mac, **kwargs)

    def test_source_daemon_migrates_offline_and_preserves_unrelated_changes(self):
        original_plist = self.server_path.read_bytes()
        original_remote = self.git("remote", "-v")
        unrelated_index = self.git("ls-files", "--stage", "--", "unrelated.txt")
        result = self.prepare()
        self.assertTrue(result.migrated)
        self.assertEqual(result.data_dir, self.data)
        config = plistlib.loads(self.server_path.read_bytes())
        self.assertEqual(config["ProgramArguments"], [str(self.binary), "--server-only"])
        self.assertEqual(config["EnvironmentVariables"], {**self.old_config["EnvironmentVariables"],
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
        self.assertEqual((result.backup_dir / (SERVER + ".plist")).read_bytes(), original_plist)
        self.assertEqual((result.backup_dir / "todos.json").read_bytes(), self.before_files["todos.json"])
        self.assertEqual((result.backup_dir / "sync-index.entries").read_bytes(),
                         b"".join(entry + b"\0" for entry in self.before_index.split(b"\0") if entry and
                                  entry.split(b"\t", 1)[1].decode() in HELPERS))
        for name in HELPERS:
            self.assertEqual((self.data / name).read_text(), "new " + name)
            self.assertEqual(self.git("show", ":" + name), ("new " + name).encode())
        self.assertEqual(self.git("ls-files", "--stage", "--", "unrelated.txt"), unrelated_index)
        self.assertEqual((self.data / "unrelated.txt").read_bytes(), self.before_files["unrelated.txt"])
        self.assertEqual(self.git("remote", "-v"), original_remote)
        self.assertEqual((self.data / "todos.json").read_bytes(), self.before_files["todos.json"])

    def test_unknown_listener_refuses_before_touching_configuration_or_helpers(self):
        self.mac.listeners = {999}
        before = self.server_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "端口|服务"):
            self.prepare()
        self.assertEqual(self.mac.stops, [])
        self.assertEqual(self.server_path.read_bytes(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), self.before_index)
        self.assertFalse((self.data / "sync-helper.py").exists())

    def test_spoofed_source_plist_is_not_stopped_even_with_matching_health(self):
        self.source.write_text("print('unrelated program')")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.mac.stops, [])

    def test_program_override_cannot_disguise_an_unrelated_service(self):
        config = dict(self.old_config, Program="/other/unrelated-daemon")
        self.server_path.write_bytes(plistlib.dumps(config))
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.mac.stops, [])

    def test_partial_sync_stop_failure_restarts_old_jobs_without_modifying_data(self):
        stop = self.mac.stop
        def failing_stop(label):
            stop(label)
            if label == SYNC:
                raise RuntimeError("sync bootout returned an error after stopping")
        self.mac.stop = failing_stop
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertIn(SYNC, self.mac.jobs)
        self.assertIn(SERVER, self.mac.jobs)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), self.before_index)
        for name, content in self.before_files.items():
            self.assertEqual((self.data / name).read_bytes(), content)

    def test_closed_progress_view_does_not_interrupt_file_transaction(self):
        def closed_view(message):
            raise RuntimeError("window closed")
        result = self.prepare(progress=closed_view)
        self.assertTrue(result.migrated)
        self.assertEqual(self.mac.health_value["version"], "1.2.0")

    def test_invalid_data_is_rejected_before_stopping_original_writer(self):
        for content in ("not-json", '{"tasks":{}}', '{"tasks":[],"projects":{}}'):
            with self.subTest(content=content):
                (self.data / "todos.json").write_text(content)
                with self.assertRaises(RuntimeError):
                    self.prepare()
                self.assertEqual(self.mac.stops, [])
                self.assertEqual((self.data / "todos.json").read_text(), content)
                self.assertEqual(self.git("ls-files", "--stage", "-z"), self.before_index)

    def test_invalid_last_write_from_old_sync_does_not_install_new_helpers(self):
        self.mac.pending_sync = 1
        self.mac.on_wait = lambda: (self.data / "todos.json").write_text("broken-last-write")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertFalse((self.data / "sync-helper.py").exists())
        self.assertEqual(self.git("ls-files", "--stage", "-z"), self.before_index)

    def test_rollback_reports_source_restart_failure(self):
        self.mac.fail_new = True
        start = self.mac.start
        def fail_old(path):
            config = plistlib.loads(path.read_bytes())
            if config["Label"] == SERVER and "--server-only" not in config["ProgramArguments"]:
                raise RuntimeError("old source restart failed")
            start(path)
        self.mac.start = fail_old
        with self.assertRaisesRegex(RuntimeError, "恢复失败"):
            self.prepare()
        self.assertEqual(self.git("ls-files", "--stage", "-z"), self.before_index)
        self.assertEqual(plistlib.loads(self.server_path.read_bytes()), self.old_config)

    def test_waits_for_detached_sync_before_snapshot_and_replacement(self):
        self.mac.pending_sync = 2
        def finish_edit():
            self.assertFalse((self.data / "sync-helper.py").exists())
            (self.data / "todos.json").write_text('{"tasks":[{"id":"last-old-write"}]}')
        self.mac.on_wait = finish_edit
        result = self.prepare()
        self.assertEqual(json.loads((result.backup_dir / "todos.json").read_text())["tasks"][0]["id"],
                         "last-old-write")

    def test_running_sync_finishes_before_its_launchd_job_is_stopped(self):
        self.mac.pending_sync = 3
        original_stop = self.mac.stop
        def safe_stop(label):
            if label == SYNC and self.mac.pending_sync:
                raise RuntimeError("bootout interrupted an active sync")
            original_stop(label)
        self.mac.stop = safe_stop
        result = self.prepare()
        self.assertTrue(result.migrated)

    def test_current_install_with_loaded_sync_only_starts_daemon_without_overwriting_helper(self):
        self.prepare()
        self.mac.stop(SERVER)
        (self.data / "sync.sh").write_text("user changes")
        index = self.git("ls-files", "--stage", "-z")
        result = self.prepare()
        self.assertFalse(result.migrated)
        self.assertEqual((self.data / "sync.sh").read_text(), "user changes")
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_sync_and_data_locks_cover_every_helper_replacement(self):
        module = self.module()
        atomic = module._atomic_bytes
        replacements = []
        def check_locks(path, content, mode=None):
            if path.parent == self.data and path.name in HELPERS:
                for name in (".sync.lock", ".data.lock"):
                    with (self.data / name).open("a") as handle:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                replacements.append(path.name)
            return atomic(path, content, mode)
        with patch.object(module, "_atomic_bytes", side_effect=check_locks):
            module.prepare_runtime(app_path=self.app, default_data_dir=self.data, version="1.2.0",
                                   home=self.home, system=self.mac)
        self.assertEqual(set(replacements), set(HELPERS))

    def test_health_failure_restores_scripts_plists_and_exact_index_but_keeps_new_todos(self):
        old_server, old_sync = self.server_path.read_bytes(), self.sync_path.read_bytes()
        self.mac.fail_new = True
        self.mac.write_on_failure = True
        with self.assertRaisesRegex(RuntimeError, "健康|验证|启动"):
            self.prepare()
        self.assertEqual(self.server_path.read_bytes(), old_server)
        self.assertEqual(self.sync_path.read_bytes(), old_sync)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), self.before_index)
        self.assertFalse((self.data / "sync-helper.py").exists())
        for name in ("sync.sh", "json-merge.py", "unrelated.txt"):
            self.assertEqual((self.data / name).read_bytes(), self.before_files[name])
        self.assertEqual(json.loads((self.data / "todos.json").read_text())["tasks"][0]["id"], "new-edit")
        self.assertEqual(self.mac.jobs[SERVER]["arguments"], self.old_config["ProgramArguments"])
        backups = list((self.home / "Library/Application Support/daily-todo/runtime-backups").iterdir())
        self.assertEqual(len(backups), 1)

    def test_repeat_start_preserves_helper_edits_and_does_not_restart_services(self):
        self.prepare()
        (self.data / "sync.sh").write_text("user helper edit after upgrade")
        before_index = self.git("ls-files", "--stage", "-z")
        before_plist = self.server_path.read_bytes()
        stop_count = len(self.mac.stops)
        result = self.prepare()
        self.assertFalse(result.migrated)
        self.assertEqual(len(self.mac.stops), stop_count)
        self.assertEqual((self.data / "sync.sh").read_text(), "user helper edit after upgrade")
        self.assertEqual(self.git("ls-files", "--stage", "-z"), before_index)
        self.assertEqual(self.server_path.read_bytes(), before_plist)

    def test_old_app_without_daemon_uses_server_legacy_data_fallback(self):
        self.server_path.unlink()
        self.sync_path.unlink()
        self.mac.jobs = {}
        self.mac.listeners = set()
        self.mac.health_value = None
        result = self.module().prepare_runtime(app_path=self.app, default_data_dir=self.data,
            version="1.2.0", home=self.home, system=self.mac)
        self.assertEqual(result.data_dir, self.data)
        self.assertTrue(result.migrated)

    def test_health_must_match_channel_and_data_path(self):
        self.prepare()
        for field, value in (("channel", "project-preview"), ("data_file", "/other/todos.json")):
            with self.subTest(field=field):
                self.mac.health_value[field] = value
                # An existing managed daemon can be restarted, but cannot be
                # accepted with wrong health even if the version is current.
                original_start = self.mac.start
                def wrong_start(path):
                    original_start(path)
                    if plistlib.loads(path.read_bytes())["Label"] == SERVER:
                        self.mac.health_value[field] = value
                self.mac.start = wrong_start
                with self.assertRaises(RuntimeError):
                    self.prepare()
                self.mac.start = original_start


class DesktopRuntimeTests(unittest.TestCase):
    def invoke(self, arguments):
        self.assertIn("argv", inspect.signature(desktop_app.main).parameters,
                      "Desktop startup has no headless argument handling yet")
        desktop_app.main(arguments)

    def test_server_only_never_opens_gui_or_prepares_migration(self):
        served = []
        with patch.object(desktop_app.server, "serve_forever", side_effect=lambda: served.append(True)), \
                patch.dict("sys.modules", {"webview": None}):
            self.invoke(["--server-only"])
        self.assertEqual(served, [True])

    def test_local_health_bypasses_global_proxy_configuration(self):
        health = {"app": "daily-todo", "version": desktop_app.server.VERSION,
                  "channel": desktop_app.server.RELEASE_CHANNEL, "data_file": desktop_app.server.DATA_FILE,
                  "preview": False}
        def build(handler):
            self.assertEqual(handler.proxies, {})
            return SimpleNamespace(open=lambda *a, **k: io.BytesIO(json.dumps(health).encode()))
        with patch.dict(os.environ, {"TODO_PREVIEW": "0"}), \
                patch.object(desktop_app.urllib.request, "build_opener", side_effect=build), \
                patch.object(desktop_app.urllib.request, "urlopen", side_effect=AssertionError("global proxy used")):
            self.assertTrue(desktop_app.server_up())

    def test_window_is_visible_while_worker_prepares_then_loads_real_service(self):
        events = []
        window = SimpleNamespace(evaluate_js=lambda js: events.append(("progress", js)),
                                 load_url=lambda url: events.append(("url", url)),
                                 load_html=lambda html: events.append(("error", html)))
        main_ident = threading.get_ident()
        def create_window(*args, **kwargs):
            self.assertIn("正在", kwargs.get("html", ""))
            events.append(("window", main_ident))
            return window
        def prepare(progress):
            self.assertEqual(events[0][0], "window")
            self.assertNotEqual(threading.get_ident(), main_ident)
            progress("正在升级本机服务")
            events.append(("prepared", threading.get_ident()))
            return None
        def start(callback):
            thread = threading.Thread(target=callback)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        fake_webview = SimpleNamespace(create_window=create_window, start=start)
        with patch.dict("sys.modules", {"webview": fake_webview}), \
                patch.object(desktop_app, "prepare_desktop_runtime", side_effect=prepare, create=True), \
                patch.object(desktop_app, "setup_app_menu"), \
                patch.object(desktop_app, "sync_pull_blocking"):
            self.invoke([])
        self.assertIn(("url", "http://localhost:8766"), events)
        self.assertTrue(any(event[0] == "progress" for event in events))

    def test_failed_prepare_keeps_error_visible_without_loading_wrong_service(self):
        pages, urls = [], []
        window = SimpleNamespace(evaluate_js=lambda js: None, load_url=urls.append, load_html=pages.append)
        fake_webview = SimpleNamespace(create_window=lambda *a, **k: window, start=lambda callback: callback())
        with patch.dict("sys.modules", {"webview": fake_webview}), \
                patch.object(desktop_app, "prepare_desktop_runtime", side_effect=RuntimeError("无法确认端口服务"), create=True), \
                patch.object(desktop_app, "setup_app_menu"):
            self.invoke([])
        self.assertEqual(urls, [])
        self.assertIn("无法确认端口服务", pages[-1])


if __name__ == "__main__":
    unittest.main()
