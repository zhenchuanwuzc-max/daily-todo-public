# Weekly and Monthly Recurring Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add daily, weekly, and monthly recurring templates to daily-todo, then migrate Ocean's three reminders into the app.

**Architecture:** Preserve the current seed-template and generated-instance model. Add a pure schedule matcher, a locked recurring-seed endpoint, and UI controls for frequency-specific fields. The existing Asia/Shanghai 00:01 job remains the only generator.

**Tech Stack:** Python 3, `unittest`, standard-library HTTP server, vanilla JavaScript, HTML/CSS, JSON, APScheduler.

## Global Constraints

- Existing `recurring: "daily"` templates remain valid.
- Weekly weekdays use ISO integers 1-7; Monday is 1.
- Monthly dates use integers 1-31; nonexistent dates generate nothing that month.
- Instance IDs remain `recur:<seed-id>:YYYY-MM-DD`.
- Older unfinished instances are auto-skipped before generation.
- `POST /todos/add` remains recurrence-free.
- No RRULE, multiple weekdays, last-day semantics, schedule editing, or backfill.

---

### Task 1: Schedule matching and generation

**Files:**
- Create: `tests/test_recurring.py`
- Modify: `server.py:7-20`
- Modify: `server.py:480-559`

**Interfaces:**
- Produces: `schedule_matches(seed: dict, run_date: datetime.date) -> bool`
- Preserves: `ensure_recurring_today() -> bool`

- [ ] **Step 1: Write failing tests**

Create a `unittest.TestCase` that patches `DATA_FILE`, `STATE_FILE`, `APP_SUPPORT_DIR`, `BACKUP_DIR`, and `schedule_sync` to temporary locations. Add these exact behaviors:

```python
def test_schedule_matches_daily_weekly_and_monthly(self):
    run_date = date(2026, 7, 13)
    self.assertTrue(server.schedule_matches({"recurring": "daily"}, run_date))
    self.assertTrue(server.schedule_matches({"recurring": "weekly", "recur_weekday": 1}, run_date))
    self.assertFalse(server.schedule_matches({"recurring": "weekly", "recur_weekday": 2}, run_date))
    self.assertTrue(server.schedule_matches({"recurring": "monthly", "recur_monthday": 13}, run_date))
    self.assertFalse(server.schedule_matches({"recurring": "monthly", "recur_monthday": 12}, run_date))

def test_ensure_is_selective_and_idempotent(self):
    self.write_tasks([
        {"id": "daily", "text": "daily", "recurring": "daily"},
        {"id": "mon", "text": "mon", "recurring": "weekly", "recur_weekday": 1},
        {"id": "tue", "text": "tue", "recurring": "weekly", "recur_weekday": 2},
        {"id": "m13", "text": "m13", "recurring": "monthly", "recur_monthday": 13},
    ])
    with patch.object(server, "today_str", return_value="2026-07-13"):
        self.assertTrue(server.ensure_recurring_today())
        self.assertFalse(server.ensure_recurring_today())
    ids = {item["id"] for item in self.read_tasks()}
    self.assertEqual(
        {item for item in ids if item.startswith("recur:")},
        {"recur:daily:2026-07-13", "recur:mon:2026-07-13", "recur:m13:2026-07-13"},
    )
```

Also retain a regression test proving an older unfinished instance becomes `done: true` and `auto_skipped: true`.

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest tests.test_recurring -v`.

Expected: FAIL because `schedule_matches` does not exist and weekly/monthly seeds are ignored.

- [ ] **Step 3: Add minimal production code**

Import `date` and add:

```python
def schedule_matches(seed: dict, run_date: date) -> bool:
    recurring = seed.get("recurring")
    if recurring == "daily":
        return True
    if recurring == "weekly":
        value = seed.get("recur_weekday")
        return isinstance(value, int) and not isinstance(value, bool) and value == run_date.isoweekday()
    if recurring == "monthly":
        value = seed.get("recur_monthday")
        return isinstance(value, int) and not isinstance(value, bool) and value == run_date.day
    return False
