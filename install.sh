#!/bin/bash
# daily-todo 一键安装
# 1. 注册 launchd 定时任务（每天 09:00）
# 2. 在 ~/Applications 生成「每日待办.app」
# 3. 立即启动一次，验证可用
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="${DIR}/com.ocean.daily-todo.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.ocean.daily-todo.plist"
APP_DIR="${HOME}/Applications"
APP_NAME="每日待办.app"
APP_PATH="${APP_DIR}/${APP_NAME}"

echo "==> 1/4 赋可执行权限"
chmod +x "${DIR}/launch.sh" "${DIR}/server.py" "${DIR}/start.sh"

echo "==> 2/4 安装 launchd 定时任务（每天 09:00）"
mkdir -p "${HOME}/Library/LaunchAgents"
# 卸载旧版（若有）
launchctl unload "${PLIST_DST}" 2>/dev/null || true
cp "${PLIST_SRC}" "${PLIST_DST}"
launchctl load "${PLIST_DST}"
echo "    ✓ 已注册：${PLIST_DST}"

echo "==> 3/4 生成「${APP_NAME}」到 ${APP_DIR}"
mkdir -p "${APP_DIR}"
# 删旧 app
rm -rf "${APP_PATH}"
# 用 osacompile 生成 .app（双击运行 launch.sh）
osacompile -o "${APP_PATH}" -e "do shell script \"bash '${DIR}/launch.sh'\""
# 替换图标（如已备份）
if [ -f "${DIR}/todo.icns" ]; then
    cp "${DIR}/todo.icns" "${APP_PATH}/Contents/Resources/applet.icns"
    touch "${APP_PATH}"
fi
echo "    ✓ 已生成：${APP_PATH}"

echo "==> 4/4 立即启动一次"
bash "${DIR}/launch.sh"

echo ""
echo "✅ 安装完成。"
echo ""
echo "用法："
echo "  - 每天 09:00 自动开"
echo "  - 平时双击「${APP_PATH}」也能开"
echo "  - 手动：bash '${DIR}/launch.sh'"
echo ""
echo "日志：/tmp/daily-todo.log（服务）/ /tmp/daily-todo.launchd.log（定时器）"
echo ""
echo "卸载：bash '${DIR}/uninstall.sh'"
