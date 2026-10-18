# 每日待办 1.1.5 · 项目跟进版

项目可按 CRM／物流标签分别查看，里程碑直接拖动排序，人员名单统一维护中英文名和工号。

本次更新：

- 项目列表增加「全部／CRM／物流」切换，记住上次选择。新建、编辑项目可直接点选标签；分类内排序保留其他项目的全局位置。
- 里程碑标题前增加拖动手柄，也支持上下方向键。隐藏的完成节点保留位置，取消或保存失败回退，未提交事项草稿继续保留。
- 「添加里程碑」移至项目标题和左侧项目条目的右键菜单，移除「项目设置」按钮；已完成项目在右上角全局「设置」中查看。
- 人员维护仅显示中文名、英文名、工号。选人可按中英文名、旧名或工号联想筛选；已确认的重复身份支持合并，旧负责人文字仍可识别。修复 WebKit 选人确认时可能误存确认前输入的问题。
- 编辑项目的标签和核心项目可直接选择，负责人与日期并排，保存与取消固定在弹窗右下角。事项归属仅显示项目名称，窄窗口布局同步调整。
- 项目标签、人员资料和合并标记接入字段级同步；过期修改提示冲突并保留输入。

## 安装

这是手动安装的项目版预发布。安装包只包含程序代码，不含个人事项、人员资料、工号、群号或私有数据仓库。个人数据继续保存在各设备的私有数据目录。

Apple Silicon（M 系列）下载 `daily-todo-1.1.5-macos-arm64.zip`；Intel Mac 下载 `daily-todo-1.1.5-macos-x86_64.zip`。对应 `SHA256SUMS` 文件可校验安装包。

已有私有数据仓库的设备：

```bash
git clone --branch v1.1.5 https://github.com/zhenchuanwuzc-max/daily-todo-public.git ~/Developer/Ocean/daily-todo-project-tracking
cd ~/Developer/Ocean/daily-todo-project-tracking
mkdir -p dist
# Intel Mac 将文件名中的 arm64 换为 x86_64。
ditto -x -k ~/Downloads/daily-todo-1.1.5-macos-arm64.zip dist
python3 scripts/install-local.py --install
```

若已有源码目录，请先保留本地修改，再切换到 `v1.1.5`。安装器会备份旧应用、真实数据和启动配置，更新配套同步脚本；源码目录作为本地服务运行入口，需要保留。

所有使用同一私有数据仓库的 Mac 都需要升级应用和同步脚本后再编辑新版数据。详见 `sync/DEPLOY.md`。

## 验证边界

本机 153 项 Python 检查通过；项目主流程、标签、里程碑拖动、新增事项、完成状态、模板、日期、人员资料、人员配置和多人选人共 10 组隔离数据浏览器回归，分别在 Chromium 与 WebKit 验证。发布流水线仅在 Apple Silicon、Intel 两种架构的 Python 检查、资源校验、架构校验及签名校验通过后发布安装包。

同 context 自审，非独立视角。本机验证不代表其他 Mac 已升级；未进行 Instruments 帧时间测量或其他设备实机性能验收。