```

In `ensure_recurring_today()`, parse `run_date = date.fromisoformat(today)`, collect seeds whose `recurring` is `daily`, `weekly`, or `monthly`, and skip nonmatching seeds before building an instance ID. Do not update `last_generated` for nonmatching seeds.

- [ ] **Step 4: Verify GREEN and commit**

Run `python3 -m unittest tests.test_recurring -v`; expect all tests PASS.

Commit:

```bash
git add server.py tests/test_recurring.py
git commit -m "feat: generate weekly and monthly recurring tasks"
```

---

### Task 2: Locked recurring-template endpoint

**Files:**
- Modify: `tests/test_recurring.py`
- Modify: `server.py:403-440`
- Modify: `server.py:662-726`

**Interfaces:**
- Produces: `add_recurring_seed(item: dict) -> tuple[dict, bool]`
- Produces: `POST /recurring/add` returning `{ok, deduped, task}`.

- [ ] **Step 1: Write failing validation tests**

Add tests that reject weekly values missing or outside 1-7, monthly values missing or outside 1-31, and unknown recurrence types. Add a success test:

```python
item = {
    "text": "CPSC weekly", "tag": "work", "priority": "P1",
    "recurring": "weekly", "recur_weekday": 1,
}
seed, deduped = server.add_recurring_seed(item)
duplicate, duplicate_deduped = server.add_recurring_seed(item)
self.assertFalse(deduped)
self.assertTrue(duplicate_deduped)
self.assertEqual(seed["id"], duplicate["id"])
self.assertEqual(seed["recur_weekday"], 1)
self.assertNotIn("due", seed)
self.assertEqual(len(self.read_tasks()), 1)
```

- [ ] **Step 2: Verify RED**

Run the two new test methods directly. Expected: FAIL because `add_recurring_seed` is missing.

- [ ] **Step 3: Implement validation and persistence**

`add_recurring_seed()` must validate text, recurrence type, and the relevant integer field. Under `_write_lock`, dedupe only against seed templates with the same normalized text, type, and schedule value; otherwise create `seed_<12 hex>` with `done`, timestamps, source, tag, priority, recurrence, and schedule fields. Persist with `_atomic_write_todos`; call `schedule_sync()` after releasing the lock.

- [ ] **Step 4: Add the endpoint**

Add before `/todos/add`:

```python
if self.path == "/recurring/add":
    length = int(self.headers.get("Content-Length", 0))
    try:
        item = json.loads(self.rfile.read(length).decode("utf-8"))
        task, deduped = add_recurring_seed(item)
        self._send(200, json.dumps({"ok": True, "deduped": deduped, "task": task}, ensure_ascii=False))
    except ValueError as exc:
        self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
    except Exception as exc:
        self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))
    return
```

- [ ] **Step 5: Verify GREEN and commit**

Run `python3 -m unittest discover -s tests -v`; expect all tests PASS.

Commit:

```bash
git add server.py tests/test_recurring.py
git commit -m "feat: add recurring task template endpoint"
```

---

### Task 3: Recurring-task UI

**Files:**
- Create: `tests/test_recurring_ui.py`
- Modify: `index.html:1514-1640`
- Modify: `README.md`

**Interfaces:**
- Consumes: `POST /recurring/add`, `POST /ensure-recurring`, `load()`.
- Produces: `scheduleLabel(seed) -> string` and frequency-specific form controls.

- [ ] **Step 1: Write a failing UI contract test**

```python
import pathlib
import unittest

class RecurringUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = pathlib.Path("index.html").read_text(encoding="utf-8")

    def test_frequency_controls_and_locked_endpoint_exist(self):
        self.assertIn('id="recurNewFrequency"', self.html)
        self.assertIn('<option value="daily">每日</option>', self.html)
        self.assertIn('<option value="weekly">每周</option>', self.html)
        self.assertIn('<option value="monthly">每月</option>', self.html)
        self.assertIn('id="recurNewWeekday"', self.html)
        self.assertIn('id="recurNewMonthday"', self.html)
        self.assertIn('fetch("/recurring/add"', self.html)
        self.assertIn("function scheduleLabel", self.html)
```

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest tests.test_recurring_ui -v`.

Expected: FAIL because the new controls are absent.

- [ ] **Step 3: Upgrade rendering and controls**

Filter seeds using `['daily', 'weekly', 'monthly'].includes(t.recurring)`. Rename visible copy to "重复任务". Add:

