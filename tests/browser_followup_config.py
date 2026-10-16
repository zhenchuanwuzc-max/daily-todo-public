"""Follow-up configuration checks against an isolated store; never sends messages."""
import copy
import json
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
        server.APP_SUPPORT_DIR = tmp
        server.STATE_FILE = str(Path(tmp) / "state.json")
        server.schedule_sync = lambda: None
        legacy = {"id": "legacy", "text": "存量独立事项", "done": False, "priority": "P1"}
        server.write_data({"tasks": [legacy]})
        project = server.add_project({"name": "测试项目甲", "owner": "测试甲"})
        other = server.add_project({"name": "测试项目乙", "owner": "Ocean"})
        server.add_milestone({"name": "测试节点", "owner": "测试乙", "project_id": project["id"]})
        server.add_single_task({"text": "测试事项", "owner": "测试甲", "deadline": "2026-12-31", "project_id": project["id"]})
        original_tasks = copy.deepcopy(server.read_data()["tasks"])
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch(headless=True)
                context = browser.new_context(viewport={"width": 1013, "height": 774}, locale="zh-CN", timezone_id="Asia/Shanghai")
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                page.goto(url)
                page.locator("#groups [data-id=legacy]").wait_for()
                page.locator("#projectsTab").click()
                page.locator(f'#projectList [data-project-id="{project["id"]}"]').click()

                def settle():
                    page.evaluate("document.activeElement?.blur()")
                    page.wait_for_function("mutationCount === 0 && !renderPending")

                def menu_action(name):
                    if name == "编辑":
                        page.locator("#projectDetail .project-heading h2[role=button]").click()
                        return
                    selector = "#globalSettings" if name in ("人员维护", "周报接收群") else ".project-actions details.action-menu"
                    menu = page.locator(selector)
                    if not menu.evaluate("node => node.open"):
                        menu.locator("summary").click()
                    menu.get_by_role("button", name=name, exact=True).click()

                def person(name):
                    return next(p for p in server.read_data()["people"] if p["name"] == name)

                def person_row(name):
                    return page.locator(f'#peopleList .person-row[data-person-name="{name}"]')

                def groups():
                    return {g["group_id"] for g in server.read_data()["weekly_report_groups"] if not g.get("deleted_at")}

                def save(dialog_id, button_id):
                    page.locator(button_id).click()
                    page.locator(dialog_id).wait_for(state="hidden")
                    settle()

                assert page.locator("#globalSettings > summary").inner_text() == "设置"
                project_actions = page.locator(".project-actions .action-menu-list button").all_text_contents()
                assert "人员维护" not in project_actions and "人员工号" not in project_actions and "周报接收群" not in project_actions
                assert "编辑" not in project_actions and "归档" not in project_actions

                def project_card(value):
                    return page.locator(f'#projectList .project-item[data-project-id="{value["id"]}"]')

                context_menu = page.locator("#projectContextMenu")
                entity = page.locator("#entityDialog")

                def open_context(value):
                    project_card(value).click(button="right")
                    context_menu.wait_for(state="visible")
                    assert entity.is_hidden(), "Right-click must open the project menu before the editor"
                    assert context_menu.get_by_role("menuitem").all_text_contents() == ["编辑", "删除"]

                def assert_project_editor(value):
                    entity.wait_for(state="visible")
                    assert page.locator("#entityTitle").inner_text() == "编辑项目"
                    assert entity.locator('[name="name"]').input_value() == value["name"]
                    assert entity.locator('[name="owner"]').input_value() == value["owner"]

                def cancel_entity():
                    page.locator("#entityCancel").click()
                    entity.wait_for(state="hidden")
                    settle()

                # Context-menu targets do not depend on which project is currently selected.
                snapshot = Path(server.DATA_FILE).read_bytes()
                for target in (project, other):
                    open_context(target)
                    assert project_card(project).get_attribute("aria-current") == "true"
                    context_menu.get_by_role("menuitem", name="编辑", exact=True).click()
                    assert_project_editor(target)
                    assert page.locator("#projectAddToggle").get_attribute("aria-pressed") == "false"
                    cancel_entity()
                    assert Path(server.DATA_FILE).read_bytes() == snapshot

                # Project and milestone titles edit metadata without opening the large add forms.
                page.locator("#projectDetail .project-heading h2[role=button]").click()
                assert_project_editor(project)
                cancel_entity()
                page.locator("#projectDetail .milestone-title").first.click()
                assert page.locator("#entityTitle").inner_text() == "编辑里程碑"
                assert entity.locator('[name="name"]').input_value() == "测试节点"
                cancel_entity()
                assert page.locator("#projectAddToggle").get_attribute("aria-pressed") == "false"
                assert Path(server.DATA_FILE).read_bytes() == snapshot

                # Keyboard, outside click, scrolling, and viewport edges all close/place the menu.
                project_card(other).focus()
                page.keyboard.press("Shift+F10")
                context_menu.wait_for(state="visible")
                assert entity.is_hidden()
                page.keyboard.press("ArrowDown")
                assert context_menu.get_by_role("menuitem", name="删除", exact=True).evaluate("node => document.activeElement === node")
                page.keyboard.press("ArrowUp")
                assert context_menu.get_by_role("menuitem", name="编辑", exact=True).evaluate("node => document.activeElement === node")
                page.keyboard.press("Escape")
                context_menu.wait_for(state="hidden")
                open_context(other)
                page.mouse.click(4, 4)
                context_menu.wait_for(state="hidden")
                for width in (775, 1013):
                    page.set_viewport_size({"width": width, "height": 774})
                    # Resize intentionally dismisses menus; finish that event before opening at the edge.
                    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                    project_card(other).dispatch_event("contextmenu", {"button": 2, "clientX": width - 2, "clientY": 772})
                    context_menu.wait_for(state="visible")
                    bounds = context_menu.bounding_box()
                    assert bounds["x"] >= 0 and bounds["y"] >= 0
                    assert bounds["x"] + bounds["width"] <= width and bounds["y"] + bounds["height"] <= 774, bounds
                    page.keyboard.press("Escape")
                page.evaluate("() => { const spacer=document.createElement('div'); spacer.id='context-scroll-test'; spacer.style.height='1500px'; document.body.append(spacer); }")
                open_context(other)
                page.evaluate("window.scrollTo(0, 120)")
                context_menu.wait_for(state="hidden")
                page.evaluate("() => { document.getElementById('context-scroll-test').remove(); window.scrollTo(0,0); }")

                # Editing another project's metadata must not discard the current add-form draft.
                page.locator("#projectAddToggle").click()
                add_form = page.locator("#projectDetail .milestone-add").first
                add_form.locator('[name="text"]').fill("保留的新增事项草稿")
                add_form.locator('[name="owner"]').fill("草稿负责人")
                add_form.locator('[name="deadline"]').fill("2099-12-30")
                open_context(other)
                context_menu.get_by_role("menuitem", name="编辑", exact=True).click()
                assert_project_editor(other)
                entity.locator('[name="name"]').fill("取消的项目名称草稿")
                cancel_entity()
                assert page.locator("#projectAddToggle").get_attribute("aria-pressed") == "true"
                assert add_form.locator('[name="text"]').input_value() == "保留的新增事项草稿"
                assert add_form.locator('[name="owner"]').input_value() == "草稿负责人"
                assert add_form.locator('[name="deadline"]').input_value() == "2099-12-30"
                assert Path(server.DATA_FILE).read_bytes() == snapshot
                page.locator("#projectAddToggle").click()
                settle()

                # Menu deletion can be cancelled; accepted deletion remains recoverable.
                page.once("dialog", lambda dialog: dialog.dismiss())
                open_context(other)
                context_menu.get_by_role("menuitem", name="删除", exact=True).click()
                settle()
                assert Path(server.DATA_FILE).read_bytes() == snapshot
                page.once("dialog", lambda dialog: dialog.accept())
                open_context(other)
                context_menu.get_by_role("menuitem", name="删除", exact=True).click()
                settle()
                assert next(value for value in server.read_data()["projects"] if value["id"] == other["id"])["deleted_at"]
                assert project_card(other).count() == 0
                assert server.read_data()["tasks"] == original_tasks
                menu_action("恢复项目")
                page.locator(f'#projectRestoreList [data-project-id="{other["id"]}"]').get_by_role("button", name="恢复到进行中", exact=True).click()
                page.locator("#projectRestoreDialog").wait_for(state="hidden")
                settle()
                assert not next(value for value in server.read_data()["projects"] if value["id"] == other["id"])["deleted_at"]
                assert project_card(other).count() == 1
                project_card(project).click()

                # Project routing is per project; cancel and clear are both supported.
                menu_action("编辑")
                group_input = page.locator('#entityDialog [name="dingtalk_group_id"]')
                assert group_input.input_value() == ""
                group_input.fill("00123456")
                page.locator("#entityDialog").get_by_role("button", name="保存", exact=True).click()
                page.locator("#entityDialog").wait_for(state="hidden")
                settle()
                data = server.read_data()
                assert next(p for p in data["projects"] if p["id"] == project["id"])["dingtalk_group_id"] == "00123456"
                assert "dingtalk_group_id" not in next(p for p in data["projects"] if p["id"] == other["id"])
                menu_action("编辑")
                group_input.fill("discard-me")
                page.locator("#entityCancel").click()
                settle()
                menu_action("编辑")
                assert group_input.input_value() == "00123456"
                group_input.fill("")
                page.locator("#entityDialog").get_by_role("button", name="保存", exact=True).click()
                page.locator("#entityDialog").wait_for(state="hidden")
                settle()
                assert next(p for p in server.read_data()["projects"] if p["id"] == project["id"])["dingtalk_group_id"] == ""
                menu_action("编辑")
                response = context.request.patch(url + "/projects/" + project["id"], data={"dingtalk_group_id": "other-device-project-group"})
                assert response.ok
                page.locator('#entityDialog [name="name"]').fill("测试项目甲更名")
                page.locator("#entityDialog").get_by_role("button", name="保存", exact=True).click()
                page.locator("#entityDialog").wait_for(state="hidden")
                settle()
                assert next(p for p in server.read_data()["projects"] if p["id"] == project["id"])["dingtalk_group_id"] == "other-device-project-group", "Editing another field must not overwrite an unchanged group from a stale form"

                # Existing owners appear once without changing any source records.
                snapshot = Path(server.DATA_FILE).read_bytes()
                menu_action("人员维护")
                assert page.locator("#peopleTitle").inner_text() == "人员维护"
                assert page.locator("#peopleList .person-row").count() == 3
                assert person_row("测试甲").locator('[name="name"]').get_attribute("readonly") is not None
                assert person_row("Ocean").locator('[name="employee_id"]').input_value() == ""
                page.locator("#peopleCancel").click()
                settle()
                assert Path(server.DATA_FILE).read_bytes() == snapshot
                menu_action("人员维护")
                person_row("测试甲").locator('[name="employee_id"]').fill("00123")
                page.locator("#addPersonBtn").click()
                added = page.locator("#peopleList .person-row").last
                added.locator('[name="name"]').fill("测试丙")
                added.locator('[name="employee_id"]').fill("00999")
                save("#peopleDialog", "#peopleSave")
                assert person("测试甲")["employee_id"] == "00123"
                assert person("测试丙")["employee_id"] == "00999"
                assert server.read_data()["tasks"] == original_tasks

                # One global directory is available from either tab, including saved employee IDs.
                page.locator("#thingsTab").click()
                menu_action("人员维护")
                assert page.locator("#peopleList .person-row").count() == 4
                assert person_row("测试甲").locator('[name="employee_id"]').input_value() == "00123"
                assert person_row("测试丙").locator('[name="employee_id"]').input_value() == "00999"
                page.locator("#peopleCancel").click()
                settle()
                menu_action("周报接收群")
                assert page.locator("#weeklyReportGroupIds").input_value() == ""
                page.locator("#weeklyReportGroupsCancel").click()
                settle()
                page.locator("#projectsTab").click()

                # Slow/failed persistence locks controls, keeps the draft and permits retry.
                menu_action("人员维护")
                person_row("测试甲").locator('[name="employee_id"]').fill("00456")
                held = []
                page.route("**/people/upsert", lambda route: held.append(route))
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleForm').dataset.saving === 'true'")
                assert page.locator("#peopleForm input, #peopleForm button").evaluate_all("nodes => nodes.every(n => n.disabled)")
                page.keyboard.press("Escape")
                assert page.locator("#peopleDialog").is_visible()
                assert held, "The held save must have reached the route"
                held[0].fulfill(status=500, content_type="application/json", body=json.dumps({"error": "测试保存失败"}))
                page.wait_for_function("document.querySelector('#peopleForm').dataset.saving !== 'true'")
                assert person_row("测试甲").locator('[name="employee_id"]').input_value() == "00456"
                assert person("测试甲")["employee_id"] == "00123"
                assert page.locator("#peopleError").inner_text()
                page.unroute("**/people/upsert")
                save("#peopleDialog", "#peopleSave")
                assert person("测试甲")["employee_id"] == "00456"

                # A stale form must not overwrite an independently corrected employee number.
                menu_action("人员维护")
                person_row("测试甲").locator('[name="employee_id"]').fill("00777")
                response = context.request.post(url + "/people/upsert", data={"people": [{"name": "测试甲", "employee_id": "00888", "base_employee_id": "00456"}]})
                assert response.ok, response.text()
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleError').textContent.length > 0")
                assert "刷新" in page.locator("#peopleError").inner_text()
                assert person("测试甲")["employee_id"] == "00888"
                assert person_row("测试甲").locator('[name="employee_id"]').input_value() == "00777"
                page.locator("#peopleCancel").click()
                settle()
                page.reload()
                menu_action("人员维护")
                assert person_row("测试甲").locator('[name="employee_id"]').input_value() == "00888"
                person_row("测试甲").locator('[name="employee_id"]').fill("")
                save("#peopleDialog", "#peopleSave")
                assert person("测试甲")["employee_id"] == ""

                # Weekly recipients are separate from project groups, deduplicated and removable.
                menu_action("周报接收群")
                text = page.locator("#weeklyReportGroupIds")
                assert text.input_value() == ""
                text.fill("00123456\n cid-test-group== \n00123456\n\n")
                save("#weeklyReportGroupsDialog", "#weeklyReportGroupsSave")
                assert groups() == {"00123456", "cid-test-group=="}
                menu_action("周报接收群")
                text.fill("00123456\nlocal-added")
                response = context.request.post(url + "/weekly-report/groups", data={"group_ids": ["00123456", "cid-test-group==", "other-device-added"], "base_group_ids": ["00123456", "cid-test-group=="]})
                assert response.ok, response.text()
                save("#weeklyReportGroupsDialog", "#weeklyReportGroupsSave")
                assert groups() == {"00123456", "local-added", "other-device-added"}
                menu_action("周报接收群")
                text.fill("bad group id")
                page.locator("#weeklyReportGroupsSave").click()
                page.wait_for_function("document.querySelector('#weeklyReportGroupsError').textContent.length > 0")
                assert text.input_value() == "bad group id"
                assert groups() == {"00123456", "local-added", "other-device-added"}
                text.fill("")
                save("#weeklyReportGroupsDialog", "#weeklyReportGroupsSave")
                assert groups() == set()
                page.reload()
                menu_action("周报接收群")
                assert text.input_value() == ""
                page.locator("#weeklyReportGroupsCancel").click()
                settle()

                # The wider sidebar keeps project cards readable without overflowing the viewport.
                for width, expected_sidebar in ((920, 220), (1013, 220), (1305, 260)):
                    page.set_viewport_size({"width": width, "height": 774})
                    sidebar = page.locator(".project-sidebar").bounding_box()
                    assert abs(sidebar["width"] - expected_sidebar) <= 1, (width, sidebar)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Page overflow at {width}px"
                    assert page.locator("#projectList .project-item").evaluate_all("nodes => nodes.every(n => n.scrollWidth <= n.clientWidth + 1)"), f"Project card overflow at {width}px"
                    menu_action("人员维护")
                    assert page.locator("#peopleDialog").evaluate("n => n.scrollWidth <= n.clientWidth + 1"), f"Directory overflow at {width}px"
                    page.locator("#peopleCancel").click()
                    settle()

                # Configuration stays accessible when no active projects exist.
                for value in (project, other):
                    response = context.request.patch(url + "/projects/" + value["id"], data={"archived": True})
                    assert response.ok
                page.reload()
                assert page.locator("#projectList .project-item").count() == 0
                for width in (775, 1013):
                    page.set_viewport_size({"width": width, "height": 774})
                    for label, dialog_id, cancel in (("人员维护", "#peopleDialog", "#peopleCancel"), ("周报接收群", "#weeklyReportGroupsDialog", "#weeklyReportGroupsCancel")):
                        menu_action(label)
                        dialog = page.locator(dialog_id)
                        assert dialog.evaluate("n => n.scrollWidth <= n.clientWidth + 1"), f"Overflow in {dialog_id} at {width}px"
                        assert dialog.evaluate("n => [...n.querySelectorAll('input,textarea,label,h2')].every(x => getComputedStyle(x).fontStyle === 'normal')")
                        page.locator(cancel).click()
                        settle()
                page.locator("#thingsTab").click()
                menu_action("人员维护")
                assert person_row("测试丙").locator('[name="employee_id"]').input_value() == "00999"
                page.locator("#peopleCancel").click()
                settle()
                assert server.read_data()["tasks"] == original_tasks
                assert not errors, errors
                browser.close()
                print(f"Follow-up config browser checks passed ({engine}); isolated data only.")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
