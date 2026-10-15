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
                page.goto(url)
                page.locator("#groups [data-id=legacy]").wait_for()
                page.locator("#projectsTab").click()
                page.locator(f'#projectList [data-project-id="{project["id"]}"]').click()

                def settle():
                    page.evaluate("document.activeElement?.blur()")
                    page.wait_for_function("mutationCount === 0 && !renderPending")

                def menu_action(name):
                    menu = page.locator(".project-actions details.action-menu")
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
                menu_action("人员工号")
                assert page.locator("#peopleList .person-row").count() == 3
                assert person_row("测试甲").locator('[name="name"]').get_attribute("readonly") is not None
                assert person_row("Ocean").locator('[name="employee_id"]').input_value() == ""
                page.locator("#peopleCancel").click()
                settle()
                assert Path(server.DATA_FILE).read_bytes() == snapshot
                menu_action("人员工号")
                person_row("测试甲").locator('[name="employee_id"]').fill("00123")
                page.locator("#addPersonBtn").click()
                added = page.locator("#peopleList .person-row").last
                added.locator('[name="name"]').fill("测试丙")
                added.locator('[name="employee_id"]').fill("00999")
                save("#peopleDialog", "#peopleSave")
                assert person("测试甲")["employee_id"] == "00123"
                assert person("测试丙")["employee_id"] == "00999"
                assert server.read_data()["tasks"] == original_tasks

                # Slow/failed persistence locks controls, keeps the draft and permits retry.
                menu_action("人员工号")
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
                menu_action("人员工号")
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
                menu_action("人员工号")
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

                # Configuration stays accessible when no active projects exist.
                for value in (project, other):
                    response = context.request.patch(url + "/projects/" + value["id"], data={"archived": True})
                    assert response.ok
                page.reload()
                for width in (775, 1013):
                    page.set_viewport_size({"width": width, "height": 774})
                    for label, dialog_id, cancel in (("人员工号", "#peopleDialog", "#peopleCancel"), ("周报接收群", "#weeklyReportGroupsDialog", "#weeklyReportGroupsCancel")):
                        menu_action(label)
                        dialog = page.locator(dialog_id)
                        assert dialog.evaluate("n => n.scrollWidth <= n.clientWidth + 1"), f"Overflow in {dialog_id} at {width}px"
                        assert dialog.evaluate("n => [...n.querySelectorAll('input,textarea,label,h2')].every(x => getComputedStyle(x).fontStyle === 'normal')")
                        page.locator(cancel).click()
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