```javascript
function scheduleLabel(seed) {
  if (seed.recurring === "daily") return "每天";
  if (seed.recurring === "weekly") {
    return `每周${["", "一", "二", "三", "四", "五", "六", "日"][seed.recur_weekday] || "?"}`;
  }
  if (seed.recurring === "monthly") return `每月 ${seed.recur_monthday} 日`;
  return "未知周期";
}
```

Add a frequency select with daily/weekly/monthly, a weekday select with values 1-7, and a month-day select populated with 1-31. Frequency changes reveal only the relevant selector.

- [ ] **Step 4: Use the locked endpoint**

Replace direct `state.tasks.push()` with:

```javascript
async function addRecurSeed(text, priority, tag, recurring, weekday, monthday) {
  const payload = { text, priority, tag, recurring, source: "user" };
  if (recurring === "weekly") payload.recur_weekday = Number(weekday);
  if (recurring === "monthly") payload.recur_monthday = Number(monthday);
  const response = await fetch("/recurring/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "添加重复任务失败");
  await fetch("/ensure-recurring", { method: "POST" });
  await load();
}
```

Disable the add button while awaiting, show errors with `alert`, and restore it in `finally`.

- [ ] **Step 5: Document and verify**

Document `/recurring/add` and all three schemas in `README.md`. Then run:

```bash
python3 -m unittest discover -s tests -v
python3 -c 'from pathlib import Path; import re; h=Path("index.html").read_text(); Path("/tmp/daily-todo-inline.js").write_text("\n".join(re.findall(r"<script[^>]*>(.*?)</script>", h, re.S)))'
node --check /tmp/daily-todo-inline.js
```

Expected: all tests PASS and Node exits 0.

- [ ] **Step 6: Commit the UI**

```bash
git add index.html README.md tests/test_recurring_ui.py
git commit -m "feat: manage weekly and monthly tasks in the app"
```

---

### Task 4: Deploy and migrate

**Files:**
- Modify through HTTP only: `~/daily-todo-data/todos.json`
- Delete through Codex automation API: `cpsc`, `cpsc-2`

**Interfaces:**
- Consumes: `PATCH /todos/{id}`, `POST /recurring/add`, `POST /ensure-recurring`, `GET /todos`.

- [ ] **Step 1: Verify before deployment**

Run `python3 -m unittest discover -s tests -v` and `git diff --check`. Both must succeed.

- [ ] **Step 2: Restart the source server**

Confirm `lsof -nP -iTCP:8766 -sTCP:LISTEN` points to `server.py`. Kill that listener, then run `nohup python3 ~/daily-todo/server.py >/tmp/daily-todo.log 2>&1 &`. Poll `/version` and `/todos` until both return HTTP 200.

- [ ] **Step 3: Retire the temporary one-time tasks**

PATCH IDs `claude-c779f1f6b6be` and `claude-2895640fd083` with `{"done":true}`. Both responses must contain `ok:true` and `done:true`.

- [ ] **Step 4: Create exact recurring templates**

POST these bodies to `/recurring/add`:

```json
{"text":"CPSC｜每周反馈印巴本地可承运服务商进展","tag":"work","priority":"P1","source":"Codex","recurring":"weekly","recur_weekday":1}
{"text":"CPSC｜每周监控美国承运政策变化","tag":"work","priority":"P1","source":"Codex","recurring":"weekly","recur_weekday":1}
{"text":"香港活动排期","tag":"work","priority":"P1","source":"Codex","recurring":"monthly","recur_monthday":13}
```

First creation returns `deduped:false`; safe retries return `deduped:true`.

- [ ] **Step 5: Generate and verify idempotency**

Call `/ensure-recurring` twice. Verify one seed per text/schedule. If the Asia/Shanghai date is Monday the 13th, verify one generated instance per seed for that date; the second ensure adds nothing.

- [ ] **Step 6: Delete mistaken Codex automations**

Delete automation IDs `cpsc` and `cpsc-2` with the Codex automation API, then confirm their automation files no longer exist.

- [ ] **Step 7: Smoke-test and final verification**

In the local UI, verify the "重复任务" section, two "每周一" labels, one "每月 13 日" label, frequency-control switching, and ordinary current-day instances. Then rerun all tests, `git diff --check`, `git status --short`, and parse `GET /todos` as JSON.

