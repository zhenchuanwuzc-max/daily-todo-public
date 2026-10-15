"""Recurring-task management checks in a disposable store; never uses live data."""
import copy
from datetime import date, timedelta
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
    output = Path(os.environ.get("TODO_BROWSER_OUTPUT", "/tmp/daily-todo-recurring-browser"))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR = tmp
        server.DATA_FILE = str(Path(tmp) / "todos.json")
        server.BACKUP_DIR = str(Path(tmp) / "backups")
        server.APP_SUPPORT_DIR = tmp
        server.STATE_FILE = str(Path(tmp) / "state.json")
        server.schedule_sync = lambda: None
        today = date.fromisoformat(server.today_str())
        future_date = str(today + timedelta(days=7))
        server.write_data({"tasks": [
            {"id": "ordinary-now", "text": "普通事项", "priority": "P1", "done": False,
             "created": "2026-01-01T10:00:00"},
            {"id": "ordinary-future", "text": "未来计划事项", "priority": "P1", "done": False,
             "created": "2026-01-02T10:00:00", "due": future_date},
        ]})
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch(headless=True)
                context = browser.new_context(viewport={"width": 1013, "height": 774}, locale="zh-CN", timezone_id="Asia/Shanghai")
                page = context.new_page()
                errors, console_errors = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: console_errors.append((message.text, message.location.get("url", ""))) if message.type == "error" else None)
                page.on("dialog", lambda dialog: dialog.accept())

                def active_seeds():
                    return [task for task in server.read_data()["tasks"] if task.get("recurring") in ("daily", "weekly", "monthly") and not task.get("recur_source") and not task.get("deleted_at")]

                def seed_named(text):
                    return next(seed for seed in active_seeds() if seed["text"] == text)

                def instances(seed_id):
                    return [task for task in server.read_data()["tasks"] if task.get("recur_source") == seed_id]

                def idle():
                    page.wait_for_function("mutationCount === 0")

                def open_recurring():
                    page.locator("#recurringBtn").click()
                    page.locator("#recurringModal").wait_for(state="visible")

                def close_recurring():
                    page.keyboard.press("Escape")
                    page.locator("#recurringModal").wait_for(state="hidden")
                    page.wait_for_function("document.activeElement === document.getElementById('recurringBtn')")
                    page.wait_for_function("mutationCount === 0 && !renderPending")

                def open_add():
                    if not page.locator("#recurAddForm").is_visible():
                        page.locator("#recurAddToggle").click()
                    page.locator("#recurNewText").wait_for(state="visible")

                def refresh_state():
                    page.evaluate("async () => { await readState(true); }")

                page.goto(url)
                page.locator('#groups [data-id="ordinary-future"]').wait_for()
                # Planned dates remain stored, while future ordinary tasks join the sortable list.
                assert page.locator('#groups [data-id="ordinary-now"]').count() == 1
                future_handle = page.locator('#groups [data-id="ordinary-future"] .drag-handle')
                future_handle.focus()
                with page.expect_response(lambda response: response.url.endswith("/todos/reorder")) as response:
                    page.keyboard.press("ArrowDown")
                assert response.value.ok
                idle()
                assert page.locator("#group-P1 .inline-task").evaluate_all("rows => rows.map(row => row.dataset.id)") == ["ordinary-now", "ordinary-future"]
                assert next(task for task in server.read_data()["tasks"] if task["id"] == "ordinary-future")["due"] == future_date

                # Empty state has a clear title and an accessible creation entry point.
                open_recurring()
                assert page.locator("#recurringTitle").inner_text() == "重复任务"
                assert "0" in page.locator("#recurringSummary").inner_text()
                assert page.locator("#recurSeeds .recur-seed[data-id]").count() == 0
                assert page.locator("#recurSeeds").inner_text().strip()
                open_add()
                assert page.locator("#recurNewText").evaluate("node => document.activeElement === node")
                close_recurring()

                # Existing daily, weekly and monthly templates retain their schedules.
                specifications = [
                    {"text": "已有每日模板", "recurring": "daily"},
                    {"text": "已有每周模板", "recurring": "weekly", "recur_weekday": today.isoweekday()},
                    {"text": "已有每月模板", "recurring": "monthly", "recur_monthday": today.day},
                ]
                for payload in specifications:
                    response = context.request.post(url + "/recurring/add", data=payload)
                    assert response.ok, response.text()
                assert context.request.post(url + "/ensure-recurring").ok
                daily = seed_named("已有每日模板")
                history = {"id": f'recur:{daily["id"]}:{today - timedelta(days=1)}', "text": daily["text"],
                           "recur_source": daily["id"], "done": True, "done_at": str(today - timedelta(days=1)) + "T12:00:00+08:00",
                           "created": str(today - timedelta(days=1)) + "T09:00:00+08:00", "source": "recur", "priority": "P1"}
                data = server.read_data()
                data["tasks"].append(history)
                server.write_data(data)
                page.reload()
                open_recurring()
                assert page.locator("#recurSeeds .recur-seed[data-id]").count() == 3
                assert "3" in page.locator("#recurringSummary").inner_text()
                assert "每天" in page.locator(f'#recurSeeds .recur-seed[data-id="{daily["id"]}"]').inner_text()
                assert "每周" in page.locator(f'#recurSeeds .recur-seed[data-id="{seed_named("已有每周模板")["id"]}"]').inner_text()
                assert f"每月 {today.day} 日" in page.locator(f'#recurSeeds .recur-seed[data-id="{seed_named("已有每月模板")["id"]}"]').inner_text()
                for seed in active_seeds():
                    assert page.locator(f'#groups [data-id="{seed["id"]}"]').count() == 0

                # A matching weekly template creates today's instance immediately, without reloading.
                open_add()
                page.locator("#recurNewText").fill("本日新增周计划")
                page.locator("#recurNewFrequency").select_option("weekly")
                assert page.locator("#recurNewWeekday").is_visible()
                assert page.locator("#recurNewMonthday").is_hidden()
                page.locator("#recurNewWeekday").select_option(str(today.isoweekday()))
                page.locator("#recurNewPri").select_option("P0")
                page.locator("#recurNewTag").select_option("work")
                page.locator("#recurAddBtn").click()
                page.wait_for_function("mutationCount === 0 && document.querySelector('#recurNewText').value === ''")
                added_seed = seed_named("本日新增周计划")
                assert added_seed["recur_weekday"] == today.isoweekday()
                assert added_seed["priority"] == "P0" and added_seed["tag"] == "work"
                assert page.locator(f'#recurSeeds .recur-seed[data-id="{added_seed["id"]}"]').count() == 1
                assert len(instances(added_seed["id"])) == 1
                close_recurring()
                instance_id = f'recur:{added_seed["id"]}:{today}'
                page.locator(f'#groups [data-id="{instance_id}"]').wait_for()
                assert page.locator(f'#groups [data-id="{added_seed["id"]}"]').count() == 0
                before = copy.deepcopy(server.read_data()["tasks"])
                for _ in range(2):
                    assert context.request.post(url + "/ensure-recurring").ok
                assert server.read_data()["tasks"] == before, "Ensuring repeatedly must neither duplicate nor alter existing instances"

                # Refreshes preserve the form node and the user's draft, including schedule choices.
                open_recurring()
                open_add()
                page.locator("#recurNewText").fill("保存失败后重试的月计划")
                page.locator("#recurNewFrequency").select_option("monthly")
                assert page.locator("#recurNewWeekday").is_hidden()
                page.locator("#recurNewMonthday").select_option(str(today.day))
                page.locator("#recurNewPri").select_option("P2")
                page.locator("#recurNewTag").select_option("startup")
                page.evaluate("window.__recurDraftNode = document.getElementById('recurNewText')")
                refresh_state()
                page.evaluate("document.activeElement?.blur()")
                refresh_state()
                assert page.evaluate("window.__recurDraftNode === document.getElementById('recurNewText')")
                assert page.locator("#recurNewText").input_value() == "保存失败后重试的月计划"
                assert page.locator("#recurNewFrequency").input_value() == "monthly"
                assert page.locator("#recurNewMonthday").input_value() == str(today.day)
                assert page.locator("#recurNewPri").input_value() == "P2"
                assert page.locator("#recurNewTag").input_value() == "startup"

                # A held double click sends once; failure retains all input, and retry creates once.
                held = []
                page.route("**/recurring/add", lambda route: held.append(route))
                page.locator("#recurAddBtn").evaluate("button => { button.click(); button.click(); }")
                page.wait_for_function("document.getElementById('recurAddBtn').disabled")
                assert len(held) == 1, "A pending add must suppress the duplicate click"
                held[0].fulfill(status=500, json={"error": "模拟重复任务保存失败"})
                page.wait_for_function("mutationCount === 0 && !document.getElementById('recurAddBtn').disabled")
                assert page.locator("#recurNewText").input_value() == "保存失败后重试的月计划"
                assert page.locator("#recurNewFrequency").input_value() == "monthly"
                assert page.locator("#recurNewMonthday").input_value() == str(today.day)
                assert page.locator("#recurError").inner_text().strip()
                assert not any(seed["text"] == "保存失败后重试的月计划" for seed in active_seeds())
                page.unroute("**/recurring/add")
                page.locator("#recurAddBtn").click()
                page.wait_for_function("mutationCount === 0 && document.getElementById('recurNewText').value === ''")
                retry_seed = seed_named("保存失败后重试的月计划")
                assert len([seed for seed in active_seeds() if seed["text"] == retry_seed["text"]]) == 1
                assert len(instances(retry_seed["id"])) == 1
                assert page.locator("#recurError").inner_text() == ""

                # Removing the template keeps both today's instance and previously completed history.
                preserved_instances = copy.deepcopy(instances(daily["id"]))
                assert len(preserved_instances) == 2
                page.locator(f'#recurSeeds .recur-seed[data-id="{daily["id"]}"] [data-action="del-seed"]').click()
                page.wait_for_function("mutationCount === 0")
                page.locator(f'#recurSeeds .recur-seed[data-id="{daily["id"]}"]').wait_for(state="detached")
                assert instances(daily["id"]) == preserved_instances
                assert next(task for task in server.read_data()["tasks"] if task["id"] == daily["id"])["deleted_at"]

                # Narrow and regular layouts keep the template list and expanded form within the modal.
                open_add()
                for width in (775, 1013):
                    page.set_viewport_size({"width": width, "height": 774})
                    for frequency in ("daily", "weekly", "monthly"):
                        page.locator("#recurNewFrequency").select_option(frequency)
                        card = page.locator("#recurringModal .modal-card")
                        assert card.evaluate("node => node.scrollWidth <= node.clientWidth + 1"), (engine, width, frequency)
                        assert page.locator("#recurAddForm").evaluate("node => node.scrollWidth <= node.clientWidth + 1"), (engine, width, frequency)
                        bounds = card.bounding_box()
                        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1, bounds
                    page.screenshot(path=str(output / f"recurring-{engine}-{width}.png"))
                close_recurring()
                assert next(task for task in server.read_data()["tasks"] if task["id"] == "ordinary-future")["due"] == future_date
                assert not errors, errors
                unexpected_console_errors = [(message, location) for message, location in console_errors
                                             if not ("500" in message and "/recurring/add" in location)
                                             and not location.endswith("/favicon.ico")]
                assert not unexpected_console_errors, unexpected_console_errors
                browser.close()
                print(f"PASS {engine}: recurring empty/existing schedules, due-preserving main-list reorder, immediate matching instance, idempotency, draft-safe refresh, double-click protection, failed retry, history-preserving deletion, Escape/focus, 775/1013px modal layout; isolated data only")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
