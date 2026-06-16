#!/bin/bash
# 另一台 Mac 上一键装 daily-todo
# 前提：已经 git clone https://github.com/<你>/daily-todo ~/daily-todo
#       并且 ~/daily-todo 是当前目录
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> 1/5 创建 venv + 装依赖"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet pywebview pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit py2app
echo "    ✓ venv 就绪"

echo ""
echo "==> 2/5 py2app 打包 .app（1-2 分钟）"
rm -rf build dist
python setup.py py2app 2>&1 | tail -2

echo ""
echo "==> 3/5 部署到 ~/Applications/"
mkdir -p ~/Applications
APP_PATH="$HOME/Applications/每日待办.app"
if [ -d "$APP_PATH" ]; then
    rm -rf "$APP_PATH"
fi
mv dist/每日待办.app "$APP_PATH"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_PATH"
echo "    ✓ $APP_PATH"

echo ""
echo "==> 4/5 装 launchd 定时（每天 09:00）"
PLIST="$HOME/Library/LaunchAgents/com.ocean.daily-todo.plist"
launchctl unload "$PLIST" 2>/dev/null || true
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ocean.daily-todo</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-a</string>
        <string>$APP_PATH</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/daily-todo.launchd.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/daily-todo.launchd.err</string>
</dict>
</plist>
EOF
launchctl load "$PLIST"
echo "    ✓ 已注册"

echo ""
echo "==> 5/5 立即拉一次 + 启动一次"
bash "$DIR/sync.sh"
open -a "$APP_PATH"

echo ""
echo "✅ 装完了"
echo "  - Dock 上有「每日待办」"
echo "  - 每天 09:00 自动开"
echo "  - 改任何任务 5 秒后自动 push 到 GitHub"
echo "  - 启动 App 时自动 pull 最新"
