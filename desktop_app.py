#!/usr/bin/env python3
"""
daily-todo 桌面 App 主入口（py2app 打包入口）
- 后台线程跑 HTTP server
- 主线程跑 WKWebView 原生窗口
"""
import os
import json
from html import escape
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

import server  # 同包内


PORT = server.PORT
URL = f"http://localhost:{PORT}"
SYNC_SH = os.path.join(server.DATA_DIR, "sync.sh")


def sync_pull_blocking(timeout: int = 8, environment=None) -> None:
    """启动时同步阻塞跑一次（拉远端），失败不抛"""
    environment = dict(os.environ, **(environment or {}))
    if environment.get("TODO_DISABLE_SYNC") == "1" or not os.path.exists(SYNC_SH):
        return
    try:
        subprocess.run(
            ["/bin/bash", SYNC_SH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(SYNC_SH),
            env=environment,
            timeout=timeout,
        )
    except Exception:
        pass


def server_up() -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{URL}/health", timeout=1) as response:
            health = json.load(response)
    except OSError:
        return False
    except ValueError:
        raise RuntimeError("本地端口被其他服务占用，请先退出旧版每日待办后重试。")
    if (not isinstance(health, dict) or not isinstance(health.get("data_file"), str)
            or health.get("app") != "daily-todo" or health.get("version") != server.VERSION
            or health.get("channel") != server.RELEASE_CHANNEL
            or os.path.realpath(health.get("data_file", "")) != os.path.realpath(server.DATA_FILE)
            or health.get("preview") != (os.environ.get("TODO_PREVIEW") == "1")):
        raise RuntimeError("正在运行的待办服务与此版本不一致，请退出旧版或重新运行本地安装器。")
    return True


def start_server_thread() -> None:
    if server_up():
        return
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # 等就绪
    for _ in range(20):
        if server_up():
            return
        time.sleep(0.15)
    raise RuntimeError("每日待办服务未能启动，请检查本地服务日志。")


_window_ref = [None]
_menu_helper = [None]


def _menu_log(msg: str) -> None:
    try:
        with open("/tmp/daily-todo-menu.log", "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
    except Exception:
        pass


def setup_app_menu() -> None:
    """启动后在 macOS App 菜单（最左侧'每日待办'菜单）里加「检查更新…」项

    关键：操作 NSApp.mainMenu() 必须在主线程，否则会 silent fail。
    pywebview 把这个 callback 放在 worker thread 里跑，所以我们用
    NSObject + performSelectorOnMainThread 切回主线程。
    """
    _menu_log(f"setup_app_menu called")
    try:
        from AppKit import NSApp, NSMenuItem  # type: ignore
        from Foundation import NSObject  # type: ignore
    except Exception as e:
        _menu_log(f"import error: {e}")
        return

    class MenuSetupHelper(NSObject):
        def doSetup_(self, _):  # noqa: N802 — runs on main thread
            try:
                main_menu = NSApp.mainMenu()
                _menu_log(f"doSetup: mainMenu={main_menu}, items={main_menu.numberOfItems() if main_menu else 'N/A'}")
                if not main_menu or main_menu.numberOfItems() < 1:
                    return
                app_menu = main_menu.itemAtIndex_(0).submenu()
                if not app_menu:
                    return
                for i in range(app_menu.numberOfItems()):
                    if app_menu.itemAtIndex_(i).title() == "检查更新…":
                        _menu_log("already inserted, skip")
                        return
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    "检查更新…", "checkForUpdate:", ""
                )
                item.setTarget_(self)
                app_menu.insertItem_atIndex_(item, 1)
                app_menu.insertItem_atIndex_(NSMenuItem.separatorItem(), 2)
                self._inserted_item = item  # 保留引用
                _menu_log(f"INSERTED, app_menu now has {app_menu.numberOfItems()} items")
            except Exception as e:
                _menu_log(f"doSetup error: {e}")

        def checkForUpdate_(self, sender):  # noqa: N802 — menu callback (main thread)
            # ⚠️ 关键：evaluate_js 必须在子线程调用，否则主线程死锁：
            # 主线程调 evaluate_js → 等 WKWebView completionHandler →
            # completionHandler 也要回到主线程 → 主线程被自己占着 → 卡死
            import threading
            def _do():
                try:
                    w = _window_ref[0]
                    if w is not None:
                        w.evaluate_js("if (typeof checkUpdate === 'function') checkUpdate(true);")
                    _menu_log("menu clicked → checkUpdate(true)")
                except Exception as e:
                    _menu_log(f"checkForUpdate error: {e}")
            threading.Thread(target=_do, daemon=True).start()

        def scheduleSetup(self):  # noqa: N802
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "doSetup:", None, False
            )

    helper = MenuSetupHelper.alloc().init()
    _menu_helper[0] = helper  # 全局 retain，防 GC

    # 启动后多次尝试，确保菜单一定能挂上（pywebview 菜单创建时机不固定）
    import threading
    for delay in (0.5, 1.5, 3.0):
        threading.Timer(delay, helper.scheduleSetup).start()


