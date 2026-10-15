"""Local deployment rollback and desktop service identity without native calls."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop_app


SPEC = importlib.util.spec_from_file_location(
    "daily_todo_local_install", Path(__file__).resolve().parents[1] / "scripts/install-local.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class LocalInstallRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name) / "user"
        self.root = Path(self.tmp.name) / "source"
        self.data = self.user / "daily-todo-data"
        self.app = self.user / "Applications/每日待办.app"
        self.agents = self.user / "Library/LaunchAgents"
        self.server_plist = self.agents / "com.ocean.daily-todo-server.plist"
        self.sync_plist = self.agents / "com.ocean.daily-todo-sync.plist"
        for directory in (self.data, self.app, self.agents, self.root / "sync",
                          self.root / "dist/每日待办.app/Contents"):
            directory.mkdir(parents=True)
        (self.app / "old-marker").write_text("old application")
        with (self.root / "dist/每日待办.app/Contents/Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": "com.ocean.dailytodo",
                          "CFBundleShortVersionString": "1.1.0"}, handle)
        for name in ("sync.sh", "json-merge.py", "sync-helper.py"):
            (self.root / "sync" / name).write_text("new " + name)
        (self.data / "todos.json").write_text('{"tasks":[{"id":"keep","text":"keep"}]}')
        for name in ("sync.sh", "json-merge.py", "unrelated.txt"):
            (self.data / name).write_text("baseline " + name)
        self.git("init", "-q")
        self.git("config", "user.email", "isolated-test@example.invalid")
        self.git("config", "user.name", "Isolated Test")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        # Staged and working versions differ intentionally. Restoring only the
        # files or HEAD would destroy one of these two preexisting changes.
        for name in ("sync.sh", "unrelated.txt"):
            (self.data / name).write_text("preexisting staged " + name)
            self.git("add", "--", name)
            (self.data / name).write_text("preexisting working " + name)
        self.index_before = self.git("ls-files", "--stage", "-z")
        self.files_before = {name: (self.data / name).read_bytes()
                             for name in ("todos.json", "sync.sh", "json-merge.py", "unrelated.txt")}
        self.native_calls = []

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.data), *args], stderr=subprocess.PIPE)

    def execute_failure(self, *, cleanup_raises=False, first_stop_raises=False,
                        write_after_snapshot=False, health_mismatch=False):
        failed = False
        def fake_run(*args, check=True):
            nonlocal failed
            if args[0] == "git":
                return subprocess.run(args, check=check, capture_output=True, text=True)
            self.native_calls.append(args)
            if (args[:2] == ("launchctl", "bootstrap") and check
                    and not failed and not first_stop_raises and not health_mismatch):
                failed = True
                if write_after_snapshot:
                    (self.data / "todos.json").write_text('{"tasks":[{"id":"new-user-edit"}]}')
                raise subprocess.CalledProcessError(1, args, stderr="injected bootstrap failure")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        stop_effect = [RuntimeError("initial stop failed") if first_stop_raises else None,
                       RuntimeError("cleanup failed") if cleanup_raises else None]
        with ExitStack() as stack:
            for key, value in {"ROOT": self.root, "USER_DIR": self.user, "DATA": self.data,
                               "APP": self.app, "AGENTS": self.agents,
                               "SERVER_PLIST": self.server_plist, "SYNC_PLIST": self.sync_plist}.items():
                stack.enter_context(patch.object(installer, key, value))
            stack.enter_context(patch.object(installer, "run", side_effect=fake_run))
            stack.enter_context(patch.object(installer, "stop_services", side_effect=stop_effect))
            stack.enter_context(patch.object(sys, "argv", ["install-local.py", "--install"]))
            stack.enter_context(patch.object(installer.time, "sleep", return_value=None))
            # Even for the health-failure case, no real local server is contacted.
            stack.enter_context(patch.object(installer.urllib.request, "urlopen", side_effect=lambda *a, **k:
                                            io.BytesIO(json.dumps({"channel": "project-preview", "version": "0.0.0",
                                                                   "data_file": str(self.data / "todos.json"),
                                                                   "preview": False}).encode())))
            stack.enter_context(redirect_stdout(io.StringIO()))
            expected = RuntimeError if first_stop_raises or health_mismatch else subprocess.CalledProcessError
            with self.assertRaises(expected):
                installer.main()

    def assert_original_restored(self, expect_original_data=True):
        self.assertEqual((self.app / "old-marker").read_text(), "old application")
        self.assertFalse((self.app / "Contents/Info.plist").exists())
        self.assertFalse(self.app.with_name("每日待办-new.app").exists())
        self.assertFalse((self.data / "sync-helper.py").exists())
        self.assertEqual(self.git("ls-files", "--stage", "-z"), self.index_before)
        for name, contents in self.files_before.items():
            if name != "todos.json" or expect_original_data:
                self.assertEqual((self.data / name).read_bytes(), contents, name)
        backups = list((self.user / "Library/Application Support/daily-todo/deploy-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "todos.json").read_bytes(), self.files_before["todos.json"])

    def test_rollback_removes_new_helper_and_plist_restores_exact_index(self):
        self.execute_failure()
        self.assert_original_restored()
        self.assertFalse(self.server_plist.exists())
        self.assertFalse(self.sync_plist.exists())

    def test_cleanup_error_does_not_prevent_restore_and_keeps_new_user_data(self):
        self.execute_failure(cleanup_raises=True, write_after_snapshot=True)
        self.assert_original_restored(expect_original_data=False)
        self.assertEqual(json.loads((self.data / "todos.json").read_text())["tasks"][0]["id"], "new-user-edit")
        self.assertFalse(self.server_plist.exists())

    def test_existing_plists_and_source_worktree_changes_are_restored(self):
        original_plists = {}
        for plist in (self.server_plist, self.sync_plist):
            with plist.open("wb") as handle:
                plistlib.dump({"Label": plist.stem, "ProgramArguments": ["/old/program"],
                              "EnvironmentVariables": {"UNCHANGED": "preserve"}}, handle)
            original_plists[plist] = plist.read_bytes()
        self.execute_failure(cleanup_raises=True)
        self.assert_original_restored()
        for plist, contents in original_plists.items():
            self.assertEqual(plist.read_bytes(), contents)
            self.assertIn(("launchctl", "bootstrap", installer.DOMAIN, str(plist)), self.native_calls)

    def test_failure_before_replacement_keeps_files_and_cleans_staged_bundle(self):
        self.execute_failure(first_stop_raises=True, cleanup_raises=True)
        self.assert_original_restored()
        self.assertFalse(self.server_plist.exists())

    def test_wrong_version_health_fails_installation_and_rolls_back(self):
        self.execute_failure(health_mismatch=True)
        self.assert_original_restored()
        self.assertFalse(self.server_plist.exists())


class DesktopServiceIdentityTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"TODO_PREVIEW": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.health = {"app": "daily-todo", "version": desktop_app.server.VERSION,
                       "channel": desktop_app.server.RELEASE_CHANNEL,
                       "data_file": desktop_app.server.DATA_FILE, "preview": False}

    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_matching_service_is_reused_without_starting_native_or_server_thread(self):
        with patch.object(desktop_app.urllib.request, "urlopen", side_effect=lambda *a, **k: self.response(self.health)) as call, \
                patch.object(desktop_app.threading, "Thread") as thread:
            self.assertTrue(desktop_app.server_up())
            desktop_app.start_server_thread()
            thread.assert_not_called()
            self.assertTrue(all(args[0][0].endswith("/health") for args in call.call_args_list))

    def test_each_identity_mismatch_fails_before_starting_thread(self):
        for field, value in (("app", "other"), ("version", "0.0.0"), ("channel", "stable"),
                             ("data_file", "/tmp/wrong-data/todos.json"), ("preview", True)):
            with self.subTest(field=field), \
                    patch.object(desktop_app.urllib.request, "urlopen", side_effect=lambda *a, **k:
                                 self.response({**self.health, field: value})), \
                    patch.object(desktop_app.threading, "Thread") as thread:
                with self.assertRaisesRegex(RuntimeError, "不一致"):
                    desktop_app.start_server_thread()
                thread.assert_not_called()

    def test_unreachable_and_non_json_services_are_distinguished(self):
        with patch.object(desktop_app.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            self.assertFalse(desktop_app.server_up())
        with patch.object(desktop_app.urllib.request, "urlopen", return_value=io.BytesIO(b"not-json")):
            with self.assertRaisesRegex(RuntimeError, "其他服务"):
                desktop_app.server_up()

    def test_malformed_health_shape_is_reported_as_service_mismatch(self):
        for health in ([], None, 1, {**self.health, "data_file": None}):
            with self.subTest(health=health), patch.object(
                    desktop_app.urllib.request, "urlopen", side_effect=lambda *a, **k: self.response(health)):
                with self.assertRaises(RuntimeError):
                    desktop_app.server_up()

    def test_start_timeout_fails_without_opening_an_app(self):
        with patch.object(desktop_app, "server_up", return_value=False), \
                patch.object(desktop_app.threading, "Thread") as thread, \
                patch.object(desktop_app.time, "sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "未能启动"):
                desktop_app.start_server_thread()
            thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
