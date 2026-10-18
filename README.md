# daily-todo

## 项目跟进正式版 1.2.0

This branch adds **事情跟进 / 项目跟进** tabs using one task store. Tasks have
inline owners and deadlines; projects and milestones support recovery after
deletion. Existing tasks remain unchanged until edited.

正式版使用同一个 universal ZIP 支持 Intel 与 Apple Silicon。原正式版 1.0.22 可用「检查更新」升级；1.1.4 项目预览版需用本版源码运行一次 `scripts/release.sh --install 1.2.0`，之后使用应用内更新。安装会核验包摘要、保留旧 App，并通过事务迁移本机后台服务；实际待办继续保存在已有私有数据目录。详见 [1.2.0 发布说明](docs/RELEASE-1.2.0.md)。

Run an isolated local preview (copies existing data once; never syncs it):

```bash
python3 scripts/preview.py
```

Open the printed localhost URL. See [project tracking usage](docs/PROJECT_TRACKING.md)
for controls, verification and local installation. The local preview remains
isolated and never uses the public automatic updater.

A tiny, local-first **daily todo** web app — one JSON file is the source of truth, a small Python server serves a single HTML page, and multi-device sync runs over a **private** Git repo with conflict-free JSON merging.

> Open-source code version. Ships example data only — your real todos live in a separate **private** data repo, so your code can be public while your todos stay private.

## Architecture
- **Code (this repo, public):** `server.py` + `index.html` + packaging scripts.
- **Data (private repo):** `todos.json` + `sync.sh` + `json-merge.py` + `.gitattributes`.
- The server reads data from `TODO_DATA_DIR` (defaults to `~/daily-todo-data`, falls back to `~/daily-todo`).
- A custom Git merge driver (`json-merge.py`) merges records and independent fields; invalid data or unresolved Git operations stop synchronization without discarding local edits.

## Install
```bash
git clone <this-repo> ~/daily-todo
git clone <your-private-data-repo> ~/daily-todo-data
cd ~/daily-todo && ./setup-on-this-mac.sh
```

## Recurring tasks

Recurring templates are stored beside ordinary tasks and generate normal task
instances at 00:01 Asia/Shanghai when their schedule matches:

```json
{"text":"Daily","recurring":"daily"}
{"text":"Monday","recurring":"weekly","recur_weekday":1}
{"text":"Month day 13","recurring":"monthly","recur_monthday":13}
```

Weekly values use ISO weekdays (`1` is Monday, `7` is Sunday). Monthly values
are calendar days `1` through `31`; a missing date is skipped rather than moved
to the end of the month.

Create templates through the locked endpoint:

```text
POST /recurring/add
```

The request accepts `text`, `tag`, `priority`, `source`, `recurring`, and the
relevant schedule field. `POST /todos/add` remains limited to ordinary tasks so
callers cannot accidentally turn a one-time task into a recurring template.

## 上线：scripts/release.sh

```bash
scripts/release.sh --no-restart    # 仅隔离测试；无参数也只测试
scripts/release.sh --stage 1.2.0   # 测试 → 推送已有版本的标签 → CI 验证 → 生成草稿
scripts/release.sh --install 1.2.0 # 唯一 universal ZIP + SHA 校验 → 备份安装 → 健康验收
scripts/release.sh --publish 1.2.0 # 核验本机安装回执与 stable 健康，再公开并设 latest
```

- 测试 = `python -m unittest discover -s tests`（含 `tests/test_smoke.py` 主流程冒烟：临时数据目录 + 临时 HOME + 随机端口起真实 `server.py`，不碰真实数据、不触发 git 同步）。任一失败即 exit 非 0，不发版。
- 构建草稿前要求：在 main、工作区干净、不落后公开仓库，`VERSION` 与显式版本一致，并已有对应 `docs/RELEASE-x.y.z.md`。脚本不自动增加版本号，也不覆盖已有标签。
- CI 先在两种架构完整测试，合成并重签一个通用包，再在两种架构解压同一 ZIP 验证包内服务。全部成功后只生成草稿。
- 本机安装用当前源码里的 `app_update.apply_update`，兼容尚无新版更新器的 1.1.4。旧 App、配置与数据快照保存在 `~/Library/Application Support/daily-todo/update-backups/`；失败恢复 App 与配置，不覆盖期间的新数据。
- `--publish` 要求该 ZIP 已通过本机安装验收、摘要未变，运行中服务来自已安装 App，版本与 `stable` 通道准确且使用既有数据目录。任一不符都不会公开。
- `tests/browser_reorder.py` 需要 playwright + 浏览器且不是 unittest 用例，未纳入 release 门禁。
- 测试实例用 env `TODO_PORT` 换端口（不设置则仍是 8766）。
