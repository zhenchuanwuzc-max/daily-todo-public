"""Isolated Git/file-remote tests. No real data or network remotes are used."""
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_project_merge import EARLY, LATER, LATEST, edited


SYNC = Path(__file__).resolve().parents[1] / "sync"
REAL_GIT = shutil.which("git")


@unittest.skipUnless(REAL_GIT, "Git is required for isolated sync tests")
class ProjectSyncTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local, self.remote, self.other = [self.root / name for name in ("local", "remote.git", "other")]
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0", TODO_DATA_DIR=str(self.local),
                        TODO_SYNC_LOG_FILE=str(self.root / "sync.log"),
                        TODO_SYNC_STATUS_FILE=str(self.root / "sync.status"), SYNC_ROUTES="local",
                        TODO_SYNC_BRANCH="main")
        self.command([REAL_GIT, "init", "--bare", "--initial-branch=main", str(self.remote)])
        self.command([REAL_GIT, "init", "--initial-branch=main", str(self.local)])
        self.git(self.local, "config", "user.name", "Test")
        self.git(self.local, "config", "user.email", "test@example.invalid")
        self.data = {"revision": 1, "schema_version": 2,
                     "tasks": [{"id": "t1", "text": "Original", "owner": "Ocean", "done": False,
                                "done_at": None, "created": "2026-10-14T08:00:00", "deadline": "2026-10-20"}],
                     "projects": [{"id": "p1", "name": "Project", "deleted_at": None}],
                     "milestones": [{"id": "m1", "project_id": "p1", "name": "Milestone", "deleted_at": None}]}
        self.write(self.local, self.data)
        (self.local / ".gitattributes").write_text("todos.json merge=todos-union\n")
        self.git(self.local, "add", "todos.json", ".gitattributes")
        self.git(self.local, "commit", "-m", "Initial")
        self.git(self.local, "remote", "add", "origin", str(self.remote))
        self.git(self.local, "push", "-u", "origin", "main")
        self.command([REAL_GIT, "clone", str(self.remote), str(self.other)])
        self.git(self.other, "config", "user.name", "Other")
        self.git(self.other, "config", "user.email", "other@example.invalid")

    def command(self, command, cwd=None, env=None, check=True, timeout=15):
        result = subprocess.run(command, cwd=cwd, env=env or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result

    def git(self, directory, *args):
        return self.command([REAL_GIT, "-C", str(directory), *args]).stdout.decode().strip()

    def write(self, directory, data):
        (directory / "todos.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def read(self, directory):
        return json.loads((directory / "todos.json").read_text())

    def sync(self, env=None, check=True):
        return self.command(["/bin/bash", str(SYNC / "sync.sh")], env=env, check=check)

    def status(self):
        return (self.root / "sync.status").read_text().strip()

    def publish_other(self, data):
        self.write(self.other, data)
        self.git(self.other, "add", "todos.json")
        self.git(self.other, "commit", "-m", "Other edits")
        self.git(self.other, "push", "origin", "main")

    def test_dirty_local_and_remote_edits_merge_without_loss(self):
        local, other = copy.deepcopy(self.data), copy.deepcopy(self.data)
        local["tasks"][0] = edited(local["tasks"][0], LATEST, owner="Local owner")
        local["tasks"].append({"id": "local-added", "text": "Local task"})
        other["tasks"][0] = edited(other["tasks"][0], LATER, text="Remote edit")
        other["tasks"].append({"id": "remote-added", "text": "Remote task"})
        other["projects"][0] = edited(other["projects"][0], LATER, name="Remote project name")
        self.write(self.local, local)
        self.publish_other(other)
        self.sync()
        result = self.read(self.local)
        task = next(t for t in result["tasks"] if t["id"] == "t1")
        self.assertEqual((task["owner"], task["text"]), ("Local owner", "Remote edit"))
        self.assertEqual({t["id"] for t in result["tasks"]}, {"t1", "local-added", "remote-added"})
        self.assertEqual(result["projects"][0]["name"], "Remote project name")
        self.assertTrue(self.status().startswith("ok:"))
        self.assertEqual(self.git(self.local, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.local, "ls-files", ".data.lock", ".sync.lock"), "")
        self.assertIn(".data.lock", (self.local / ".git/info/exclude").read_text())

    def test_true_undo_survives_git_rebase_and_stale_remote(self):
        local, other = copy.deepcopy(self.data), copy.deepcopy(self.data)
        local["tasks"][0] = edited(local["tasks"][0], LATEST, done=False, done_at=None)
        other["tasks"][0] = edited(other["tasks"][0], LATER, done=True, done_at=LATER)
        self.write(self.local, local)
        self.publish_other(other)
        self.sync()
        self.assertFalse(self.read(self.local)["tasks"][0]["done"])
        self.assertIsNone(self.read(self.local)["tasks"][0]["done_at"])

    def test_project_task_order_survives_sync_without_changing_thing_order(self):
        local, other = copy.deepcopy(self.data), copy.deepcopy(self.data)
        local["tasks"][0] = edited(local["tasks"][0], LATER, project_sort_order=2)
        other["tasks"][0] = edited(other["tasks"][0], LATEST, sort_order=7, text="Remote content")
        other["tasks"].append({"id": "new-task", "text": "Concurrent addition", "project_id": "p1"})
        self.write(self.local, local)
        self.publish_other(other)
        self.sync()
        tasks = {task["id"]: task for task in self.read(self.local)["tasks"]}
        self.assertEqual((tasks["t1"]["project_sort_order"], tasks["t1"]["sort_order"], tasks["t1"]["text"]),
                         (2, 7, "Remote content"))
        self.assertEqual(tasks["t1"]["_field_updated"]["project_sort_order"], LATER)
        self.assertIn("new-task", tasks)
        self.assertTrue(self.status().startswith("ok:"))

    def test_bad_local_data_is_not_committed(self):
        before = self.git(self.local, "rev-parse", "HEAD")
        (self.local / "todos.json").write_text("broken")
        result = self.sync(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.status().startswith("fail:"))
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertEqual((self.local / "todos.json").read_text(), "broken")

    def test_bad_remote_fast_forward_restores_local_snapshot(self):
        before = self.git(self.local, "rev-parse", "HEAD")
        (self.other / "todos.json").write_text("not json")
        self.git(self.other, "commit", "-am", "Malformed external writer")
        self.git(self.other, "push", "origin", "main")
        result = self.sync(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertEqual(self.read(self.local), self.data)
        self.assertTrue(self.status().startswith("fail:"))

    def test_missing_remote_is_failure(self):
        self.git(self.local, "remote", "remove", "origin")
        self.assertNotEqual(self.sync(check=False).returncode, 0)
        self.assertTrue(self.status().startswith("fail:"))

    def test_wrong_branch_is_failure(self):
        self.git(self.local, "checkout", "-b", "human-work")
        self.assertNotEqual(self.sync(check=False).returncode, 0)
        self.assertEqual(self.git(self.local, "branch", "--show-current"), "human-work")
        self.assertTrue(self.status().startswith("fail:"))

    def test_previously_tracked_lock_is_refused_and_stale_success_cleared(self):
        (self.local / ".data.lock").touch()
        self.git(self.local, "add", ".data.lock")
        self.git(self.local, "commit", "-m", "Accidentally tracked lock")
        (self.root / "sync.status").write_text("ok: old result")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.assertNotEqual(self.sync(check=False).returncode, 0)
        self.assertTrue(self.status().startswith("fail:"))
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)

    def delayed_env(self, phase):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "git"
        shim.write_text("#!" + sys.executable + "\n" +
                        "import os,sys,time\nfrom pathlib import Path\n" +
                        "phase=os.environ.get('SYNC_TEST_PHASE')\n" +
                        "if phase in sys.argv[1:]:\n" +
                        "    Path(os.environ['SYNC_TEST_READY']).write_text('ready')\n" +
                        "    until=time.monotonic()+8\n" +
                        "    while not Path(os.environ['SYNC_TEST_RELEASE']).exists():\n" +
                        "        if time.monotonic()>until: sys.exit(2)\n" +
                        "        time.sleep(0.02)\n" +
                        "os.execv(" + repr(REAL_GIT) + ", [" + repr(REAL_GIT) + ",*sys.argv[1:]])\n")
        shim.chmod(0o755)
        return dict(self.env, PATH=str(bin_dir) + os.pathsep + self.env.get("PATH", ""),
                    SYNC_TEST_PHASE=phase, SYNC_TEST_READY=str(self.root / "ready"),
                    SYNC_TEST_RELEASE=str(self.root / "release"))

    def start_sync(self, env):
        process = subprocess.Popen(["/bin/bash", str(SYNC / "sync.sh")], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            (self.root / "release").touch()
            if process.poll() is None:
                process.communicate(timeout=12)
            else:
                process.communicate()
        self.addCleanup(cleanup)
        return process

    def wait_for_file(self, path):
        until = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() > until:
                self.fail("isolated sync did not reach expected phase")
            time.sleep(0.02)

    def test_fetch_does_not_take_data_lock(self):
        env = self.delayed_env("fetch")
        process = self.start_sync(env)
        self.wait_for_file(self.root / "ready")
        with open(self.local / ".data.lock", "a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            new_data = copy.deepcopy(self.data)
            new_data["tasks"][0]["text"] = "Edit during fetch"
            self.write(self.local, new_data)
            (self.root / "release").touch()
        output, error = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(self.read(self.local)["tasks"][0]["text"], "Edit during fetch")

    def test_push_does_not_take_data_lock_and_reports_pending_edits(self):
        local = copy.deepcopy(self.data)
        local["tasks"][0]["text"] = "Committed edit"
        self.write(self.local, local)
        process = self.start_sync(self.delayed_env("push"))
        self.wait_for_file(self.root / "ready")
        with open(self.local / ".data.lock", "a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            new_data = self.read(self.local)
            new_data["tasks"].append({"id": "during-push", "text": "New work"})
            self.write(self.local, new_data)
        (self.root / "release").touch()
        _, error = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, error)
        self.assertTrue(self.status().startswith("pending:"))
        self.assertIn("during-push", {t["id"] for t in self.read(self.local)["tasks"]})
        self.sync()
        self.assertTrue(self.status().startswith("ok:"))

    def test_outer_lock_serializes_entire_sync(self):
        process = self.start_sync(self.delayed_env("fetch"))
        self.wait_for_file(self.root / "ready")
        with open(self.local / ".sync.lock", "a") as handle:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        second = self.start_sync(self.env)
        time.sleep(0.15)
        self.assertIsNone(second.poll())
        self.assertEqual(self.status(), "busy: 正在同步")
        (self.root / "release").touch()
        for job in (process, second):
            _, error = job.communicate(timeout=12)
            self.assertEqual(job.returncode, 0, error)

    def test_local_stage_waits_for_application_data_lock(self):
        # Fetch is allowed while a writer is active; local rebase must wait.
        with open(self.local / ".data.lock", "a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            process = self.start_sync(self.delayed_env("fetch"))
            self.wait_for_file(self.root / "ready")
            (self.root / "release").touch()
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), self.git(self.local, "rev-parse", "origin/main"))
        _, error = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, error)


if __name__ == "__main__":
    unittest.main()
