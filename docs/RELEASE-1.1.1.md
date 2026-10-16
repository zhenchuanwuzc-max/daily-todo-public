# 每日待办 1.1.1 · 项目跟进版

事情跟进和项目跟进共用事项，支持项目、多个里程碑、负责人和截止日期，以及删除恢复和可复用任务模板。

本次更新：

- 事情跟进默认显示北京时间今天及后两天，在优先级组内按截止日期排序。时间范围和排序收进右上角「设置」，归属列只显示大项目名。
- 所有日期字段共用紧凑日历，选择后正确回显；支持直接输入、快捷日期和可选日期清空。
- 负责人支持自由输入、多选、按姓名或工号联想筛选。
- 左侧项目支持拖动排序；右键项目或里程碑可选择编辑、删除。
- 项目增加独立的预计上线时间，支持 PRD、运营物料链接和模板。
- 新接口与安装包同步更新，修复旧服务未加载新路由导致的保存 `not found`。

## 安装

这是手动安装的项目版预发布，不触发旧正式版自动更新。安装包不含个人事项、人员工号或私有数据仓库；已有数据继续存于本机的私有数据目录。

Apple Silicon（M 系列）下载 `daily-todo-1.1.1-macos-arm64.zip`；Intel Mac 下载 `daily-todo-1.1.1-macos-x86_64.zip`。可用对应的 `SHA256SUMS` 文件校验下载包。

已有私有数据仓库的设备，将这一版本源码放在本机开发目录，解压安装包至源码下的 `dist/`，然后运行配套安装器：

```bash
git clone --branch v1.1.1 https://github.com/zhenchuanwuzc-max/daily-todo-public.git ~/Developer/Ocean/daily-todo-project-tracking
cd ~/Developer/Ocean/daily-todo-project-tracking
mkdir -p dist
# Intel Mac 将文件名中的 arm64 换为 x86_64。
ditto -x -k ~/Downloads/daily-todo-1.1.1-macos-arm64.zip dist
python3 scripts/install-local.py --install
```

若已有该源码目录，请保留本地修改并切换到 `v1.1.1`，不要覆盖目录。安装器会备份旧应用、数据和启动配置，并一起更新三个同步脚本。安装后此源码目录用于运行本地服务，需保留在原处。

所有使用同一私有数据仓库的 Mac 都需要升级应用和同步脚本后再编辑项目数据，不能混用旧写入端。详细说明见仓库内 `sync/DEPLOY.md`。

## 验证边界

本机 117 项 Python 检查及 Chromium、WebKit 项目主流程通过；本机原生应用已启动，版本、资源与真实数据校验通过。另覆盖日期、人员多选与筛选、项目拖动、上线时间和近三天视图的隔离数据回归。

本机验证不代表其他 Mac 已升级；尚未完成其他设备的实机交互验收或 Instruments 帧时间测量。
