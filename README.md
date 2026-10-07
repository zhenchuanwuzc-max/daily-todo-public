# daily-todo

A tiny, local-first **daily todo** web app — one JSON file is the source of truth, a small Python server serves a single HTML page, and multi-device sync runs over a **private** Git repo with conflict-free JSON merging.

> Open-source code version. Ships example data only — your real todos live in a separate **private** data repo, so your code can be public while your todos stay private.

## Architecture
- **Code (this repo, public):** `server.py` + `index.html` + packaging scripts.
- **Data (private repo):** `todos.json` + `sync.sh` + `json-merge.py` + `.gitattributes`.
- The server reads data from `TODO_DATA_DIR` (defaults to `~/daily-todo-data`, falls back to `~/daily-todo`).
- A custom Git merge driver (`json-merge.py`) does a set-union merge so two machines editing in parallel never produce conflict markers.

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
scripts/release.sh               # 跑全部测试 → 通过后重启线上服务 → 10 秒内轮询首页与 /todos
scripts/release.sh --no-restart  # 只跑测试，不碰线上
```

- 测试 = `python -m unittest discover -s tests`（含 `tests/test_smoke.py` 主流程冒烟：临时数据目录 + 临时 HOME + 随机端口起真实 `server.py`，不碰真实数据、不触发 git 同步）。任一失败即 exit 非 0，且**不重启**。
- 重启 = 退出并重新 `open -a ~/Applications/每日待办.app`（与 launchd `com.ocean.daily-todo` 每天 09:00 的启动命令一致）。重启后线上不通会弹 macOS 通知并 exit 非 0。
- 注意：线上 App 里带的是打包时的 `server.py` / `index.html` 副本，重启不会把仓库里的新代码装进去；要让新代码生效需重新打包（`setup-on-this-mac.sh`）或走 GitHub release 的 App 内更新。
- `tests/browser_reorder.py` 需要 playwright + 浏览器且不是 unittest 用例，未纳入 release 门禁。
- 测试实例用 env `TODO_PORT` 换端口（不设置则仍是 8766）。
