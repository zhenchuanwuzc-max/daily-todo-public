# 每日待办 1.1.4 · 项目跟进版

项目新增更简洁：右键对应项目或里程碑即可添加事项，不再同时展开所有新增框。

本次更新：

- 移除项目右上角「新增事项」。右键里程碑，选择「新增 1 个事项 / 新增 2 个事项」，只在当前里程碑下打开输入行。
- 每行独立添加、取消；失败保留输入并支持重试。切换 Tab 或项目保留草稿，保存期间继续输入的内容不会被清空。刷新页面会丢弃未提交草稿。
- 新建项目提供「基础模板（无里程碑）」，只创建项目，不预置事项；通过右键项目名称直接新增待办。已有任务模板继续可用。
- 保留行内编辑、日期默认值、选人多选、项目及事项排序、完成联动和删除恢复。
- 修复右键菜单关闭时，待刷新的列表可能导致键盘焦点丢失的问题。

## 安装

这是手动安装的项目版预发布，不触发旧正式版自动更新。安装包不含个人事项、人员工号或私有数据仓库；已有数据继续存于本机私有数据目录。

Apple Silicon（M 系列）下载 `daily-todo-1.1.4-macos-arm64.zip`；Intel Mac 下载 `daily-todo-1.1.4-macos-x86_64.zip`。可用对应 `SHA256SUMS` 文件校验下载包。

已有私有数据仓库的设备，将此版本源码放在本机开发目录，解压安装包至源码下的 `dist/`，运行配套安装器：

```bash
git clone --branch v1.1.4 https://github.com/zhenchuanwuzc-max/daily-todo-public.git ~/Developer/Ocean/daily-todo-project-tracking
cd ~/Developer/Ocean/daily-todo-project-tracking
mkdir -p dist
# Intel Mac 将文件名中的 arm64 换为 x86_64。
ditto -x -k ~/Downloads/daily-todo-1.1.4-macos-arm64.zip dist
python3 scripts/install-local.py --install
```

若已有该源码目录，请保留本地修改并切换到 `v1.1.4`，不要覆盖目录。安装器会备份旧应用、数据和启动配置，并更新配套同步脚本。安装后此源码目录用于运行本地服务，需保留在原处。

所有使用同一私有数据仓库的 Mac 都需要升级应用和同步脚本后再编辑项目数据，不能混用旧写入端。详细说明见 `sync/DEPLOY.md`。

## 验证边界

本机 117 项 Python 检查通过。新增事项、项目主流程、项目配置、模板、日期选择、多人负责人、完成状态和项目菜单排序，共 8 组隔离数据浏览器回归，均在 Chromium 和 WebKit 通过。发布流程在两种架构上执行完整 Python 检查后打包，并校验资源、架构及签名。

同 context 自审，非独立视角。本机验证不代表其他 Mac 已升级；尚未完成其他设备的实机交互验收或 Instruments 帧时间测量。
