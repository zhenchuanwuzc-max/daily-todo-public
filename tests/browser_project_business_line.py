"""Project label UI acceptance against an isolated store in Chromium/WebKit."""
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
    engine = os.environ.get('TODO_TEST_BROWSER', 'chromium')
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR = server.APP_SUPPORT_DIR = tmp
        server.DATA_FILE = str(Path(tmp) / 'todos.json')
        server.BACKUP_DIR = str(Path(tmp) / 'backups')
        server.STATE_FILE = str(Path(tmp) / 'state.json')
        server.schedule_sync = lambda: None
        os.environ['TODO_DISABLE_SYNC'] = os.environ['TODO_DISABLE_UPDATE'] = '1'
        fixture = {
            'projects': [
                {'id': 'old', 'name': '原有项目'},
                {'id': 'c1', 'name': 'CRM 项目甲', 'business_line': 'crm', 'core': True},
                {'id': 'l1', 'name': '物流项目甲', 'business_line': 'logistics', 'core': True},
                {'id': 'c2', 'name': 'CRM 项目乙', 'business_line': 'crm'},
                {'id': 'l2', 'name': '物流项目乙', 'business_line': 'logistics'},
            ],
            'milestones': [{'id': 'm', 'project_id': 'c1', 'name': '交付', 'due': '2099-12-20'}],
            'tasks': [{'id': 't', 'text': '现有事项', 'owner': 'Ocean', 'done': False, 'priority': 'P1',
                       'project_id': 'c1', 'milestone_id': 'm', 'deadline': server.today_str()}],
        }
        for order, project in enumerate(fixture['projects']):
            project['sort_order'] = order
        server.write_data(copy.deepcopy(fixture))
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        assert httpd.server_port != 8766
        base = f'http://127.0.0.1:{httpd.server_port}'
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch()
                page = browser.new_page(viewport={'width': 1064, 'height': 954})
                errors, patches = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: patches.append(request.post_data_json)
                        if request.method == 'PATCH' and '/projects/' in request.url else None)
                def label(value): return page.locator(f'[data-project-label="{value}"]')
                def row(identity): return page.locator(f'#projectList [data-project-id="{identity}"]')
                def order(): return page.locator('#projectList .project-item').evaluate_all('nodes=>nodes.map(n=>n.dataset.projectId)')
                def settle(): page.wait_for_function('mutationCount===0 && !renderPending && !projectDrag')
                def save():
                    page.locator('#entityForm').get_by_role('button', name='保存', exact=True).click()
                    page.locator('#entityDialog').wait_for(state='hidden')
                    settle()
                def project(identity): return next(p for p in server.read_data()['projects'] if p['id'] == identity)
                before = Path(server.DATA_FILE).read_bytes()
                page.goto(base)
                page.wait_for_function('state.projects?.length===5')
                page.locator('#projectsTab').click()
                assert order() == ['old', 'c1', 'l1', 'c2', 'l2']
                assert Path(server.DATA_FILE).read_bytes() == before
                label('crm').click()
                assert order() == ['c1', 'c2'] and row('c1').get_attribute('aria-current') == 'true'
                row('c2').click()
                page.reload(); row('c2').wait_for()
                assert label('crm').get_attribute('aria-pressed') == 'true'
                assert row('c2').get_attribute('aria-current') == 'true'
                label('logistics').click()
                assert order() == ['l1', 'l2'] and row('l1').get_attribute('aria-current') == 'true'
                assert page.locator('#coreSummary button').count() == 2, 'Project labels must not filter global progress'
                label('crm').click()

                # A draft survives switching labels and retains owner/date/text.
                page.locator('.milestone-title').click(button='right')
                page.get_by_role('menuitem', name='新增 1 个事项', exact=True).click()
                draft = page.locator('.milestone-add')
                draft.locator('[name=text]').fill('保留这条草稿')
                draft.locator('[name=owner]').fill('任意负责人')
                label('logistics').click(); assert not draft.count()
                label('crm').click()
                assert draft.locator('[name=text]').input_value() == '保留这条草稿'
                assert draft.locator('[name=owner]').input_value() == '任意负责人'
                assert draft.locator('[name=deadline]').input_value() == '2099-12-20'

                # Filtered pointer drag and keyboard reorder preserve other slots.
                a, b = row('c2').bounding_box(), row('c1').bounding_box()
                page.mouse.move(a['x'] + 24, a['y'] + 22); page.mouse.down()
                page.mouse.move(b['x'] + 24, b['y'] + 3, steps=12)
                with page.expect_response(lambda r: r.url.endswith('/projects/reorder')) as response:
                    page.mouse.up()
                assert response.value.ok; settle()
                assert order() == ['c2', 'c1']
                label('all').click(); assert order() == ['old', 'c2', 'l1', 'c1', 'l2']
                label('crm').click(); row('c1').focus(); page.keyboard.press('Alt+ArrowUp'); settle()
                label('all').click(); assert order() == ['old', 'c1', 'l1', 'c2', 'l2']
                label('crm').click(); row('c1').click()
                draft.get_by_role('button', name='取消新增事项').click(); settle()
                original_order = order()
                page.route('**/projects/reorder', lambda route: route.fulfill(status=500, json={'error': '排序失败'}))
                row('c2').focus(); page.keyboard.press('Alt+ArrowUp'); settle()
                assert order() == original_order
                page.unroute('**/projects/reorder')

                # Edit, failed save/retry, and clear all preserve the form input.
                row('c1').click(); page.locator('.project-heading h2').click()
                field = page.locator('#entityForm [name=business_line]')
                assert field.input_value() == 'crm'
                assert page.locator('[data-entity-business-line=crm]').get_attribute('aria-pressed') == 'true'
                core = page.locator('#entityCoreLabel')
                assert core.get_attribute('aria-pressed') == 'true'
                core.click(); assert core.get_attribute('aria-pressed') == 'false'
                page.locator('[data-entity-business-line=logistics]').click()
                assert core.get_attribute('aria-pressed') == 'false', 'Core is independent of CRM/logistics'
                assert page.locator('[data-entity-business-line=crm]').get_attribute('aria-pressed') == 'false'
                core.click()
                owner_box = page.locator('#entityForm [name=owner]').bounding_box()
                date_box = page.locator('#entityForm [name=launch_date]').bounding_box()
                assert abs(owner_box['y'] - date_box['y']) < 1
                assert owner_box['x'] + owner_box['width'] < date_box['x']
                # Save/cancel stay in the modal's bottom layer while only its
                # fields scroll. Optional fields may remain empty during save.
                page.set_viewport_size({'width': 790, 'height': 600})
                save_button = page.locator('#entityForm').get_by_role('button', name='保存', exact=True)
                button_before = save_button.bounding_box()
                body = page.locator('#entityForm .entity-dialog-body')
                assert body.evaluate('n=>n.scrollHeight>n.clientHeight')
                body.evaluate('n=>n.scrollTop=n.scrollHeight')
                button_after = save_button.bounding_box()
                assert abs(button_before['y'] - button_after['y']) < 1
                assert button_after['y'] + button_after['height'] < 600
                assert save_button.evaluate('n=>{const r=n.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest("button")===n}')
                body.evaluate('n=>n.scrollTop=0')
                page.screenshot(path=f'/tmp/daily-todo-project-editor-compact-{engine}.png')
                page.set_viewport_size({'width': 1064, 'height': 954})
                page.route('**/projects/c1', lambda route: route.fulfill(status=500, json={'error': '保存失败'}))
                page.locator('#entityForm').get_by_role('button', name='保存', exact=True).click()
                page.locator('#entityError').get_by_text('保存失败，内容已保留，请重试。', exact=True).wait_for()
                assert field.input_value() == 'logistics' and project('c1')['business_line'] == 'crm'
                page.unroute('**/projects/c1'); save()
                assert label('logistics').get_attribute('aria-pressed') == 'true'
                assert row('c1').get_attribute('aria-current') == 'true'
                page.locator('.project-heading h2').click(); page.locator('[data-entity-business-line=logistics]').click(); save()
                assert project('c1')['business_line'] is None and label('all').get_attribute('aria-pressed') == 'true'
                page.locator('.project-heading h2').click(); page.locator('[data-entity-business-line=crm]').click(); save()

                # An untouched label is omitted, preserving a concurrent edit.
                page.locator('.project-heading h2').click()
                server.patch_entity('projects', 'c1', {'business_line': 'logistics'})
                page.locator('#entityForm [name=goal]').fill('仅修改目标'); save()
                assert 'business_line' not in patches[-1] and project('c1')['business_line'] == 'logistics'
                label('logistics').click(); row('c1').click()
                page.locator('.project-heading h2').click(); page.locator('[data-entity-business-line=crm]').click(); save()
                page.locator('.milestone-title').click()
                assert page.locator('#entityBusinessLineLabel').is_hidden()
                page.locator('#entityCancel').click(); settle()

                # A CRM template creates logistics projects using the form label.
                assert page.request.post(base + '/projects/c1/template', data={}).ok
                page.reload(); row('c1').wait_for()
                label('logistics').click(); page.locator('#newProjectBtn').click()
                assert field.input_value() == 'logistics'
                assert page.locator('#entityTemplate').input_value() == 'c1'
                page.locator('#entityForm [name=name]').fill('物流模板克隆'); save()
                clone = next(p for p in server.read_data()['projects'] if p['name'] == '物流模板克隆')
                assert clone['business_line'] == 'logistics' and row(clone['id']).get_attribute('aria-current') == 'true'
                label('all').click(); page.locator('#newProjectBtn').click()
                assert field.input_value() == ''
                page.locator('#entityCancel').click(); settle()

                # A core summary crosses the active filter and locates its project.
                page.locator('#thingsTab').click()
                page.locator('#coreSummary button').filter(has_text='CRM 项目甲').click()
                assert label('crm').get_attribute('aria-pressed') == 'true'
                assert row('c1').get_attribute('aria-current') == 'true'
                assert page.locator('#projectDetail [data-id=t]').is_visible()
                assert server.read_data()['tasks'][0] == fixture['tasks'][0]
                assert server.read_data()['milestones'][0] == fixture['milestones'][0]
                assert 'business_line' not in project('old')

                # Narrow layout keeps a single compact filter row and the add button.
                page.set_viewport_size({'width': 696, 'height': 954})
                box, sidebar = page.locator('#projectLabels').bounding_box(), page.locator('.project-sidebar').bounding_box()
                assert box['height'] < 44 and box['x'] + box['width'] <= sidebar['x'] + sidebar['width'] + 1
                assert page.locator('#newProjectBtn').is_visible()
                page.screenshot(path=f'/tmp/daily-todo-project-labels-{engine}.png', full_page=True)

                # The last project leaving a label produces a usable empty state.
                for p in server.read_data()['projects']:
                    if p.get('business_line') == 'logistics':
                        server.patch_entity('projects', p['id'], {'business_line': None})
                label('logistics').click(); page.reload()
                page.get_by_text('暂无物流项目', exact=True).wait_for()
                assert not order() and not page.locator('#projectDetail .project-heading h2').count()
                page.locator('#newProjectBtn').click(); assert field.input_value() == 'logistics'
                page.locator('#entityCancel').click(); settle()
                assert not errors, errors
                print(f'PASS {engine}: labels/create/edit/clear/reload/empty state/template/drafts/scoped drag and keyboard/rollback/concurrent update/global progress/cross-label jump/narrow layout')
                browser.close()
        finally:
            httpd.shutdown(); httpd.server_close()


if __name__ == '__main__':
    main()
