"""主流程冒烟测试：真起一个 server.py 子进程，走 HTTP 验证。

隔离保证（不碰真实数据 / 网络）：
- TODO_DATA_DIR 指向临时目录（里面没有 sync.sh，所以 schedule_sync 直接 return，不会 git 同步/推送）
- HOME 指向临时目录（server 的 state.json / backups 在 ~/Library/Application Support 下，随之隔离）
- TODO_PORT 用随机空闲端口（线上固定 8766，互不干扰）
- 启动后校验子进程日志里打印的 data 路径确实是临时目录，否则直接失败
运行：python3 -m unittest tests.test_smoke -v
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server.py")

STARTUP_TIMEOUT = 10   # 起服务到能响应的上限（秒）
REQUEST_TIMEOUT = 5    # 单个 HTTP 请求上限
TEST_TIMEOUT = 40      # 单个测试整体上限（SIGALRM 兜底）


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SmokeTest(unittest.TestCase):
    def setUp(self):
        # 单测超时兜底：卡住就抛错，cleanup 仍会杀子进程、删临时目录
        def _on_alarm(signum, frame):
            raise AssertionError(f"测试超过 {TEST_TIMEOUT}s 上限")
        old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(TEST_TIMEOUT)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        self.addCleanup(signal.alarm, 0)

        self.tmp = tempfile.mkdtemp(prefix="daily-todo-smoke-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.data_dir)
        os.makedirs(self.home)
        # 最小数据：结构同线上（顶层 tasks 数组）；todos.example.json 的 "todos" 键与 server 实际读的 "tasks" 不一致，这里按 server 为准
        with open(os.path.join(self.data_dir, "todos.json"), "w", encoding="utf-8") as f:
            json.dump({"updated": None, "tasks": [
                {"id": "seed-1", "text": "示例待办", "done": False, "priority": "P2"},
            ]}, f, ensure_ascii=False)

        self.proc = None
        self.port = None
        self.addCleanup(self.stop_server)  # 先于 rmtree 执行（后进先出）
        self.start_server()

    # ---------- 服务进程 ----------
    def start_server(self):
        self.port = free_port()
        self.log_path = os.path.join(self.tmp, f"server-{self.port}.log")
        env = dict(os.environ)
        env.update({
            "TODO_DATA_DIR": self.data_dir,
            "TODO_PORT": str(self.port),
            "HOME": self.home,
            "PYTHONUNBUFFERED": "1",
            "PYTHONFAULTHANDLER": "1",  # 卡住时 SIGABRT 能打印调用栈
        })
        env.pop("PYTHONPATH", None)
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, SERVER], cwd=ROOT, env=env,
            stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.fail(f"server 提前退出 rc={self.proc.returncode}\n{self.read_log()}")
            try:
                with urllib.request.urlopen(self.url("/todos"), timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            if self.proc.poll() is None:  # 还活着却没就绪：打出它卡在哪
                import signal
                self.proc.send_signal(signal.SIGABRT)
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self.fail(f"server {STARTUP_TIMEOUT}s 内未就绪\n{self.read_log()}")
        # 隔离自检：必须确认读写的是临时目录，不是真实数据仓
        log = self.read_log()
        expected = os.path.join(self.data_dir, "todos.json")
        self.assertIn(f"data: {expected}", log, f"server 没有使用临时数据目录，立即中止\n{log}")
        self.assertEqual(self.proc.poll(), None)

    def stop_server(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc = None
        if getattr(self, "_log", None):
            self._log.close()
            self._log = None

    def read_log(self) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    # ---------- HTTP 小工具 ----------
    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url(path), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8")

    def get_tasks(self):
        status, _, body = self.call("GET", "/todos")
        self.assertEqual(status, 200)
        return json.loads(body)["tasks"]

    def add(self, text, **extra):
        status, _, body = self.call("POST", "/todos/add", {"text": text, **extra})
        self.assertEqual(status, 200, body)
        return json.loads(body)

    # ---------- 主流程 ----------
    def test_homepage_is_html(self):
        status, ctype, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("<html", body.lower())
        self.assertIn("每日待办", body)

    def test_read_todos(self):
        tasks = self.get_tasks()
        self.assertEqual([t["id"] for t in tasks], ["seed-1"])

    def test_add_then_read(self):
        res = self.add("冒烟：新增一条")
        self.assertTrue(res["ok"])
        self.assertFalse(res["deduped"])
        task = res["task"]
        self.assertTrue(task["id"].startswith("claude-"))
        self.assertFalse(task["done"])
        tasks = {t["id"]: t for t in self.get_tasks()}
        self.assertIn(task["id"], tasks)
        self.assertEqual(tasks[task["id"]]["text"], "冒烟：新增一条")
        # 落盘在临时目录里
        with open(os.path.join(self.data_dir, "todos.json"), encoding="utf-8") as f:
            self.assertIn(task["id"], [t["id"] for t in json.load(f)["tasks"]])

    def test_add_same_text_is_deduped(self):
        first = self.add("冒烟：去重")
        second = self.add("冒烟： 去重")  # 空格差异也应判重
        self.assertFalse(first["deduped"])
        self.assertTrue(second["deduped"])
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertEqual(sum(1 for t in self.get_tasks() if t["text"].replace(" ", "") == "冒烟：去重"), 1)

    def test_add_empty_text_rejected(self):
        status, _, _ = self.call("POST", "/todos/add", {"text": "   "})
        self.assertGreaterEqual(status, 400)
        self.assertEqual(len(self.get_tasks()), 1)

    def test_patch_done(self):
        task_id = self.add("冒烟：标完成")["task"]["id"]
        status, _, body = self.call("PATCH", f"/todos/{task_id}", {"done": True})
        self.assertEqual(status, 200, body)
        patched = json.loads(body)["task"]
        self.assertTrue(patched["done"])
        self.assertTrue(patched["done_at"])
        tasks = {t["id"]: t for t in self.get_tasks()}
        self.assertTrue(tasks[task_id]["done"])
        self.assertTrue(tasks[task_id]["done_at"])

    def test_patch_unknown_id_is_404(self):
        status, _, _ = self.call("PATCH", "/todos/no-such-id", {"done": True})
        self.assertEqual(status, 404)

    def test_data_survives_restart(self):
        kept = self.add("冒烟：重启后还在")["task"]["id"]
        done = self.add("冒烟：重启后保持已完成")["task"]["id"]
        self.assertEqual(self.call("PATCH", f"/todos/{done}", {"done": True})[0], 200)
        self.stop_server()
        self.start_server()
        tasks = {t["id"]: t for t in self.get_tasks()}
        self.assertEqual(tasks[kept]["text"], "冒烟：重启后还在")
        self.assertFalse(tasks[kept]["done"])
        self.assertTrue(tasks[done]["done"])
        self.assertIn("seed-1", tasks)


if __name__ == "__main__":
    unittest.main()
