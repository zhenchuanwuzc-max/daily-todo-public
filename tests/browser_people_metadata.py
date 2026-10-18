"""Directory enrichment and alias-aware owner picking on a disposable local store."""
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
    os.environ["TODO_DISABLE_SYNC"] = "1"
    os.environ["TODO_DISABLE_UPDATE"] = "1"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        server.DATA_DIR = server.APP_SUPPORT_DIR = str(root)
        server.DATA_FILE = str(root / "todos.json")
        server.BACKUP_DIR = str(root / "backups")
        server.STATE_FILE = str(root / "state.json")
        server.schedule_sync = lambda: None
        alice = {"id": "alice", "name": "佳禾", "employee_id": "00007", "chinese_name": "陈佳禾",
                 "english_name": "Alice Chen", "aliases": ["小禾", "佳禾花名"], "dingtalk_user_id": "u00007"}
        bob = {"id": "bob", "name": "栗子", "employee_id": "00008", "chinese_name": "李佳",
               "english_name": "Brian Li", "aliases": ["小栗"], "dingtalk_user_id": "u00008"}
        tasks = [
            {"id": "alias-task", "text": "花名负责人事项", "owner": "小禾", "done": False,
             "priority": "P1", "deadline": "2099-12-31", "project_id": "p"},
            {"id": "legacy-task", "text": "旧版人员事项", "owner": "Ocean", "done": False,
             "priority": "P1", "deadline": "2099-12-31", "project_id": "p"},
        ]
        server.write_data({"projects": [{"id": "p", "name": "隔离测试项目", "owner": "Ocean"}],
                           "milestones": [], "tasks": tasks,
                           "people": [{"id": "ocean", "name": "Ocean", "employee_id": ""}, alice, bob,
                                      {"id": "old-alias", "name": "小禾", "employee_id": "00007",
                                       "deleted_at": "2026-10-16T12:00:00Z", "merged_into": "alice"}]})
        original_tasks = copy.deepcopy(server.read_data()["tasks"])
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        assert http.server_port != 8766
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch(headless=True)
                context = browser.new_context(viewport={"width": 1013, "height": 900},
                                              locale="zh-CN", timezone_id="Asia/Shanghai")
                page = context.new_page()
                errors, submitted = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: submitted.append(request.post_data_json)
                        if request.method == "POST" and request.url.endswith("/people/upsert") else None)
                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                initial_bytes = Path(server.DATA_FILE).read_bytes()
                page.goto(url)
                page.locator("#groups [data-id=alias-task]").wait_for()

                def settle():
                    page.wait_for_function("mutationCount === 0 && !renderPending")

                def person(name):
                    return next(record for record in server.read_data()["people"] if record["name"] == name)

                def task(identity="alias-task"):
                    return next(record for record in server.read_data()["tasks"] if record["id"] == identity)

                def person_row(name):
                    return page.locator(f'#peopleList .person-row[data-person-name="{name}"]')

                def open_people():
                    menu = page.locator("#globalSettings")
                    if not menu.evaluate("node => node.open"):
                        menu.locator("summary").click()
                    menu.get_by_role("button", name="人员维护", exact=True).click()
                    page.locator("#peopleDialog").wait_for(state="visible")

                def save_people():
                    page.locator("#peopleSave").click()
                    page.locator("#peopleDialog").wait_for(state="hidden")
                    settle()

                def cancel_people():
                    page.locator("#peopleCancel").click()
                    page.locator("#peopleDialog").wait_for(state="hidden")
                    settle()

                # Unique aliases do not generate duplicate people or migrate stored task owners.
                assert task()["owner"] == "小禾"
                assert page.locator("#groups [data-id=alias-task] .owner-input").input_value() == "小禾"
                assert sorted(page.evaluate("knownPeople().map(person => person.name)")) == sorted(["Ocean", "佳禾", "栗子"])
                assert Path(server.DATA_FILE).read_bytes() == initial_bytes
                open_people()
                assert page.locator("#peopleList .person-row").count() == 3
                assert page.locator("#peopleDialog .people-columns span").all_text_contents() == ["中文名", "英文名", "工号"]
                assert page.locator("#peopleList [name=name]:visible").count() == 0
                for name in ("Ocean", "佳禾", "栗子"):
                    assert person_row(name).locator("[name=name]").get_attribute("type") == "hidden"
                    assert person_row(name).locator("input:visible").evaluate_all("nodes => nodes.map(node => node.name)") == ["chinese_name", "english_name", "employee_id"]
                assert person_row("Ocean").locator("[name=chinese_name]").input_value() == ""
                assert person_row("Ocean").locator("[name=english_name]").input_value() == ""
                cancel_people()
                assert Path(server.DATA_FILE).read_bytes() == initial_bytes

                # Enrich legacy records; IDs retain leading zeros and owner strings remain intact.
                open_people()
                person_row("Ocean").locator("[name=chinese_name]").fill("吴振川")
                person_row("Ocean").locator("[name=english_name]").fill("Ocean Wu")
                person_row("Ocean").locator("[name=employee_id]").fill("00123")
                save_people()
                ocean = person("Ocean")
                assert (ocean["chinese_name"], ocean["english_name"], ocean["employee_id"]) == ("吴振川", "Ocean Wu", "00123")
                assert server.read_data()["tasks"] == original_tasks
                assert "aliases" not in ocean and "dingtalk_user_id" not in ocean
                assert submitted[-1]["people"] == [{"name": "Ocean", "employee_id": "00123", "base_employee_id": "",
                                                     "chinese_name": "吴振川", "base_chinese_name": "",
                                                     "english_name": "Ocean Wu", "base_english_name": ""}]
                open_people()
                person_row("Ocean").locator("[name=english_name]").fill("")
                save_people()
                assert person("Ocean")["english_name"] == ""
                assert person("Ocean")["chinese_name"] == "吴振川"
                assert set(submitted[-1]["people"][0]) == {"name", "employee_id", "base_employee_id", "english_name", "base_english_name"}

                # A failed metadata-only save preserves the input and succeeds on retry.
                open_people()
                person_row("Ocean").locator("[name=english_name]").fill("Ocean Q Wu")
                page.route("**/people/upsert", lambda route: route.fulfill(
                    status=500, content_type="application/json", body=json.dumps({"error": "隔离测试失败"})))
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleError').textContent.length > 0 && document.querySelector('#peopleForm').dataset.saving !== 'true'")
                assert page.locator("#peopleDialog").is_visible()
                assert person_row("Ocean").locator("[name=english_name]").input_value() == "Ocean Q Wu"
                assert person("Ocean")["english_name"] == ""
                page.unroute("**/people/upsert")
                save_people()
                assert person("Ocean")["english_name"] == "Ocean Q Wu"

                # Unchanged metadata is omitted, preserving a concurrent alias correction.
                open_people()
                person_row("佳禾").locator("[name=chinese_name]").fill("陈嘉禾")
                response = context.request.post(url + "/people/upsert", data={"people": [{
                    "name": "佳禾", "employee_id": "00007", "base_employee_id": "00007",
                    "aliases": ["小禾", "合作花名"], "base_aliases": ["小禾", "佳禾花名"]}]})
                assert response.ok, response.text()
                save_people()
                assert person("佳禾")["chinese_name"] == "陈嘉禾"
                assert person("佳禾")["aliases"] == ["小禾", "合作花名"]
                assert person("佳禾")["dingtalk_user_id"] == "u00007"
                assert set(submitted[-1]["people"][0]) == {"name", "employee_id", "base_employee_id", "chinese_name", "base_chinese_name"}
                assert server.read_data()["tasks"] == original_tasks

                # Stale changes to the same field return a conflict and retain unsaved text.
                open_people()
                person_row("Ocean").locator("[name=english_name]").fill("Local Unsaved Name")
                response = context.request.post(url + "/people/upsert", data={"people": [{
                    "name": "Ocean", "employee_id": "00123", "base_employee_id": "00123",
                    "english_name": "Ocean Remote", "base_english_name": "Ocean Q Wu"}]})
                assert response.ok, response.text()
                before_conflict = Path(server.DATA_FILE).read_bytes()
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleError').textContent.includes('刷新') && document.querySelector('#peopleForm').dataset.saving !== 'true'")
                assert person_row("Ocean").locator("[name=english_name]").input_value() == "Local Unsaved Name"
                assert person("Ocean")["english_name"] == "Ocean Remote"
                assert Path(server.DATA_FILE).read_bytes() == before_conflict
                cancel_people()
                page.reload()
                page.locator("#groups [data-id=alias-task]").wait_for()
                assert task()["owner"] == "小禾"
                open_people()
                assert person_row("Ocean").locator("[name=english_name]").input_value() == "Ocean Remote"
                assert person_row("Ocean").locator("[name=employee_id]").input_value() == "00123"
                for width in (775, 1013):
                    page.set_viewport_size({"width": width, "height": 900})
                    assert page.locator("#peopleDialog").evaluate("node => node.scrollWidth <= node.clientWidth + 1")
                directory_shot = f"/tmp/daily-todo-people-metadata-{engine}.png"
                page.screenshot(path=directory_shot, full_page=True)
                cancel_people()

                # Chinese / English / alias / employee-ID searches all resolve to one directory person.
                row = page.locator("#groups [data-id=alias-task]")
                row.locator(".person-picker-toggle").click()
                popup = page.locator(".person-options")
                options = popup.get_by_role("listbox", name="选择负责人")
                assert options.get_by_role("option").count() == 3
                assert options.get_by_role("option", name="佳禾", exact=True).get_attribute("aria-selected") == "true"
                assert "陈嘉禾" in options.get_by_role("option", name="佳禾", exact=True).inner_text()
                assert "Alice Chen" in options.get_by_role("option", name="佳禾", exact=True).inner_text()
                assert "00007" in options.get_by_role("option", name="佳禾", exact=True).inner_text()
                assert options.get_by_role("option", name="佳禾", exact=True).locator(".person-name").inner_text() == "陈嘉禾 · Alice Chen"
                for query in ("陈嘉", "aLiCe", "合作花名", "00007"):
                    popup.get_by_label("筛选负责人").fill(query)
                    assert options.get_by_role("option").count() == 1
                    assert options.get_by_role("option", name="佳禾", exact=True).is_visible()
                assert task()["owner"] == "小禾", "Opening/filtering must not rewrite an alias owner"
                picker_shot = f"/tmp/daily-todo-people-picker-{engine}.png"
                page.screenshot(path=picker_shot, full_page=True)
                page.keyboard.press("Escape")
                assert task()["owner"] == "小禾"

                # Confirmation canonicalizes unique aliases; multi-select has no duplicate people.
                row.locator(".person-picker-toggle").click()
                options.get_by_role("option", name="栗子", exact=True).click()
                assert task()["owner"] == "小禾"
                popup.get_by_role("button", name="确认负责人", exact=True).click()
                settle()
                assert task()["owner"] == "佳禾、栗子"
                field = row.locator(".owner-input")
                field.fill("小禾、佳禾")
                assert options.get_by_role("option", name="佳禾", exact=True).get_attribute("aria-selected") == "true"
                popup.get_by_role("button", name="确认负责人", exact=True).click()
                settle()
                assert task()["owner"] == "佳禾", {"stored": task()["owner"], "input": field.input_value(),
                                                    "drafts": page.evaluate("[...fieldDrafts]"),
                                                    "pickerOpen": page.locator(".person-options").count()}
                assert sorted(page.evaluate("knownPeople().map(person => person.name)")) == sorted(["Ocean", "佳禾", "栗子"])
                field.fill("自由填写同事")
                assert popup.locator(".person-no-match").is_visible()
                field.press("Enter")
                settle()
                assert task()["owner"] == "自由填写同事"
                assert task("legacy-task")["owner"] == "Ocean"

                # Adding a known full name must not create a second row for the same identity.
                open_people()
                page.locator("#addPersonBtn").click()
                duplicate = page.locator("#peopleList .person-row").last
                duplicate.locator("[name=chinese_name]").fill("陈嘉禾")
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleError').textContent.includes('姓名重复')")
                assert not any(record["name"] == "陈嘉禾" for record in server.read_data()["people"])
                cancel_people()

                # New people need only Chinese or English names, with no separate common-name control.
                open_people()
                page.locator("#addPersonBtn").click()
                empty = page.locator("#peopleList .person-row").last
                empty.locator("[name=employee_id]").fill("00090")
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleError').textContent.length > 0")
                assert empty.locator("[name=employee_id]").input_value() == "00090"
                assert not any(record["employee_id"] == "00090" for record in server.read_data()["people"])
                cancel_people()

                open_people()
                original_rows = page.locator("#peopleList .person-row").count()
                page.locator("#addPersonBtn").click()
                chinese = page.locator("#peopleList .person-row").nth(original_rows)
                chinese.locator("[name=chinese_name]").fill("林小新")
                chinese.locator("[name=english_name]").fill("Lynn Lin")
                chinese.locator("[name=employee_id]").fill("00091")
                page.locator("#addPersonBtn").click()
                english = page.locator("#peopleList .person-row").nth(original_rows + 1)
                english.locator("[name=english_name]").fill("Robin Example")
                assert page.locator("#peopleList [name=name]:visible").count() == 0
                page.route("**/people/upsert", lambda route: route.fulfill(
                    status=500, content_type="application/json", body=json.dumps({"error": "新增隔离失败"})))
                page.locator("#peopleSave").click()
                page.wait_for_function("document.querySelector('#peopleError').textContent.length > 0 && document.querySelector('#peopleForm').dataset.saving !== 'true'")
                assert chinese.locator("[name=chinese_name]").input_value() == "林小新"
                assert chinese.locator("[name=english_name]").input_value() == "Lynn Lin"
                assert chinese.locator("[name=employee_id]").input_value() == "00091"
                assert english.locator("[name=english_name]").input_value() == "Robin Example"
                assert not any(record["name"] in ("林小新", "Robin Example") for record in server.read_data()["people"])
                page.unroute("**/people/upsert")
                save_people()
                assert person("林小新")["chinese_name"] == "林小新"
                assert person("林小新")["english_name"] == "Lynn Lin"
                assert person("林小新")["employee_id"] == "00091"
                assert person("Robin Example")["english_name"] == "Robin Example"
                assert person("Robin Example")["chinese_name"] == ""
                assert person("Robin Example")["employee_id"] == ""
                assert {record["name"] for record in submitted[-1]["people"]} == {"林小新", "Robin Example"}
                page.reload()
                page.locator("#groups [data-id=alias-task]").wait_for()
                open_people()
                assert person_row("林小新").locator("[name=chinese_name]").input_value() == "林小新"
                assert person_row("Robin Example").locator("[name=english_name]").input_value() == "Robin Example"
                assert page.locator("#peopleList [name=name]:visible").count() == 0
                page.screenshot(path=f"/tmp/daily-todo-people-three-columns-{engine}.png", full_page=True)
                cancel_people()
                assert not errors, errors
                browser.close()
                print(f"PASS {engine}: metadata enrich/clear/leading-zero ID/partial payload/failure retry/conflict retention/alias-safe owners/four-way search/multi-select dedup/free text/three-column UI/Chinese-or-English new identity")
                print(f"Screenshots: {directory_shot}; {picker_shot}")
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
