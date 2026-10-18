"""Offline release acceptance gates; no real service, GitHub write, or App launch."""
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-release.py"


class ReleaseInstallTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "Missing verified release installation entry point")
        spec = importlib.util.spec_from_file_location("install_release", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.data = self.home / "data"
        self.data.mkdir()
        self.app = self.home / "Applications/每日待办.app"
        (self.app / "Contents/MacOS").mkdir(parents=True)
        (self.app / "Contents/MacOS/每日待办").write_bytes(b"fixture executable")
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "每日待办",
            "CFBundleIdentifier": "com.ocean.dailytodo", "CFBundleShortVersionString": "1.2.0"}))
        self.filename = "daily-todo-1.2.0-macos-universal.zip"
        self.release = {"tagName": "v1.2.0", "isDraft": True, "isPrerelease": False,
                        "assets": [{"name": self.filename}, {"name": "SHA256SUMS.txt"}]}

    def test_rejects_two_architecture_zips_before_download(self):
        self.release["assets"].append({"name": "old-arm64.zip"})
        with self.assertRaisesRegex(ValueError, "universal"):
            self.module.validate_release(self.release, "1.2.0")

    def test_rejects_a_prerelease_as_stable(self):
        self.release["isPrerelease"] = True
        with self.assertRaisesRegex(ValueError, "prerelease"):
            self.module.validate_release(self.release, "1.2.0")

    def test_accepts_the_exact_draft_release(self):
        self.assertEqual(self.module.validate_release(self.release, "1.2.0"), self.filename)

    def test_checksum_checks_the_actual_download(self):
        (self.home / self.filename).write_bytes(b"downloaded bytes")
        digest = hashlib.sha256(b"downloaded bytes").hexdigest()
        (self.home / "SHA256SUMS.txt").write_text(f"{digest}  {self.filename}\n")
        self.assertEqual(self.module.verify_checksum(self.home, self.filename), digest)
        (self.home / self.filename).write_bytes(b"corrupt bytes")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.module.verify_checksum(self.home, self.filename)

    def test_checksum_must_name_the_requested_zip(self):
        (self.home / self.filename).write_bytes(b"downloaded bytes")
        (self.home / "SHA256SUMS.txt").write_text("0" * 64 + "  another.zip\n")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.module.verify_checksum(self.home, self.filename)

    def test_install_identity_accepts_owned_source_but_rejects_foreign_listener(self):
        source = self.home / "developer/server.py"
        source.parent.mkdir()
        source.write_text("# daily-todo\nDATA_FILE = ''\nclass Handler: pass\ndef serve_forever(): pass\n")
        system = FakeSystem(self.data, command=f"/usr/bin/python3 {source}")
        identity = self.module.identify_server(self.app, self.data, self.home, 8766, system)
        self.assertEqual(identity, {"server_pid": 43210, "server_source": str(source)})
        system.command_text = "/usr/bin/python3 /tmp/unrelated.py"
        with self.assertRaisesRegex(RuntimeError, "listener"):
            self.module.identify_server(self.app, self.data, self.home, 8766, system)

    def test_install_identity_rejects_other_data_even_with_known_executable(self):
        system = FakeSystem(self.home / "other-data", command=str(self.app / "Contents/MacOS/每日待办"))
        with self.assertRaisesRegex(RuntimeError, "data"):
            self.module.identify_server(self.app, self.data, self.home, 8766, system)

    def test_publish_requires_stable_exact_version_and_real_data_directory(self):
        health = FakeSystem(self.data).health(8766)
        health.update(version="1.2.0", channel="stable")
        self.module.check_publish_health(health, "1.2.0", self.data)
        for field, value in (("version", "1.1.4"), ("channel", "project-preview"),
                             ("preview", True), ("data_file", str(self.home / "other/todos.json"))):
            with self.subTest(field=field):
                changed = dict(health, **{field: value})
                with self.assertRaises(RuntimeError):
                    self.module.check_publish_health(changed, "1.2.0", self.data)

    def prepare_publish(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.ocean.daily-todo-server.plist").write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"TODO_DATA_DIR": str(self.data)}}))
        directory = self.home / "Library/Application Support/daily-todo/release-verification"
        directory.mkdir(parents=True)
        (directory / "1.2.0.json").write_text(json.dumps({"status": "installed", "version": "1.2.0",
            "app": str(self.app), "data_dir": str(self.data), "zip_sha256": "a" * 64}))
        self.release["assets"][0]["digest"] = "sha256:" + "a" * 64
        system = FakeSystem(self.data, command=str(self.app / "Contents/MacOS/每日待办") + " --server-only")
        system.health_version = "1.2.0"
        system.health_channel = "stable"
        return system

    def test_publish_does_not_call_github_mutation_for_unaccepted_health(self):
        system = self.prepare_publish()
        system.health_version = "1.1.4"
        with patch.object(self.module.Path, "home", return_value=self.home), \
                patch.object(self.module, "InstallerSystem", return_value=system), \
                patch.object(self.module, "release_info", return_value=self.release), \
                patch.object(self.module.subprocess, "run") as mutation:
            with self.assertRaises(RuntimeError):
                self.module.publish_release("1.2.0")
            mutation.assert_not_called()

    def test_publish_rejects_asset_changed_after_installation(self):
        system = self.prepare_publish()
        self.release["assets"][0]["digest"] = "sha256:" + "b" * 64
        with patch.object(self.module.Path, "home", return_value=self.home), \
                patch.object(self.module, "InstallerSystem", return_value=system), \
                patch.object(self.module, "release_info", return_value=self.release), \
                patch.object(self.module.subprocess, "run") as mutation:
            with self.assertRaisesRegex(RuntimeError, "installation acceptance"):
                self.module.publish_release("1.2.0")
            mutation.assert_not_called()

    def test_publish_accepted_bundle_becomes_latest(self):
        system = self.prepare_publish()
        published = dict(self.release, isDraft=False)
        with patch.object(self.module.Path, "home", return_value=self.home), \
                patch.object(self.module, "InstallerSystem", return_value=system), \
                patch.object(self.module, "release_info", side_effect=[self.release, published]), \
                patch.object(self.module.subprocess, "run") as mutation, \
                patch.object(self.module.subprocess, "check_output", return_value='{"tag_name":"v1.2.0"}'):
            self.module.publish_release("1.2.0")
            mutation.assert_called_once_with(["gh", "release", "edit", "v1.2.0", "--repo",
                "zhenchuanwuzc-max/daily-todo-public", "--draft=false", "--prerelease=false", "--latest"], check=True)

    def test_stage_refuses_to_invent_a_different_version(self):
        version = (SCRIPT.parent.parent / "VERSION").read_bytes()
        result = subprocess.run(["bash", str(SCRIPT.parent / "release.sh"), "--stage", "999.999.999"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VERSION", result.stderr)
        self.assertEqual((SCRIPT.parent.parent / "VERSION").read_bytes(), version)


class FakeSystem:
    def __init__(self, data, command=""):
        self.data = data
        self.command_text = command
        self.health_version = "1.1.4"
        self.health_channel = "project-preview"

    def listener_pids(self, port):
        return {43210}

    def health(self, port):
        return {"ok": True, "app": "daily-todo", "version": self.health_version, "channel": self.health_channel,
                "preview": False, "data_file": str(self.data / "todos.json")}

    def command(self, pid):
        return self.command_text

    def job(self, label):
        return None

    def verify_signature(self, app):
        pass


if __name__ == "__main__":
    unittest.main()
