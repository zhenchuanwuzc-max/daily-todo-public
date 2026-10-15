#!/usr/bin/env python3
"""
daily-todo 桌面 App 主入口（py2app 打包入口）
- 后台线程跑 HTTP server
- 主线程跑 WKWebView 原生窗口
"""
import os
import json
import subprocess
import threading
import time
import urllib.request

import server  # 同包内


PORT = server.PORT
URL = f"http://localhost:{PORT}"
SYNC_SH = os.path.join(server.DATA_DIR, "sync.sh")


def sync_pull_blocking(timeout: int = 8) -> None:
    """启动时同步阻塞跑一次（拉远端），失败不抛"""
    if os.environ.get("TODO_DISABLE_SYNC") == "1" or not os.path.exists(SYNC_SH):
        return
    try:
        subprocess.run(
            ["/bin/bash", SYNC_SH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(SYNC_SH),
            timeout=timeout,
        )
    except Exception:
        pass


def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=1) as response:
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


def main() -> None:
    try:
        start_server_thread()
    except RuntimeError as exc:
        subprocess.run(["/usr/bin/osascript", "-e", 'on run argv\n display alert "每日待办暂时无法打开" message (item 1 of argv) as critical\nend run', str(exc)])
        return
    # 网络同步在后台进行，首屏与输入不等待远端；完成后页面轮询读回。
    threading.Thread(target=sync_pull_blocking, kwargs={"timeout": 120}, daemon=True).start()
    import webview
    window = webview.create_window(
        "每日待办",
        URL,
        width=1040,
        height=840,
        resizable=True,
        min_size=(920, 540),
    )
    _window_ref[0] = window
    webview.start(setup_app_menu)


if __name__ == "__main__":
    main()
