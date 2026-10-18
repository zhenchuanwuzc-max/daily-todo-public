# Project tracking stable release implementation plan

Goal: publish 1.2.1 as the latest stable release so existing 1.0.x clients can use Check for Updates to receive project tracking and compatible local services.

Architecture: one universal bundle contains the complete app and project-aware sync helpers. On first native startup, a visible background migration backs up local configuration, adopts the existing data directory, and moves the launchd server to the installed bundle executable with --server-only. The old client can keep its existing download/replacement protocol. Subsequent upgrades gain explicit validation and app backup/rollback.

Constraints: preserve all tasks/projects/milestones and user Git changes; do not write user data into public code; do not modify unrelated services; do not depend on network to migrate or start; preserve unknown port listeners; release only after both architectures run the identical package. No claim that another physical Mac has already installed it.

- [x] Runtime migration: runtime_upgrade.py, desktop_app.py and tests/test_runtime_upgrade.py. Exercise real temporary files/index, injected OS boundaries, identity conflicts, backups, idempotence and rollback; source preview remains isolated.
- [x] Native update transaction: server.py and tests/test_updates.py. Stable release lookup, compatible asset selection, single in-flight update, validate before stop, independent updater job, app backup and health-based rollback. Preserve TODO_DISABLE_UPDATE.
- [x] Universal packaging: setup.py, scripts/merge-universal.py, tests/test_universal_package.py and release.yml. Include sync payload; merge all Mach-O resources; sign/verify; run the same universal archive on Apple Silicon and Intel runners. Publish a draft only when all checks pass.
- [x] Integration: resolve legacy smoke fixtures against current required-deadline API, run Python suite and representative WebKit workflows, independent read-only review of actual working files, commit source and tag v1.2.1.
- [x] Release: download draft asset and verify digest; install and test native startup/project view on this Mac; confirm data IDs and relationships; then publish draft as latest stable, verify old client release discovery, and update install documentation/tool inventory.

Legacy limitation: 1.0.x deletes its old bundle before first startup of the replacement. The new startup migration can preserve/restore local configuration and data snapshots, but cannot promise to restore the already-deleted legacy bundle. The new updater preserves bundles for future upgrades.

Pre-release evidence: isolated unit/API suite passed; WebKit project entry and complete project workflows passed without JavaScript errors. Independent review findings (sync ordering, recovery, proxy handling and migration commit race) were corrected with targeted regressions. Real packaged dual-architecture and local installation acceptance remain release gates.

Local acceptance caught a ditto UTF-8 ZIP filename flag incompatibility before replacement. Added an actual ditto ZIP regression and traversal checks; kept 1.2.0 unpublished and moved the candidate to 1.2.1.

Final acceptance: v1.2.1 published as latest stable from commit 2b2b3cd; CI run 39097658908 passed all six jobs. 191 isolated tests passed. Local installed bundle signature, version, stable health, bundled daemon, helper payloads, native project view, quit/reopen and Check for Updates passed. Existing parsed JSON data was identical after startup: no records or links changed. The unmodified 1.0.22 public-release lookup returned v1.2.1 and the exact accepted universal ZIP digest. Other physical Macs and Instruments frame timing remain untested.
