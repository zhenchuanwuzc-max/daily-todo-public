# Weekly and Monthly Recurring Tasks

Date: 2026-07-12
Status: approved design

## Goal

Extend daily-todo's existing daily recurring-task mechanism so the app can create tasks on a selected weekday or day of month. Migrate Ocean's current reminders into the app and remove the temporary Codex automations created for them.

The initial recurring tasks are:

- Every Monday: `CPSC｜每周反馈印巴本地可承运服务商进展`
- Every Monday: `CPSC｜每周监控美国承运政策变化`
- Every month on day 13: `香港活动排期`

## User-visible behavior

Recurring templates are managed in the existing future-items modal, whose "每日任务" section becomes "重复任务".

The add form supports:

- Daily
- Weekly, with a weekday selector
- Monthly, with a day-of-month selector from 1 through 31

At 00:01 Asia/Shanghai each day, the server checks all recurring templates. A template produces a normal task instance only when its schedule matches that date. The generated instance appears in the main list and behaves like any other task.

An unfinished recurring instance from an earlier date is automatically marked done with `auto_skipped: true` before a new day's generation check. This preserves the current daily-task behavior and prevents stale recurring instances from accumulating.

For monthly dates that do not exist in a given month, such as day 31 in February, no instance is generated that month. The schedule is not shifted to the month's final day.

## Data model

Existing daily templates remain valid:

```json
{
  "id": "seed_daily_practice",
  "recurring": "daily"
}
```

Weekly templates add an ISO weekday number, where Monday is 1 and Sunday is 7:

```json
{
  "id": "seed_cpsc_carrier",
  "recurring": "weekly",
  "recur_weekday": 1
}
```

Monthly templates add a calendar day:

```json
{
  "id": "seed_hk_campaign",
  "recurring": "monthly",
  "recur_monthday": 13
}
```

Generated instance IDs retain the existing deterministic format:

```text
recur:<seed-id>:YYYY-MM-DD
```

Generated instances keep `recur_source: <seed-id>` and do not copy schedule fields. Existing daily templates and instances require no migration.

## Server design

Rename the implementation concept from "daily recurring" to "recurring", while keeping the existing `/ensure-recurring` route and daily 00:01 scheduler.

Add a pure schedule-matching helper that accepts a template and a date:

- `daily` always matches.
- `weekly` matches when `date.isoweekday() == recur_weekday`.
- `monthly` matches when `date.day == recur_monthday`.
- Invalid or incomplete schedules do not match and are logged or rejected at creation time.

`ensure_recurring_today()` continues to:

1. Mark older unfinished recurring instances as auto-skipped.
2. Select all recurring templates without `recur_source`.
3. Generate an instance only for templates matching today.
4. Enforce idempotency through the deterministic instance ID and local `last_generated` state.

Add a dedicated `POST /recurring/add` endpoint. It accepts text, tag, priority, recurring type, and the relevant schedule field. It validates schedule values, creates a seed under the existing write lock, writes atomically, and triggers sync. The ordinary `POST /todos/add` endpoint remains unable to create recurring templates.

## UI design

The future-items modal displays all seeds with `recurring` in `daily`, `weekly`, or `monthly`.

Each template shows a readable schedule label:

- 每天
- 每周一 through 每周日
- 每月 1 日 through 每月 31 日

The add form contains a frequency selector. Selecting weekly reveals a weekday selector; selecting monthly reveals a month-day selector; selecting daily hides both. The form submits to `POST /recurring/add`, then reloads current data so the server remains the single writer for template creation.

Template deletion keeps the existing behavior: deleting the template does not delete historical instances.

## Migration

After the feature passes tests:

1. Create two weekly Monday templates for the CPSC tasks.
2. Create one monthly day-13 template for the Hong Kong campaign schedule.
3. Mark the two temporary one-time CPSC tasks complete so they remain in history but do not duplicate the first generated Monday instances.
4. Delete the Codex automations `cpsc` and `cpsc-2`.
5. Trigger `/ensure-recurring` on a controlled current-date check and verify no duplicate instance IDs are produced.

Because the migration date is a Sunday in America/Los_Angeles and a Monday in Asia/Shanghai, the server's existing Asia/Shanghai calendar date is authoritative for recurring generation.

## Testing

Add Python `unittest` coverage using temporary data and state files. Tests must be written and observed failing before production changes.

Required cases:

- Existing daily template still generates every day.
- Weekly Monday template generates on Monday.
- Weekly Monday template does not generate on Tuesday.
- Monthly day-13 template generates on the 13th.
- Monthly day-13 template does not generate on the 12th.
- Calling ensure twice for the same date produces one instance.
- An older unfinished recurring instance is auto-skipped.
- The recurring creation endpoint or its underlying function rejects missing and out-of-range schedule fields.
- A valid recurring seed is written with the expected schema.

UI verification includes JavaScript syntax validation plus a manual local-app smoke test of the frequency controls and displayed schedule labels.

## Non-goals

- Arbitrary RRULE expressions
- Multiple weekdays for one template
- "Last day of month" or "last weekday" schedules
- Time-of-day selection per template
- Editing a template's schedule in place
- Backfilling instances for past dates
