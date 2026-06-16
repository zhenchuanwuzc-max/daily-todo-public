"""
py2app 打包脚本
用法：
    cd ~/daily-todo
    venv/bin/python setup.py py2app
产物：
    dist/每日待办.app
"""
import os
from setuptools import setup

# 版本号优先级：环境变量（CI 注入）> VERSION 文件 > 默认 0.0.0
VERSION = (
    os.environ.get("DAILYTODO_VERSION")
    or (open(os.path.join(os.path.dirname(__file__), "VERSION")).read().strip()
        if os.path.exists(os.path.join(os.path.dirname(__file__), "VERSION"))
        else "0.0.0")
)

APP = ["desktop_app.py"]
DATA_FILES = ["index.html", "server.py", "VERSION"]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "todo.icns",
    "plist": {
        "CFBundleName": "每日待办",
        "CFBundleDisplayName": "每日待办",
        "CFBundleIdentifier": "com.ocean.dailytodo",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
        "LSMinimumSystemVersion": "11.0",
    },
    "packages": ["webview", "apscheduler"],
    "includes": [
        "server",
        "json",
        "http.server",
        "urllib.request",
        "Foundation",
        "WebKit",
        "AppKit",
        "zoneinfo",
        "apscheduler.schedulers.background",
        "apscheduler.triggers.cron",
        "tzlocal",
    ],
    "excludes": ["tkinter", "test", "unittest"],
}

setup(
    app=APP,
    name="每日待办",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
