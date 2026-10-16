"""Derived project completion checks against a disposable local server and store."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def main():
    engine = os.environ.get("TODO_TEST_BROWSER", "chromium")
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR = tmp
        server.DATA_FILE = str(Path(tmp) / "todos.json")
        server.BACKUP_DIR = str(Path(tmp) / "backups")
        server.STATE_FILE = str(Path(tmp) / "state.json")
        server.APP_SUPPORT_DIR = tmp
        server.schedule_sync = lambda: None
        os.environ["TODO_DISABLE_SYNC"] = "1"
        os.environ["TODO_DISABLE_UPDATE"] = "1"
        os.environ["TODO_PREVIEW"] = "1"
        today = server.today_str()
        historical = "2020-01-01T01:00:00+00:00"

        def project(identity, name, **extra):
            return {"id": identity, "name": name, "owner": "Ocean", "core": True,
                    "archived": False, "created": historical, **extra}

        def milestone(identity, project_id, order, **extra):
            return {"id": identity, "name": identity, "project_id": project_id,
                    "owner": "Ocean", "sort_order": order, "created": historical, **extra}

        def task(identity, project_id, milestone_id=None, done=False, **extra):
            return {"id": identity, "text": identity, "project_id": project_id,
                    "milestone_id": milestone_id, "done": done, "owner": "Ocean",
                    "deadline": today, "priority": "P1", "created": historical,
                    "done_at": historical if done else None, **extra}

        legacy_completed = project("legacy-completed", "没有里程碑的历史完成项目")
        legacy_completed.pop("archived")
        fixture = {
            "projects": [project("empty", "空项目"), project("empty-node", "还含空里程碑"),
                         project("main", "跨里程碑验收项目"), legacy_completed,
                         project("manual", "历史手动归档项目", archived=True),
                         project("deleted", "已删除的完成项目", deleted_at=historical)],
            "milestones": [milestone("empty-node-first", "empty-node", 0),
                           milestone("empty-node-last", "empty-node", 1),
                           milestone("main-first", "main", 0),
                           milestone("main-second", "main", 1),
                           milestone("main-deleted-node", "main", 2, deleted_at=historical)],
            "tasks": [task("empty-deleted-task", "empty", done=True, deleted_at=historical),
                      task("empty-node-done", "empty-node", "empty-node-first", done=True),
                      task("historical-done", "main", "main-first", done=True),
                      task("first-open", "main", "main-first"),
                      task("second-open", "main", "main-second"),
                      task("unscheduled-open", "main"),
                      task("deleted-open", "main", deleted_at=historical),
                      task("legacy-completed-task", "legacy-completed", done=True),
                      task("manual-task", "manual", done=True),
                      task("deleted-task", "deleted", done=True)],
        }
        server.write_data(copy.deepcopy(fixture))
        original_projects = copy.deepcopy(fixture["projects"])
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        assert httpd.server_port != 8766, "Acceptance must never use the production port"
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch(headless=True)
                context = browser.new_context(viewport={"width": 1100, "height": 850},
                                              locale="zh-CN", timezone_id="Asia/Shanghai")
                page = context.new_page()
                errors, project_writes = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept())
                page.on("request", lambda request: project_writes.append(request.url)
                        if request.method not in ("GET", "HEAD") and "/projects/" in request.url else None)
                # A second client changes only the disposable server, simulating synchronization.
                other = browser.new_context()

                def settle():
                    page.evaluate("document.activeElement?.blur()")
                    page.wait_for_function("mutationCount === 0 && !renderPending")

                def refresh_state():
                    page.evaluate("async () => { document.activeElement?.blur(); await readState(true); }")
                    settle()

                def active_ids():
                    return set(page.locator("#projectList [data-project-id]").evaluate_all(
                        "nodes => nodes.map(node => node.dataset.projectId)"))

                def assert_main_active(expected):
                    assert ("main" in active_ids()) is expected, active_ids()
                    core_names = page.locator("#coreSummary .core-summary-line b").all_text_contents()
                    assert ("跨里程碑验收项目" in core_names) is expected, core_names

                def open_completed(project_id=None):
                    page.locator("#projectsTab").click()
                    settle()
                    menu = page.locator("#projectDetail .project-actions details.action-menu")
                    if not menu.evaluate("node => node.open"):
                        menu.locator("summary").click()
                    menu.get_by_role("button", name="已完成项目", exact=True).click()
                    dialog = page.locator("#completedProjectsDialog")
                    dialog.wait_for(state="visible")
                    if project_id is not None:
                        row = dialog.locator(f'[data-completed-project-id="{project_id}"]')
                        row.get_by_role("button", name="查看", exact=True).click()
                        dialog.wait_for(state="hidden")
                        settle()
                    return dialog

                def close_completed():
                    page.keyboard.press("Escape")
                    page.locator("#completedProjectsDialog").wait_for(state="hidden")
                    settle()

                def set_done(task_id, done):
                    box = page.locator(f'#projectDetail [data-id="{task_id}"] input[type=checkbox]')
                    box.set_checked(done)
                    settle()
                    persisted = next(task for task in server.read_data()["tasks"] if task["id"] == task_id)
                    assert persisted["done"] is done

                def external_patch(task_id, patch):
                    response = other.request.patch(url + "/todos/" + task_id, data=patch)
                    assert response.ok, response.text()
                    refresh_state()

                def assert_project_metadata_unchanged():
                    assert server.read_data()["projects"] == original_projects, "Completion must not persist archive metadata"
                    assert not project_writes, f"Completion issued project mutations: {project_writes}"

                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                page.goto(url)
                page.wait_for_function("state.projects?.length === 6")
                settle()
                assert active_ids() == {"empty", "empty-node", "main"}, active_ids()
                assert_main_active(True)
                assert_project_metadata_unchanged()
                page.locator("#projectsTab").click()
                page.locator('#projectList [data-project-id="main"]').click()
                settle()
                assert "1/4 项完成" in page.locator("#projectDetail .project-progress").inner_text()
                assert not page.locator("#showCompletedProjectItems").is_checked()
                assert page.locator('#projectDetail [data-id="historical-done"]').count() == 0

                # Completing one milestone cannot archive another unfinished milestone.
                set_done("first-open", True)
                assert_main_active(True)
                assert "2/4 项完成" in page.locator("#projectDetail .project-progress").inner_text()
                set_done("second-open", True)
                assert_main_active(True)
                assert "3/4 项完成" in page.locator("#projectDetail .project-progress").inner_text()
                # An unfinished unscheduled item also blocks completion of the whole project.
                assert page.locator('#projectDetail [data-id="unscheduled-open"]').count() == 1
                set_done("unscheduled-open", True)
                assert_main_active(False)
                assert active_ids() == {"empty", "empty-node"}
                assert_project_metadata_unchanged()

                page.reload()
                page.wait_for_function("state.projects?.length === 6")
                settle()
                assert_main_active(False)
                assert_project_metadata_unchanged()
                dialog = open_completed()
                completed_ids = set(dialog.locator("[data-completed-project-id]").evaluate_all(
                    "nodes => nodes.map(node => node.dataset.completedProjectId)"))
                assert completed_ids == {"main", "legacy-completed"}, completed_ids
                close_completed()

                # Opening a completed project reveals history despite the normal hidden-done default.
                open_completed("main")
                assert "跨里程碑验收项目" in page.locator("#projectDetail .project-heading").inner_text()
                assert set(page.locator("#projectDetail .inline-task").evaluate_all(
                    "nodes => nodes.map(node => node.dataset.id)")) == {
                        "historical-done", "first-open", "second-open", "unscheduled-open"}
                assert "4/4 项完成" in page.locator("#projectDetail .project-progress").inner_text()
                assert_main_active(False)
                # Undo remains available from the history view and revives the project immediately.
                set_done("historical-done", False)
                assert_main_active(True)
                set_done("historical-done", True)
                assert_main_active(False)

                # Adding unfinished work from the completed project also restores it.
                open_completed("main")
                add_toggle = page.locator("#projectAddToggle")
                if add_toggle.inner_text().strip() == "新增事项":
                    add_toggle.click()
                    settle()
                form = page.locator('form.milestone-add[data-milestone-id="main-first"]')
                form.locator('[name=text]').fill("完成后追加事项")
                form.locator('[name=deadline]').fill(today)
                form.get_by_role("button", name="添加", exact=True).click()
                settle()
                added = next(task for task in server.read_data()["tasks"] if task.get("text") == "完成后追加事项")
                assert not added["done"]
                assert_main_active(True)
                assert_project_metadata_unchanged()

                # Another client can complete, undo, and add work; every fresh read rederives status.
                external_patch(added["id"], {"done": True})
                assert_main_active(False)
                external_patch("second-open", {"done": False})
                assert_main_active(True)
                external_patch("second-open", {"done": True})
                assert_main_active(False)
                response = other.request.post(url + "/todos/add", data={
                    "text": "另一设备追加", "owner": "Ocean", "deadline": today,
                    "project_id": "main", "milestone_id": "main-second"})
                assert response.ok, response.text()
                external_added = response.json()["task"]
                refresh_state()
                assert_main_active(True)
                external_patch(external_added["id"], {"done": True})
                assert_main_active(False)
                assert_project_metadata_unchanged()

                # A completed project's explicit deletion must remove it from completed-project access.
                response = other.request.delete(url + "/projects/main")
                assert response.ok, response.text()
                refresh_state()
                dialog = open_completed()
                assert dialog.locator('[data-completed-project-id="main"]').count() == 0
                assert dialog.locator('[data-completed-project-id="deleted"]').count() == 0
                assert dialog.locator('[data-completed-project-id="legacy-completed"]').count() == 1
                close_completed()
                # The history entry must remain reachable even with no active projects.
                for project_id in ("empty", "empty-node"):
                    response = other.request.delete(url + "/projects/" + project_id)
                    assert response.ok, response.text()
                refresh_state()
                assert not active_ids()
                open_completed("legacy-completed")
                assert page.locator('#projectDetail [data-id="legacy-completed-task"]').count() == 1
                assert not errors, errors
                other.close()
                browser.close()
                print(f"PASS {engine}: derived project completion, empty/partial/milestone/unscheduled gates, historical progress, completed access, undo/add revival, cross-client updates, no archive writes, deletion exclusion; no JS errors")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
