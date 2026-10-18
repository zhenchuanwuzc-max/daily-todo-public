"""Exercise updater preparation with real ZIP/bundle files and fake OS calls."""
from contextlib import ExitStack
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

import server


class UpdateLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "user"
        self.app = self.home / "Applications/每日待办.app"
        self.support = self.home / "Library/Application Support/daily-todo"
        resources = self.app / "Contents/Resources"
        (resources / "lib/python3.13/lib-dynload").mkdir(parents=True)
        (self.app / "Contents/MacOS").mkdir()
        (self.app / "Contents/MacOS/python").write_text("old embedded Python")
        (self.app / "Contents/MacOS/每日待办").write_text("old application")
        (resources / "app_update.py").write_text("# preserved updater runner\n")
        (resources / "runtime_upgrade.py").write_text("# preserved runtime module\n")
        with zipfile.ZipFile(resources / "lib/python3.13.zip", "w") as archive:
            archive.writestr("stdlib-marker.txt", "existing embedded library")
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.ocean.dailytodo", "CFBundleShortVersionString": "1.2.0"}))
        self.release_zip = self.home / "release.zip"
        with zipfile.ZipFile(self.release_zip, "w") as archive:
            archive.writestr("每日待办.app/Contents/Info.plist", plistlib.dumps({
                "CFBundleIdentifier": "com.ocean.dailytodo", "CFBundleShortVersionString": "1.2.1"}))
            archive.writestr("每日待办.app/Contents/MacOS/每日待办", "new application")
        self.before = self.files(self.app)
        self.commands = []
        self.release = {"tag_name": "v1.2.1"}
        self.asset = {"browser_download_url": "https://example.invalid/update.zip", "digest": "sha256:" + "0" * 64}
        server._set_progress(status="idle", error=None)
        self.addCleanup(lambda: server._set_progress(status="idle", error=None))

    @staticmethod
    def files(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def execute(self, scenario):
        def download(url, destination, digest):
            shutil.copyfile(self.release_zip, destination)

        def operating_system(command, **kwargs):
            self.commands.append(command)
            if command[0] == "ditto":
                with zipfile.ZipFile(command[3]) as archive:
                    archive.extractall(command[4])
            elif command[0] == "codesign":
                self.assertTrue((Path(command[-1]) / "Contents/Info.plist").is_file())
            elif command[0] == "/usr/bin/env":
                # Verify the real copied runner and libraries exist; no bundle
                # process is executed and no test can contact local launchd.
                runner = Path(command[4])
                self.assertTrue(runner.is_file())
                self.assertNotIn(str(self.app), str(runner))
                if scenario == "preflight-fails":
                    raise subprocess.CalledProcessError(1, command, stderr="embedded import failed")
            elif command[:2] == ["launchctl", "submit"]:
                plan_path = Path(command[-1])
                plan = json.loads(plan_path.read_text())
                self.assertEqual(plan["app"], str(self.app))
                self.assertTrue((Path(plan["staged_app"]) / "Contents/Info.plist").exists())
                if scenario == "startup-error":
                    (plan_path.parent / "started.json").write_text('{"pid":12345}')
                    (plan_path.parent / "error.txt").write_text("launcher could not initialize its runtime")
            elif command[:2] != ["launchctl", "remove"]:
                self.fail("Unexpected operating-system command: " + repr(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "home", return_value=self.home))
            stack.enter_context(patch.object(server, "APP_SUPPORT_DIR", str(self.support)))
            stack.enter_context(patch.object(server, "DATA_DIR", str(self.home / "data")))
            stack.enter_context(patch.object(server, "download_asset", side_effect=download))
            stack.enter_context(patch.object(subprocess, "run", side_effect=operating_system))
            stack.enter_context(patch.object(time, "sleep", return_value=None))
            server._do_install_update(self.release, self.asset)

    def assert_original_and_backup_intact(self):
        self.assertEqual(self.files(self.app), self.before)
        backups = list((self.support / "update-backups").glob("*/每日待办.app"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.files(backups[0]), self.before)

    def test_preflight_failure_propagates_before_launching_or_replacing_app(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.execute("preflight-fails")
        self.assertEqual(caught.exception.stderr, "embedded import failed")
        self.assertFalse(any(command[:2] == ["launchctl", "submit"] for command in self.commands))
        self.assert_original_and_backup_intact()

    def test_accepted_job_without_start_ack_is_removed_and_reports_error(self):
        with self.assertRaisesRegex(RuntimeError, "未启动"):
            self.execute("missing-ack")
        submitted = [command for command in self.commands if command[:2] == ["launchctl", "submit"]]
        removed = [command for command in self.commands if command[:2] == ["launchctl", "remove"]]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(removed, [["launchctl", "remove", submitted[0][3]]])
        self.assert_original_and_backup_intact()

    def test_startup_error_is_reported_instead_of_leaving_restarting_status(self):
        with self.assertRaisesRegex(RuntimeError, "could not initialize"):
            self.execute("startup-error")
        self.assertNotEqual(server.get_progress()["status"], "restarting")
        self.assert_original_and_backup_intact()


if __name__ == "__main__":
    unittest.main()