def installed_bundle_path():
    """Source runs and dist/preview bundles never install a system service."""
    if (not getattr(sys, "frozen", False) or server.RELEASE_CHANNEL != "stable"
            or os.environ.get("TODO_PREVIEW") == "1" or PORT != 8766):
        return None
    import runtime_upgrade
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app" and runtime_upgrade.installed_app(parent):
            return parent
    return None


def prepare_desktop_runtime(progress):
    app = installed_bundle_path()
    if app is None:
        progress("正在启动每日待办…")
        start_server_thread()
        return None
    import runtime_upgrade
    result = runtime_upgrade.prepare_runtime(
        app_path=app, default_data_dir=Path(server.DATA_DIR), version=server.VERSION,
        channel=server.RELEASE_CHANNEL, port=PORT, progress=progress)
    # The daemon's existing configuration wins over the GUI's fallback path.
    # The GUI connects to that daemon and never starts a second embedded writer.
    global SYNC_SH
    server.DATA_DIR = str(result.data_dir)
    server.DATA_FILE = str(result.data_dir / "todos.json")
    SYNC_SH = str(result.data_dir / "sync.sh")
    config = runtime_upgrade.read_configuration(Path.home() / "Library/LaunchAgents/com.ocean.daily-todo-server.plist")
    return (config or {}).get("EnvironmentVariables", {})


def _startup_html(message="正在启动每日待办…", failed=False):
    title = "每日待办暂时无法打开" if failed else "每日待办"
    note = "请处理上述问题后重新打开 App；已保留本地待办数据。" if failed else "正在准备本机服务，首次升级可能需要等待现有同步完成。"
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{margin:0;background:#f7f7f5;color:#292d31;font:16px -apple-system,BlinkMacSystemFont,sans-serif;
display:grid;place-items:center;min-height:100vh}}main{{max-width:640px;padding:40px}}h1{{font-size:25px}}
p{{line-height:1.7}}.note{{color:#71757a;font-size:14px}}</style>
<main><h1>{title}</h1><p id="startup-status" role="status" aria-live="polite">{escape(message)}</p>
<p class="note">{note}</p></main></html>'''


def main(argv=None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    if "--server-only" in arguments:
        # launchd owns this long-lived process. No GUI, startup sync or runtime
        # migration here, including when the daemon is started by rollback.
        server.serve_forever()
        return
    import webview
    window = webview.create_window(
        "每日待办",
        html=_startup_html(),
        width=1040,
        height=840,
        resizable=True,
        min_size=(920, 540),
    )
    _window_ref[0] = window

    def start_in_background():
        # pywebview runs this callback on its worker thread, after the native
        # window exists. Waiting for launchd/sync never blocks the AppKit loop.
        setup_app_menu()

        def progress(message):
            window.evaluate_js("document.getElementById('startup-status').textContent = " + json.dumps(message))

        try:
            environment = prepare_desktop_runtime(progress)
            window.load_url(URL)
            # Only ordinary startup sync may contact the remote. The migration
            # has completed and the real UI is already available before it runs.
            threading.Thread(target=sync_pull_blocking,
                             kwargs={"timeout": 120, "environment": environment}, daemon=True).start()
        except Exception as exc:
            _menu_log(f"startup failed: {exc}")
            window.load_html(_startup_html(str(exc), failed=True))

    webview.start(start_in_background)


if __name__ == "__main__":
    main()
