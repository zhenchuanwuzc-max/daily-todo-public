"""Contextual task entry with independent drafts; uses disposable data only."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def main():
    engine = os.environ.get('TODO_TEST_BROWSER', 'chromium')
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR = server.APP_SUPPORT_DIR = tmp
        server.DATA_FILE = str(Path(tmp) / 'todos.json')
        server.BACKUP_DIR = str(Path(tmp) / 'backups')
        server.STATE_FILE = str(Path(tmp) / 'state.json')
        server.schedule_sync = lambda: None
        os.environ['TODO_DISABLE_SYNC'] = os.environ['TODO_DISABLE_UPDATE'] = '1'
        server.write_data({
            'projects': [{'id': 'p', 'name': '带里程碑项目'}, {'id': 'basic', 'name': '基础项目'}],
            'milestones': [{'id': 'm1', 'name': '准备', 'project_id': 'p', 'due': '2099-12-20'},
                           {'id': 'm2', 'name': '上线', 'project_id': 'p', 'due': '2099-12-25'}],
            'tasks': []})
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        assert httpd.server_port != 8766
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch()
                page = browser.new_page(viewport={'width': 1013, 'height': 774})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.add_init_script("localStorage.setItem('tracking-tab','projects');localStorage.setItem('tracking-project','p')")
                page.goto(f'http://127.0.0.1:{httpd.server_port}')
                page.locator('section[data-milestone-id=m1]').wait_for()

                def settle():
                    page.locator('#projectDetail .project-heading h2').focus()
                    page.wait_for_function('mutationCount === 0 && !renderPending')

                def open_rows(mid, count=1):
                    page.locator(f'section[data-milestone-id={mid}] .milestone-title').click(button='right')
                    page.get_by_role('menuitem', name=f'新增 {count} 个事项', exact=True).click()

                def draft_rows(mid):
                    return page.locator(f'.milestone-add[data-milestone-id="{mid}"]')

                def drafts():
                    return page.locator('.milestone-add')

                before = Path(server.DATA_FILE).read_bytes()
                assert not drafts().count()
                assert not page.locator('#projectAddToggle').count()
                open_rows('m1', 2)
                assert draft_rows('m1').count() == 2 and not draft_rows('m2').count()
                assert draft_rows('m1').first.locator('[name=text]').evaluate('n => n === document.activeElement')
                ids = drafts().evaluate_all('nodes => nodes.map(n => n.dataset.draftId)')
                first, second = [page.locator(f'[data-draft-id="{key}"]') for key in ids]
                assert first.locator('[name=owner]').input_value() == 'Ocean'
                assert first.locator('[name=deadline]').input_value() == '2099-12-20'
                assert Path(server.DATA_FILE).read_bytes() == before, 'Opening drafts must not create empty tasks'
                first.locator('[name=text]').fill('第一行')
                second.locator('[name=text]').fill('第二行')
                second.locator('[name=owner]').fill('任意姓名')
                second.locator('[name=deadline]').fill('2099-12-28')
                page.locator('#thingsTab').click()
                page.locator('#projectsTab').click()
                # Switching projects preserves every draft, including distinct dates and owners.
                page.locator('#projectList [data-project-id=basic]').click()
                assert not drafts().count()
                page.locator('#projectList [data-project-id=p]').click()
                assert first.locator('[name=text]').input_value() == '第一行'
                assert second.locator('[name=owner]').input_value() == '任意姓名'
                assert second.locator('[name=deadline]').input_value() == '2099-12-28'
                open_rows('m2')
                third = draft_rows('m2')
                assert third.locator('[name=deadline]').input_value() == '2099-12-25'
                third.locator('[name=text]').fill('另一里程碑草稿')

                # A failed first save leaves both sibling drafts and the other milestone intact.
                page.route('**/todos/add', lambda route: route.fulfill(status=500, json={'error': '模拟保存失败'}))
                first.get_by_role('button', name='添加', exact=True).click()
                settle()
                assert first.locator('[name=text]').input_value() == '第一行'
                assert second.locator('[name=text]').input_value() == '第二行'
                assert Path(server.DATA_FILE).read_bytes() == before
                page.unroute('**/todos/add')
                first.get_by_role('button', name='添加', exact=True).click()
                settle()
                assert first.count() == 0 and second.count() == 1 and third.count() == 1
                task = server.read_data()['tasks'][0]
                assert (task['text'], task['project_id'], task['milestone_id']) == ('第一行', 'p', 'm1')
                second.get_by_role('button', name='取消新增事项').click()
                settle()
                assert not draft_rows('m1').count() and third.count() == 1
                assert third.locator('[name=text]').input_value() == '另一里程碑草稿'

                # Enter while saving cannot submit twice; edits made during the request survive.
                held = []
                page.route('**/todos/add', lambda route: held.append(route))
                third.get_by_role('button', name='添加', exact=True).click()
                page.wait_for_function('mutationCount === 1')
                assert third.get_by_role('button', name='取消新增事项').is_disabled()
                field = third.locator('[name=text]')
                field.fill('请求中继续输入')
                field.press('Enter')
                assert len(held) == 1
                held[0].continue_()
                settle()
                page.unroute('**/todos/add')
                assert len(server.read_data()['tasks']) == 2
                assert third.locator('[name=text]').input_value() == '请求中继续输入'
                third.get_by_role('button', name='取消新增事项').click()
                settle()
                assert not drafts().count()

                # A basic project stays reachable from its sidebar menu, even when not selected.
                page.locator('#projectList [data-project-id=basic]').click(button='right')
                page.get_by_role('menuitem', name='新增 2 个事项', exact=True).click()
                assert page.locator('#projectList [data-project-id=basic]').get_attribute('aria-current') == 'true'
                assert drafts().count() == 2 and not page.locator('#projectDetail .milestone-head').count()
                assert drafts().first.locator('[name=deadline]').input_value() == ''
                drafts().first.locator('[name=text]').fill('基础项目直接新增')
                drafts().first.locator('[name=deadline]').fill('2099-12-29')
                drafts().first.get_by_role('button', name='添加', exact=True).click()
                settle()
                basic_task = server.read_data()['tasks'][-1]
                assert basic_task['project_id'] == 'basic' and not basic_task.get('milestone_id')
                drafts().first.get_by_role('button', name='取消新增事项').click()
                settle()
                # Project-menu entry on a structured project opens only its unassigned area.
                page.locator('#projectList [data-project-id=p]').click(button='right')
                page.get_by_role('menuitem', name='新增 1 个事项', exact=True).click()
                assert draft_rows('').count() == 1 and not draft_rows('m1').count()
                assert drafts().first.locator('[name=text]').evaluate('n => n === document.activeElement')
                for width in (775, 1013, 1305):
                    page.set_viewport_size({'width': width, 'height': 774})
                    assert drafts().evaluate_all('nodes => nodes.every(n => n.scrollWidth <= n.clientWidth + 1)')
                assert not errors, errors
                print(f'PASS {engine}: scoped 1/2 rows, independent drafts, defaults, failure/retry, cancel, in-flight edits, basic projects, tab/project switching and layout')
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    main()
