#!/usr/bin/env python3
"""
daily-todo 桌面 App 主入口（py2app 打包入口）
- 后台线程跑 HTTP server
- 主线程跑 WKWebView 原生窗口
"""
import os
import subprocess
import threading
import time
import urllib.request

import server  # 同包内


PORT = 8766
URL = f"http://localhost:{PORT}"
SYNC_SH = os.path.expanduser("~/daily-todo/sync.sh")


def sync_pull_blocking(timeout: int = 8) -> None:
    """启动时同步阻塞跑一次（拉远端），失败不抛"""
    if not os.path.exists(SYNC_SH):
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
        urllib.request.urlopen(f"{URL}/todos", timeout=1)
        return True
    except Exception:
        return False


def start_server_thread() -> None:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # 等就绪
    for _ in range(20):
        if server_up():
            return
        time.sleep(0.15)


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
    # 启动时先拉远端（其他机器上的最新改动）
    sync_pull_blocking()
    start_server_thread()
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
