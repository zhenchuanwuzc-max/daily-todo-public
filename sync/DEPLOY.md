# Daily Todo project-tracking sync upgrade

This directory contains the project-aware replacement for the three-way merge
driver and sync launcher in the existing private data repository. It does not
change the remote URL or create a remote repository.

## Install on each device

1. Upgrade the app's writer first: all mutations must hold the exclusive fcntl
   lock at `TODO_DATA_DIR/.data.lock`, use stable record IDs, preserve unfamiliar
   fields/collections, and stamp touched fields in `_field_updated` plus
   `updated_at` using UTC ISO timestamps with microseconds. Stop old app/CLI
   writers that replace the entire JSON file. Do not run mixed old/new writers.
2. Pause existing sync jobs while installing. Back up `todos.json`, the current
   `json-merge.py` and `sync.sh`, Git HEAD/status, and any local changes outside
   the repository. Keep that backup until the upgrade has been verified.
3. Copy exactly `json-merge.py`, `sync.sh`, and `sync-helper.py` from this directory
   into `~/daily-todo-data/` (or the configured `TODO_DATA_DIR`). Set their mode to
   `755`. Keep them together. `DEPLOY.md` need not be copied.
4. In the private data repo explicitly stage the three installed files, including
   the new `sync-helper.py`, and commit the verified upgrade. Never stage lock
   files. Keep `.gitattributes` containing `todos.json merge=todos-union`.
5. Run the isolated tests first. For the first real sync use the existing launch
   mechanism; `sync.sh` will idempotently register the merge driver and add
   `.data.lock`, `.sync.lock`, and `__pycache__/` to `.git/info/exclude`.
6. Verify the status, records, and relationships on this device, then upgrade the
   other devices before allowing them to edit the project-aware data. Actual
   cross-device acceptance requires both upgraded devices; a local merge test is
   not evidence that a remote device is upgraded.

## Contracts

- Default branch: `main`; override `TODO_SYNC_BRANCH` if the existing private
  repo uses a different branch. A branch mismatch fails without rebasing.
- Default data directory: the scripts' directory; `TODO_DATA_DIR` can override it.
- Default status/log paths stay `/tmp/daily-todo-sync.status` and
  `/tmp/daily-todo-sync.log`. Isolated instances should override
  `TODO_SYNC_STATUS_FILE` and `TODO_SYNC_LOG_FILE` to avoid status collisions.
- Status prefixes: `busy:` while running, `ok:` only after a clean successful
  sync, `pending:` if new edits arrived while pushing, `fail:` for failures.
  Failed sync exits nonzero. No configured remote is a failure, not success.
- `.sync.lock` serializes complete sync processes. `.data.lock` covers only local
  snapshot commit, rebase, and validation. Fetch and push never hold the data
  lock. Concurrent app edits during push stay dirty for the next scheduled sync.
- Network limits remain 12 seconds per fetch route and 20 seconds for push.
  All child processes are terminated on timeout. GitHub retains SSH 22/443 and
  system proxy fallbacks; HTTPS retains direct/proxy routes. Other SSH hosts keep
  their configured transport instead of being redirected to GitHub.
- Local edits are committed before rebase, avoiding an unvalidated autostash
  restore. Failed rebase aborts back to that snapshot. Invalid merged/remote JSON
  restores that exact pre-rebase snapshot while holding the data lock; nothing
  invalid is pushed. Existing manual Git conflicts are left for manual resolution.
- Merge covers `tasks`, `projects`, `milestones`, `people`, and
  `weekly_report_groups`, preserves unknown top-level
  metadata, uses `max(revision)+1`, and retains the highest schema version.
  Unchanged legacy physical deletions propagate; an edit versus deletion retains
  the edited record. New soft deletions retain records and their children.
- A true same-field conflict uses `_field_updated`, then `updated_at`, then
  legacy `done_at`/`created`. Equal timestamps use canonical JSON as a deterministic
  tie-break. Independent edits survive, and explicit `done=false` can win.
  `done`/`done_at` and `project_id`/`milestone_id` each merge as a coupled pair.
- People use deterministic IDs derived from normalized names; `name` and
  `employee_id` merge together. Weekly recipient groups merge individually,
  with `deleted_at` tombstones for removal. An older snapshot that omits either
  new collection does not erase existing configuration. These fields only store
  configuration; sync does not send DingTalk messages or schedule weekly reports.
- Project `prd_url` and `materials_url` merge independently, including explicit
  empty values. A saved `template_snapshot` merges as one complete field, so a
  conflict cannot combine milestones from one saved template with tasks from
  another. Template-created project, milestone, and task IDs are new stable IDs;
  the ordinary collection merge preserves them without modifying the source.
- Bad JSON, duplicate keys/IDs, non-finite numbers, or malformed collections cause
  merge failure. The merge driver never strips conflict markers or substitutes
  the one readable side.

## Verify and rollback

Run `python3 -m unittest tests.test_project_merge tests.test_project_sync` from
the application source checkout. Tests use temporary repositories and local
file remotes; they never use the real private repo or a network remote.

After verification, check one real bidirectional edit and an undo across upgraded
devices. If rollback is needed, pause all writers and sync jobs, preserve all
post-upgrade changes in a new backup, and restore app + sync scripts + a compatible
data snapshot together. An old writer is not safe against new project data.
