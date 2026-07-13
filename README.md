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
