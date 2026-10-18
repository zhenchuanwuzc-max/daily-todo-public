"""Direct milestone ordering with disposable data and an ephemeral HTTP server."""
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
        server.DATA_DIR = server.APP_SUPPORT_DIR = tmp
        server.DATA_FILE = str(Path(tmp) / "todos.json")
        server.BACKUP_DIR = str(Path(tmp) / "backups")
        server.STATE_FILE = str(Path(tmp) / "state.json")
        server.schedule_sync = lambda: None
        os.environ["TODO_DISABLE_SYNC"] = "1"
        os.environ["TODO_DISABLE_UPDATE"] = "1"
        os.environ["TODO_PREVIEW"] = "1"

        def task(identity, milestone, project="p", **extra):
            return {"id": identity, "text": "事项 " + identity, "owner": "Ocean",
                    "deadline": "2099-10-18", "priority": "P1", "done": False,
                    "created": "2026-10-01T01:00:00Z", "project_id": project,
                    "milestone_id": milestone, **extra}

        fixture = {
            "projects": [{"id": "p", "name": "直接排序项目", "owner": "Ocean", "core": True},
                         {"id": "q", "name": "长列表项目", "owner": "同事"}],
            "milestones": [
                {"id": identity, "name": name, "project_id": "p", "owner": "负责人 " + identity,
                 "due": "2099-10-20", "sort_order": index}
                for index, (identity, name) in enumerate([
                    ("a", "前期准备"), ("hidden", "已完成节点"), ("b", "开发上线"), ("c", "效果运营")])
            ] + [{"id": "deleted", "name": "已删除节点", "project_id": "p", "sort_order": 4,
                  "deleted_at": "2026-10-01T00:00:00Z"}] + [
                {"id": "q" + str(index), "name": "长列表节点 " + str(index), "project_id": "q",
                 "owner": "同事", "sort_order": index} for index in range(18)],
            "tasks": [task("a-task", "a"), task("hidden-task", "hidden", done=True,
                       done_at="2026-10-02T01:00:00Z"), task("b-task", "b"), task("c-task", "c"),
                      task("pending", None), task("deleted-task", "deleted")],
        }
        server.write_data(copy.deepcopy(fixture))
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        assert httpd.server_port != 8766
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch(headless=True)
                page = browser.new_page(viewport={"width": 1200, "height": 900},
                                        locale="zh-CN", timezone_id="Asia/Shanghai")
                errors, requests = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.post_data_json)
                        if request.method == "POST" and request.url.endswith("/milestones/reorder") else None)
                page.add_init_script("localStorage.setItem('tracking-time-range','all')")
                page.goto(url)
                page.locator("#projectsTab").click()
                page.locator('#projectList [data-project-id="p"]').click()

                def section(identity):
                    return page.locator(f'#projectDetail section.milestone[data-milestone-id="{identity}"]')

                def handle(identity):
                    return section(identity).locator(".milestone-drag-handle")

                def order():
                    return page.locator("#projectDetail > section.milestone[data-milestone-id]").evaluate_all(
                        "nodes => nodes.map(node => node.dataset.milestoneId)")

                def stored_order(project="p"):
                    return [m["id"] for m in sorted((m for m in server.read_data()["milestones"]
                            if m["project_id"] == project and not m.get("deleted_at")),
                            key=lambda m: m["sort_order"])]

                def settle():
                    page.evaluate("document.activeElement?.blur()")
                    page.wait_for_function("mutationCount === 0 && !renderPending && !milestoneDrag")

                def begin_drag(source, target, bottom=True):
                    handle(source).scroll_into_view_if_needed()
                    start = handle(source).bounding_box()
                    target_box = section(target).locator(".milestone-head").bounding_box()
                    assert 0 <= target_box["y"] <= 850, target_box
                    page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
                    page.mouse.down()
                    page.mouse.move(target_box["x"] + 70,
                                    target_box["y"] + target_box["height"] * (0.9 if bottom else 0.1), steps=10)
                    assert page.locator(".milestone-drag-preview").count() == 1

                def drop(expected_status=200):
                    with page.expect_response(lambda response: response.url.endswith("/milestones/reorder")) as response:
                        page.mouse.up()
                    assert response.value.status == expected_status
                    settle()

                def assert_draft(draft):
                    assert draft.locator("[name=text]").input_value() == "排序期间保留的草稿"
                    assert draft.locator("[name=owner]").input_value() == "手动填写负责人"
                    assert draft.locator("[name=deadline]").input_value() == "2099-11-22"

                assert order() == ["a", "b", "c"]
                assert handle("a").get_attribute("aria-label") == "拖动里程碑：前期准备"
                assert handle("a").get_attribute("aria-keyshortcuts") == "ArrowUp ArrowDown"
                assert page.locator('.milestone:not([data-milestone-id]) .milestone-drag-handle').count() == 0
                assert page.locator("#projectDetail > section.milestone").last.get_attribute("data-milestone-id") is None
                assert not set(page.locator(".project-actions .action-menu-list button").all_text_contents()) & {
                    "里程碑排序", "恢复项目", "保存为任务模板", "更新任务模板"}
                unchanged_tasks = copy.deepcopy(server.read_data()["tasks"])
                unchanged_other = [m for m in copy.deepcopy(server.read_data()["milestones"]) if m["project_id"] == "q"]

                # A click or sub-threshold gesture has no save and does not open editing.
                start = handle("a").bounding_box()
                page.mouse.move(start["x"] + 8, start["y"] + 8)
                page.mouse.down()
                page.mouse.move(start["x"] + 10, start["y"] + 10)
                assert page.locator(".milestone-drag-preview").count() == 0
                page.mouse.up()
                settle()
                assert not requests and page.locator("#entityDialog").is_hidden()

                # Dragging updates visible order while completed milestones retain their original slot.
                begin_drag("a", "b")
                drop()
                assert order() == ["b", "a", "c"]
                assert requests[-1] == {"project_id": "p", "ids": ["b", "hidden", "a", "c"]}
                assert stored_order() == ["b", "hidden", "a", "c"]
                assert "当前：开发上线" in page.locator(".project-progress").inner_text()
                assert "下一步：事项 b-task" in page.locator(".project-progress").inner_text()
                assert page.locator("#projectDetail > section.milestone").last.get_attribute("data-milestone-id") is None
                page.reload()
                handle("c").wait_for()
                assert order() == ["b", "a", "c"]

                handle("c").focus()
                with page.expect_response(lambda response: response.url.endswith("/milestones/reorder")):
                    page.keyboard.press("ArrowUp")
                page.wait_for_function("mutationCount === 0 && !renderPending")
                assert order() == ["b", "c", "a"]
                assert stored_order() == ["b", "hidden", "c", "a"]
                assert handle("c").evaluate("node => node === document.activeElement")
                handle("b").focus()
                count = len(requests)
                page.keyboard.press("ArrowUp")
                settle()
                assert len(requests) == count, "Moving beyond the first visible milestone must not save"

                # Showing completed milestones makes them draggable without changing their completion.
                page.locator("#showCompletedProjectItems").check()
                begin_drag("hidden", "b", bottom=False)
                drop()
                assert order() == ["hidden", "b", "c", "a"]
                assert section("hidden").locator('input[type=checkbox]').is_checked()
                assert "当前：开发上线" in page.locator(".project-progress").inner_text()
                response = page.request.post(url + "/milestones/reorder", data={"project_id": "p", "ids": ["a", "hidden", "b", "c"]})
                assert response.ok, response.text()
                page.evaluate("readState(true)")
                settle()
                page.locator("#showCompletedProjectItems").uncheck()

                # A draft and a concurrent read/render survive the complete drag/save cycle.
                section("b").locator(".milestone-title").click(button="right")
                page.get_by_role("menuitem", name="新增 1 个事项", exact=True).click()
                draft = section("b").locator("form.milestone-add")
                draft.locator("[name=text]").fill("排序期间保留的草稿")
                draft.locator("[name=owner]").fill("手动填写负责人")
                draft.locator("[name=deadline]").fill("2099-11-22")
                settle()
                begin_drag("a", "b")
                response = page.request.patch(url + "/milestones/c", data={"name": "另一端更新的效果运营"})
                assert response.ok, response.text()
                page.evaluate("readState(true)")
                assert page.evaluate("milestoneDrag !== null && renderPending")
                assert_draft(draft)
                drop()
                assert order() == ["b", "a", "c"]
                assert "另一端更新的效果运营" in section("c").inner_text()
                assert_draft(draft)
                assert server.read_data()["tasks"] == unchanged_tasks

                # Failure restores both DOM and persisted order; editing input is never discarded.
                before, before_bytes = order(), Path(server.DATA_FILE).read_bytes()
                page.route("**/milestones/reorder", lambda route: route.fulfill(status=500, json={"error": "模拟排序失败"}))
                begin_drag("c", "b", bottom=False)
                drop(expected_status=500)
                assert order() == before and Path(server.DATA_FILE).read_bytes() == before_bytes
                assert "未保存" in page.locator("#saveStatus").inner_text()
                assert_draft(draft)
                page.unroute("**/milestones/reorder")

                # Escape and pointer cancellation cancel without requests and flush deferred renders.
                count = len(requests)
                begin_drag("c", "b", bottom=False)
                page.evaluate("requestRender()")
                page.keyboard.press("Escape")
                page.mouse.up()
                settle()
                assert order() == before and len(requests) == count
                assert Path(server.DATA_FILE).read_bytes() == before_bytes
                assert_draft(draft)
                begin_drag("c", "b", bottom=False)
                page.evaluate("document.dispatchEvent(new PointerEvent('pointercancel', {pointerId:milestoneDrag.pointerId,bubbles:true}))")
                page.mouse.up()
                settle()
                assert order() == before and len(requests) == count
                assert_draft(draft)

                assert server.read_data()["tasks"] == unchanged_tasks
                assert [m for m in server.read_data()["milestones"] if m["project_id"] == "q"] == unchanged_other
                assert next(m for m in server.read_data()["milestones"] if m["id"] == "deleted") == fixture["milestones"][4]

                # Auto-scroll works in a long project; cancel leaves its order untouched.
                page.locator('#projectList [data-project-id="q"]').click()
                handle("q0").scroll_into_view_if_needed()
                initial_scroll = page.evaluate("scrollY")
                start = handle("q0").bounding_box()
                page.mouse.move(start["x"] + 8, start["y"] + 8)
                page.mouse.down()
                page.mouse.move(start["x"] + 30, 890, steps=12)
                page.wait_for_function("initial => scrollY > initial + 150", arg=initial_scroll)
                assert page.locator(".milestone-drag-preview").count() == 1
                page.keyboard.press("Escape")
                page.mouse.up()
                settle()
                assert stored_order("q") == ["q" + str(index) for index in range(18)]
                assert len(requests) == count
                page.locator('#projectList [data-project-id="p"]').click()
                assert_draft(draft)

                # Native-sized windows keep every inline field reachable without horizontal
                # scrolling; the handle remains keyboard-operable and persists after reload.
                for width in (775, 790, 696):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.wait_for_function("width => innerWidth === width && !renderPending", arg=width)
                    layout = page.evaluate("""() => ({
                        viewport:innerWidth, page:document.documentElement.scrollWidth,
                        rows:[...document.querySelectorAll('#projectDetail .inline-task')].map(row => ({
                            id:row.dataset.id,
                            fields:['.drag-handle','input[type=checkbox]','.text','.person-picker',
                                    '.deadline-wrap','.pri-select','.row-delete'].map(selector => {
                                const node=row.querySelector(selector),box=node.getBoundingClientRect();
                                return {selector,left:box.left,right:box.right,top:box.top,
                                        bottom:box.bottom,width:box.width,height:box.height};
                            })
                        }))
                    })""")
                    assert layout["page"] <= width + 1, (width, layout)
                    for row in layout["rows"]:
                        fields = row["fields"]
                        assert all(field["width"] > 0 and field["height"] > 0
                                   and field["left"] >= 0 and field["right"] <= width + 1
                                   for field in fields), (width, row)
                        assert max(field["top"] for field in fields) < min(field["bottom"] for field in fields), (width, row)
                        assert all(left["right"] <= right["left"] + 1
                                   for left, right in zip(fields, fields[1:])), (width, row)
                    previous = order()
                    moved = previous[-1]
                    expected = previous[:-2] + [previous[-1], previous[-2]]
                    handle(moved).focus()
                    with page.expect_response(lambda response: response.url.endswith("/milestones/reorder")) as response:
                        page.keyboard.press("ArrowUp")
                    assert response.value.ok
                    page.wait_for_function("mutationCount === 0 && !renderPending")
                    assert order() == expected, (width, order())
                    assert handle(moved).evaluate("node => node === document.activeElement")
                    assert stored_order() == [expected[0], "hidden", *expected[1:]]
                    page.reload()
                    handle(moved).wait_for()
                    assert order() == expected, (width, order())
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
                assert server.read_data()["tasks"] == unchanged_tasks
                assert [m for m in server.read_data()["milestones"] if m["project_id"] == "q"] == unchanged_other
                assert not errors, errors
                print(f"PASS {engine}: milestone pointer/keyboard ordering, hidden completed slots, pending last, reload/current-next, threshold, draft/concurrent-render preservation, failure rollback, Escape/pointercancel, isolated scope, auto-scroll and 696/775/790px inline layout+keyboard persistence")
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
