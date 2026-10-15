"""End-to-end checks in a disposable store; never writes production data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def main():
    engine = os.environ.get("TODO_TEST_BROWSER", "chromium")
    output = Path(os.environ.get("TODO_BROWSER_OUTPUT", "/tmp/daily-todo-browser"))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR = tmp
        server.DATA_FILE = str(Path(tmp) / "todos.json")
        server.BACKUP_DIR = str(Path(tmp) / "backups")
        server.STATE_FILE = str(Path(tmp) / "state.json")
        server.APP_SUPPORT_DIR = tmp
        server.schedule_sync = lambda: None
        today = server.today_str()
        legacy = {"id": "legacy", "text": "原有独立事项", "priority": "P3", "done": False,
                  "created": "2026-01-01T10:00:00"}
        server.write_data({"tasks": [legacy]})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        with sync_playwright() as p:
            browser = getattr(p, engine).launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 850}, locale="zh-CN", timezone_id="Asia/Shanghai")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(url)
            page.locator('#groups [data-id="legacy"]').wait_for()
            assert server.read_data()["tasks"] == [legacy], "Opening must not migrate legacy tasks"
            assert page.locator('#groups [data-id="legacy"] [data-field=owner]').input_value() == "Ocean"
            assert page.locator('#group-P2 [data-id="legacy"]').count() == 1
            assert page.locator('#groups [data-id="legacy"] .pri-select').input_value() == "P2"
            assert page.locator('#priorityNav [data-group=P3], option[value=P3]').count() == 0
            assert "P3" not in page.locator('#newPri').inner_text()
            assert page.evaluate("localDateKey('2026-10-14T16:30:00Z')") == "2026-10-15"
            assert page.evaluate("localDateKey('2026-10-15T00:30:00')") == "2026-10-15"

            def settle():
                page.evaluate("document.activeElement?.blur()")
                page.wait_for_function("mutationCount === 0 && !renderPending")

            def task_named(name):
                return next(t for t in server.read_data()["tasks"] if t.get("text") == name and not t.get("deleted_at"))

            def save_entity(name, owner=None, core=False):
                dialog = page.locator("#entityDialog")
                dialog.locator('[name="name"]').fill(name)
                if owner:
                    dialog.locator('[name="owner"]').fill(owner)
                if core:
                    dialog.locator('[name="core"]').check()
                dialog.get_by_role("button", name="保存", exact=True).click()
                dialog.wait_for(state="hidden")
                settle()

            # New independent task: deadline required, default owner, no project required.
            page.locator("#newText").fill("独立事项截止日期验证")
            page.locator("#addBtn").click()
            assert len(server.read_data()["tasks"]) == 1
            page.locator("#newDeadline").fill(today)
            page.locator("#addBtn").click()
            settle()
            independent = task_named("独立事项截止日期验证")
            assert independent["owner"] == "Ocean" and not independent.get("project_id")
            assert page.locator(f'#groups [data-id="{independent["id"]}"]').count() == 1

            # Project create and two milestones, including core summary.
            page.locator("#projectsTab").click()
            page.locator("#newProjectBtn").click()
            save_entity("体验优化项目", "项目负责人", core=True)
            project_id = server.read_data()["projects"][0]["id"]
            for name in ("方案确认", "上线验收"):
                page.get_by_role("button", name="＋ 添加里程碑", exact=True).click()
                assert page.locator('#entityDialog [name="owner"]').input_value() == "项目负责人"
                save_entity(name)
            milestones = server.read_data()["milestones"]
            first, second = milestones[0]["id"], milestones[1]["id"]
            assert page.locator(".milestone.empty").count() >= 2
            assert "待拆事项" in page.locator("#projectDetail").inner_text()

            def add_in_milestone(mid, text, owner="Ocean", deadline=today):
                form = page.locator(f'form.milestone-add[data-milestone-id="{mid}"]')
                assert form.locator('[name=owner]').input_value() == "Ocean"
                form.locator('[name=text]').fill(text)
                form.locator('[name=owner]').fill(owner)
                form.locator('[name=deadline]').fill(deadline)
                form.get_by_role("button", name="添加", exact=True).click()
                settle()
                return task_named(text)

            first_task = add_in_milestone(first, "核对方案并完成评审", "张三")
            second_task = add_in_milestone(first, "完成交互验收", deadline="2099-12-31")
            later_task = add_in_milestone(second, "发布前检查", deadline="2099-12-31")
            assert first_task["project_id"] == project_id and first_task["milestone_id"] == first
            assert "0/2 完成" in page.locator(f'section[data-milestone-id="{first}"]').inner_text()
            assert "核对方案并完成评审" in page.locator("#coreSummary").inner_text()

            # Same item in both views; deadline is not a scheduled start date.
            page.locator("#thingsTab").click()
            row = page.locator(f'#groups [data-id="{first_task["id"]}"]')
            assert row.locator('[data-field=owner]').input_value() == "张三"
            assert page.locator(f'#groups [data-id="{second_task["id"]}"]').count() == 1
            row.locator('[data-field=owner]').fill("李四")
            row.locator('[data-field=owner]').press("Enter")
            settle()
            row.locator('input[type=checkbox]').check()
            settle()
            page.locator("#projectsTab").click()
            assert "1/2 完成" in page.locator(f'section[data-milestone-id="{first}"]').inner_text()
            assert page.locator(f'#projectDetail [data-id="{first_task["id"]}"] [data-field=owner]').input_value() == "李四"
            page.locator(f'#projectDetail [data-id="{second_task["id"]}"] input[type=checkbox]').check()
            settle()
            assert "✓ 方案确认" in page.locator(f'section[data-milestone-id="{first}"]').inner_text()
            assert "当前：上线验收" in page.locator(".project-progress").inner_text()
            section = page.locator(f'section[data-milestone-id="{first}"]')
            section.locator(".milestone-title").click()
            section.locator(f'[data-id="{first_task["id"]}"] input[type=checkbox]').uncheck()
            settle()
            assert "当前：方案确认" in page.locator(".project-progress").inner_text()
            page.screenshot(path=str(output / f"projects-{engine}.png"), full_page=True)

            # Keep a third item linked so unlink and automatic restoration are tested separately.
            retained_task = add_in_milestone(first, "里程碑恢复关联验证")

            # Reordering works; deletion preserves tasks, manual reassignment survives restore.
            page.locator(f'section[data-milestone-id="{second}"]').get_by_role("button", name="上移里程碑", exact=True).click()
            settle()
            assert page.locator("section.milestone[data-milestone-id]").first.get_attribute("data-milestone-id") == second
            page.locator(f'section[data-milestone-id="{first}"] .milestone-actions').get_by_role("button", name="删除", exact=True).click()
            settle()
            assert page.locator(f'.milestone:not([data-milestone-id]) [data-id="{first_task["id"]}"]').count() == 1
            page.locator(f'#projectDetail [data-id="{first_task["id"]}"] .association').select_option(json.dumps([project_id, second], separators=(",", ":")))
            settle()
            deleted_milestone_association = page.locator(f'#projectDetail [data-id="{second_task["id"]}"] .association')
            assert deleted_milestone_association.input_value().startswith("retained:")
            deleted_milestone_association.select_option(json.dumps([project_id, None], separators=(",", ":")))
            settle()
            page.locator(".deleted-milestones summary").click()
            page.locator(".deleted-milestone").get_by_role("button", name="恢复", exact=True).click()
            settle()
            assert task_named(first_task["text"])["milestone_id"] == second
            assert task_named(second_task["text"])["milestone_id"] is None
            assert task_named(retained_task["text"])["milestone_id"] == first

            page.locator(".project-actions").get_by_role("button", name="编辑", exact=True).click()
            save_entity("体验优化项目已修改")
            page.locator("#projectSearch").fill("项目负责人")
            assert page.locator(".project-item").count() == 1
            page.locator("#projectSearch").fill("不存在")
            assert page.locator(".project-item").count() == 0
            page.locator("#projectSearch").fill("")
            settle()
            page.locator(".project-actions").get_by_role("button", name="归档", exact=True).click()
            settle()
            assert page.locator('[data-filter="archived"]').get_attribute("aria-pressed") == "true"
            page.locator(".project-actions").get_by_role("button", name="恢复跟进", exact=True).click()
            settle()
            page.locator(".project-actions").get_by_role("button", name="删除", exact=True).click()
            settle()
            assert "回收站" in page.locator("#projectDetail").inner_text()
            page.locator("#thingsTab").click()
            row = page.locator(f'#groups [data-id="{first_task["id"]}"]')
            assert row.locator(".association").input_value().startswith("retained:")
            assert row.locator(".association option:checked").inner_text() == "项目已删除"
            row.locator(".association").select_option("")
            settle()
            assert not task_named(first_task["text"]).get("deleted_at")
            page.locator("#projectsTab").click()
            page.get_by_role("button", name="恢复项目", exact=True).click()
            settle()
            assert task_named(first_task["text"])["milestone_id"] is None
            assert task_named(first_task["text"])["project_id"] is None
            assert task_named(later_task["text"])["milestone_id"] == second

            # Failed save must retain draft and show error without changing storage.
            page.locator("#thingsTab").click()
            row = page.locator(f'#groups [data-id="{independent["id"]}"]')
            page.route(f'**/todos/{independent["id"]}', lambda route: route.fulfill(status=500, json={"error": "模拟保存失败"}))
            row.locator('[data-field=owner]').fill("失败后保留输入")
            row.locator('[data-field=owner]').press("Enter")
            settle()
            assert task_named(independent["text"])["owner"] == "Ocean"
            assert page.locator(f'#groups [data-id="{independent["id"]}"] [data-field=owner]').input_value() == "失败后保留输入"
            assert "未保存" in page.locator("#saveStatus").inner_text()
            page.unroute(f'**/todos/{independent["id"]}')
            page.locator(f'#groups [data-id="{independent["id"]}"] [data-field=owner]').press("Escape")
            settle()

            # An older failed PATCH must not replace text typed while that request is pending.
            held_patch = []
            page.evaluate("window.__heldPatch = false")
            def hold_patch(route):
                held_patch.append(route)
                page.evaluate("window.__heldPatch = true")
            page.route(f'**/todos/{independent["id"]}', hold_patch)
            owner = page.locator(f'#groups [data-id="{independent["id"]}"] [data-field=owner]')
            owner.fill("旧的提交姓名")
            owner.press("Enter")
            page.wait_for_function("window.__heldPatch === true")
            owner.fill("请求期间的新姓名")
            held_patch[0].fulfill(status=500, json={"error": "延迟保存失败"})
            page.wait_for_function("mutationCount === 0")
            assert owner.input_value() == "请求期间的新姓名"
            assert page.evaluate("id => fieldDrafts.get(id + ':owner').value", independent["id"]) == "请求期间的新姓名"
            assert task_named(independent["text"])["owner"] == "Ocean"
            page.unroute(f'**/todos/{independent["id"]}', hold_patch)
            page.locator("#projectsTab").click()  # Blurring saves the new value, then switches views.
            settle()
            page.locator("#thingsTab").click()
            assert page.locator(f'#groups [data-id="{independent["id"]}"] [data-field=owner]').input_value() == "请求期间的新姓名"
            assert task_named(independent["text"])["owner"] == "请求期间的新姓名"

            # Entity forms lock only while the request is pending, then recover all draft fields.
            page.locator("#projectsTab").click()
            page.locator(".project-actions").get_by_role("button", name="编辑", exact=True).click()
            dialog = page.locator("#entityDialog")
            dialog.locator('[name="name"]').fill("失败后保留项目名")
            held_entity = []
            page.evaluate("window.__heldEntity = false")
            def hold_entity(route):
                held_entity.append(route)
                page.evaluate("window.__heldEntity = true")
            page.route(f"**/projects/{project_id}", hold_entity)
            dialog.get_by_role("button", name="保存", exact=True).click()
            page.wait_for_function("window.__heldEntity === true")
            assert page.locator("#entityForm").get_attribute("aria-busy") == "true"
            assert page.evaluate("Array.from(document.querySelector('#entityForm').elements).every(control => control.disabled)")
            page.keyboard.press("Escape")
            assert page.locator("#entityDialog").is_visible(), "Escape must not dismiss a pending save"
            held_entity[0].fulfill(status=500, json={"error": "项目保存失败"})
            page.wait_for_function("mutationCount === 0 && !document.querySelector('#entityForm').hasAttribute('aria-busy')")
            assert page.evaluate("Array.from(document.querySelector('#entityForm').elements).every(control => !control.disabled)")
            assert dialog.locator('[name="name"]').input_value() == "失败后保留项目名"
            assert "保存失败" in page.locator("#entityError").inner_text()
            assert server.read_data()["projects"][0]["name"] == "体验优化项目已修改"
            page.unroute(f"**/projects/{project_id}", hold_entity)
            dialog.get_by_role("button", name="取消", exact=True).click()
            settle()

            # Minimum native window width, both views; refresh persistence.
            for width in (1100, 920):
                page.set_viewport_size({"width": width, "height": 850})
                for tab in ("things", "projects"):
                    page.locator(f"#{tab}Tab").click()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{tab} overflow at {width}"
            page.set_viewport_size({"width": 1100, "height": 850})
            page.reload()
            page.locator("#projectDetail .project-heading").wait_for()
            assert "体验优化项目已修改" in page.locator("#projectDetail").inner_text()
            page.locator("#thingsTab").click()
            page.screenshot(path=str(output / f"things-{engine}.png"), full_page=True)
            # A date rollover invalidates rendering even when the data revision does not change.
            midnight_fixture = {"revision": "unchanged-midnight", "updated": "2026-10-15T15:00:00Z",
                                "projects": [], "milestones": [], "tasks": [
                {"id": "midnight-done", "text": "昨日完成项", "done": True, "priority": "P1",
                 "created": "2026-10-15T01:00:00Z", "done_at": "2026-10-15T15:30:00Z"},
                {"id": "midnight-due", "text": "跨午夜截止项", "done": False, "priority": "P1",
                 "created": "2026-10-15T01:00:00Z", "deadline": "2026-10-15"},
                {"id": "mixed-naive", "text": "周日历史事项", "done": True,
                 "created": "2026-10-11T23:30:00", "done_at": "2026-10-11T23:40:00"},
                {"id": "mixed-utc", "text": "周一历史事项", "done": True,
                 "created": "2026-10-11T16:30:00Z", "done_at": "2026-10-11T16:40:00Z"},
            ]}
            page.route("**/todos", lambda route: route.fulfill(json=midnight_fixture))
            page.clock.install(time="2026-10-15T23:59:50+08:00")
            page.clock.pause_at("2026-10-15T23:59:55+08:00")
            page.evaluate("async () => { document.activeElement.blur(); await readState(true); }")
            assert page.locator('#groups [data-id="midnight-done"]').count() == 1
            assert page.locator('#groups [data-id="midnight-due"] .date-status').inner_text() == "今日"
            assert page.evaluate("calendarKey(earliestMonday())") == "2026-10-05"
            page.clock.run_for(10000)
            page.evaluate("readState()")
            assert page.evaluate("state.revision") == "unchanged-midnight"
            assert page.evaluate("todayStr()") == "2026-10-16"
            assert page.locator('#groups [data-id="midnight-done"]').count() == 0
            assert page.locator('#groups [data-id="midnight-due"] .date-status').inner_text() == "逾期"
            assert not errors, errors
            print(f"PASS {engine}: row editing/deadline, project CRUD/search/archive/restore, milestone CRUD/reorder, bidirectional done/undo, explicit unlink/restore, delayed failed-save draft retention, pending entity lock/recovery, midnight unchanged-revision refresh, mixed UTC/naive review weeks, 920px layout, persistence; no JS errors")
            browser.close()
        httpd.shutdown()


if __name__ == "__main__":
    main()
