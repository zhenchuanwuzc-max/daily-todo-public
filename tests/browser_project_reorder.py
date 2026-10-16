"""Project task ordering acceptance in an isolated store and ephemeral server."""
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
        today = date.fromisoformat(server.today_str())

        def task(identity, index, **extra):
            return {"id": identity, "text": "事项 " + identity, "owner": "Ocean", "done": False,
                    "project_id": "p", "milestone_id": "m", "priority": ["P0", "P2", "P1"][index % 3],
                    "deadline": (today + timedelta(days=index)).isoformat(),
                    "created": "2026-10-01T00:00:00Z", "sort_order": index, **extra}

        fixture = {"projects": [{"id": "p", "name": "排序验收项目", "owner": "Ocean"}],
                   "milestones": [{"id": "m", "project_id": "p", "name": "第一里程碑", "sort_order": 0},
                                  {"id": "other", "project_id": "p", "name": "第二里程碑", "sort_order": 1},
                                  {"id": "deleted", "project_id": "p", "name": "已删除里程碑", "deleted_at": "2026-10-01T00:00:00Z"}],
                   "tasks": [task("t0", 0), task("t1", 1), task("t2", 2), task("t3", 3),
                             task("done", 4, done=True, done_at="2026-10-02T00:00:00Z"),
                             task("other-task", 5, milestone_id="other"),
                             task("u0", 6, milestone_id=None), task("u1", 7, milestone_id="deleted")]}
        server.write_data(copy.deepcopy(fixture))
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        assert httpd.server_port != 8766
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch(headless=True)
                page = browser.new_page(viewport={"width": 1200, "height": 960}, locale="zh-CN", timezone_id="Asia/Shanghai")
                errors, reorders = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept())
                page.on("request", lambda request: reorders.append(request.post_data_json)
                        if request.method == "POST" and request.url.endswith("/todos/reorder") else None)
                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                page.goto(url)
                page.locator('#groups li[data-id="t0"]').wait_for()
                thing_order = page.locator("#groups li").evaluate_all("rows => rows.map(row => row.dataset.id)")
                page.locator('[data-tab="projects"]').click()
                page.locator('#projectList [data-project-id="p"]').click()
                scope = '#projectDetail ul[data-project-id="p"][data-milestone-id="m"]'
                page.locator(scope + ' .drag-handle').first.wait_for()

                def order(selector=scope):
                    return page.locator(selector + " > li").evaluate_all("rows => rows.map(row => row.dataset.id)")

                def settle():
                    page.wait_for_function("mutationCount === 0 && !renderPending && !taskDrag")

                def row(identity):
                    return page.locator('#projectDetail li[data-id="' + identity + '"]')

                def drag(source, target, bottom=True, save=True):
                    handle = row(source).locator('.drag-handle')
                    handle.scroll_into_view_if_needed()
                    start, end = handle.bounding_box(), row(target).bounding_box()
                    styles = """node => ['.text', '.person-picker', '.owner-input', '.deadline-wrap', '.deadline-input'].map(selector => {
                        const child = node.querySelector(selector), style = getComputedStyle(child);
                        return {selector, width:child.getBoundingClientRect().width, font:style.fontSize, padding:style.padding};
                    })"""
                    source_styles = row(source).evaluate(styles)
                    page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
                    page.mouse.down()
                    # The prior drop can still be fading while the next drag starts.
                    preview = page.locator('.task-drag-preview.project-task-preview').last
                    preview_styles = preview.evaluate(styles)
                    horizontal_border = "node => {const style=getComputedStyle(node);return parseFloat(style.borderLeftWidth)+parseFloat(style.borderRightWidth)}"
                    border_delta = preview.evaluate(horizontal_border) - row(source).evaluate(horizontal_border)
                    for actual, expected in zip(preview_styles, source_styles):
                        # The floating preview adds a 1px outline border on each
                        # side; only the flexible text column absorbs that space.
                        expected_width = expected['width'] - (border_delta if actual['selector'] == '.text' else 0)
                        assert abs(actual['width'] - expected_width) < 1, (actual, expected)
                        assert actual['font'] == expected['font'] and actual['padding'] == expected['padding'], (actual, expected)
                    assert preview.evaluate("node => node.classList.contains('done')") == row(source).evaluate("node => node.classList.contains('done')")
                    page.mouse.move(end["x"] + 20, end["y"] + end["height"] * (0.9 if bottom else 0.1), steps=12)
                    if save:
                        with page.expect_response(lambda response: response.url.endswith('/todos/reorder')) as response:
                            page.mouse.up()
                        assert response.value.status == 200
                    else:
                        page.mouse.up()
                    settle()

                assert order() == ['t0', 't1', 't2', 't3'], order()
                assert "同一里程碑" in row('t0').locator('.drag-handle').get_attribute('title')
                drag('t0', 't2')
                assert order() == ['t1', 't2', 't0', 't3'], order()
                assert reorders[-1] == {"ids": ['t1', 't2', 't0', 't3'], "scope": "project", "project_id": "p", "milestone_id": "m"}
                page.reload()
                page.locator(scope + ' .drag-handle').first.wait_for()
                assert order() == ['t1', 't2', 't0', 't3'], order()
                drag('t3', 't1', False)
                assert order() == ['t3', 't1', 't2', 't0'], order()

                # A pointer moved into another milestone never changes membership.
                drag('t3', 'other-task')
                assert order() == ['t1', 't2', 't0', 't3'], order()
                assert page.locator('#projectDetail ul[data-milestone-id="other"] > li').count() == 1
                assert 'other-task' not in reorders[-1]['ids']

                row('t3').locator('.drag-handle').focus()
                with page.expect_response(lambda response: response.url.endswith('/todos/reorder')):
                    page.keyboard.press('ArrowUp')
                settle()
                assert order() == ['t1', 't2', 't3', 't0'], order()
                assert row('t3').locator('.drag-handle').evaluate('node => node === document.activeElement')

                # Completed rows participate when shown; drag order takes priority over done state.
                page.locator('#showCompletedProjectItems').check()
                settle()
                assert 'done' in order()
                drag('done', 't1', False)
                assert order()[0] == 'done', order()
                assert row('done').locator('input[type=checkbox]').is_checked()
                page.reload()
                page.locator(scope + ' .drag-handle').first.wait_for()
                assert order()[0] == 'done', order()

                # Failure rolls back the actual DOM and storage and restores keyboard focus.
                before_order, before_data = order(), Path(server.DATA_FILE).read_bytes()
                page.route('**/todos/reorder', lambda route: route.fulfill(status=500, json={'error': '排序保存失败'}))
                row('t0').locator('.drag-handle').focus()
                with page.expect_response(lambda response: response.url.endswith('/todos/reorder')):
                    page.keyboard.press('ArrowUp')
                settle()
                assert order() == before_order, order()
                assert Path(server.DATA_FILE).read_bytes() == before_data
                assert '未保存' in page.locator('#saveStatus').inner_text()
                assert row('t0').locator('.drag-handle').evaluate('node => node === document.activeElement')
                page.unroute('**/todos/reorder')

                # Cancellation restores rows and flushes a render deferred during the gesture.
                previous_requests = len(reorders)
                start, end = row('t0').locator('.drag-handle').bounding_box(), row('t1').bounding_box()
                page.mouse.move(start['x'] + start['width'] / 2, start['y'] + start['height'] / 2)
                page.mouse.down()
                page.mouse.move(end['x'] + 20, end['y'] + 2, steps=8)
                page.evaluate('renderPending = true')
                page.keyboard.press('Escape')
                page.mouse.up()
                settle()
                assert order() == before_order
                assert len(reorders) == previous_requests

                # Editing/selecting text never starts a row drag.
                text = row('t2').locator('[data-field=text]')
                box = text.bounding_box()
                page.mouse.move(box['x'] + 4, box['y'] + box['height'] / 2)
                page.mouse.down()
                page.mouse.move(box['x'] + min(90, box['width'] - 4), box['y'] + box['height'] / 2, steps=8)
                page.mouse.up()
                assert page.evaluate('taskDrag === null')
                assert len(reorders) == previous_requests
                text.fill('修改内容仍保持排序')
                with page.expect_response(lambda response: response.url.endswith('/todos/t2') and response.request.method == 'PATCH'):
                    text.press('Enter')
                settle()
                assert order() == before_order
                assert len(reorders) == previous_requests

                # Deleted milestone links stay recoverable while their tasks reorder in pending.
                pending = '#projectDetail ul[data-project-id="p"][data-milestone-id=""]'
                assert order(pending) == ['u0', 'u1']
                drag('u1', 'u0', False)
                assert order(pending) == ['u1', 'u0']
                assert reorders[-1]['milestone_id'] is None
                saved = {item['id']: item for item in server.read_data()['tasks']}
                assert saved['u1']['milestone_id'] == 'deleted'

                # Sorting changes only project_sort_order and its clock; Tab1 remains stable.
                for original in fixture['tasks']:
                    actual = saved[original['id']]
                    for field in ('priority', 'sort_order', 'project_id', 'milestone_id', 'deadline', 'done', 'done_at'):
                        assert actual.get(field) == original.get(field), (original['id'], field)
                page.locator('[data-tab="things"]').click()
                assert page.locator('#groups li').evaluate_all('rows => rows.map(row => row.dataset.id)') == thing_order
                assert not errors, errors
                print(f'PASS ({engine}): project drag both directions, keyboard/focus, completed rows, scope boundary, reload, failure rollback, cancellation, inline editing, pending tasks and unchanged Tab1 ordering')
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    main()
