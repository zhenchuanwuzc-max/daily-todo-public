"""Sidebar drag and milestone context-menu checks on disposable data only."""
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
        os.environ['TODO_DISABLE_SYNC'] = '1'
        os.environ['TODO_DISABLE_UPDATE'] = '1'
        fixture = {'projects': [{'id': f'p{i}', 'name': f'项目{i}', 'owner': 'Ocean', 'created': f'2026-10-{i+1:02d}'} for i in range(14)],
                   'milestones': [{'id': 'm', 'name': '效果运营', 'project_id': 'p0', 'owner': 'Ocean'}],
                   'tasks': [{'id': 't', 'text': '验收事项', 'owner': 'Ocean', 'done': False, 'priority': 'P1', 'project_id': 'p0', 'milestone_id': 'm', 'deadline': '2099-01-01'}]}
        server.write_data(copy.deepcopy(fixture))
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        assert httpd.server_port != 8766
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, engine).launch()
                page = browser.new_page(viewport={'width': 1064, 'height': 800})
                errors, requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.post_data_json) if request.url.endswith('/projects/reorder') else None)
                page.goto(f'http://127.0.0.1:{httpd.server_port}')
                page.wait_for_function('state.projects?.length===14')
                page.locator('#projectsTab').click()
                def row(identity): return page.locator(f'#projectList [data-project-id="{identity}"]')
                def order(): return page.locator('#projectList .project-item').evaluate_all('nodes=>nodes.map(n=>n.dataset.projectId)')
                def settle(): page.wait_for_function('mutationCount===0 && !renderPending && !projectDrag')
                def begin(source, target, bottom=True):
                    row(source).scroll_into_view_if_needed()
                    a, b = row(source).bounding_box(), row(target).bounding_box()
                    page.mouse.move(a['x']+30, a['y']+a['height']/2)
                    page.mouse.down()
                    page.mouse.move(b['x']+30, b['y']+b['height']*(.9 if bottom else .1), steps=10)
                row('p0').click()
                assert page.locator('.project-heading h2').inner_text() == '项目0'
                begin('p0', 'p2')
                with page.expect_response(lambda r: r.url.endswith('/projects/reorder')) as response: page.mouse.up()
                assert response.value.ok
                settle()
                assert order()[:3] == ['p1', 'p2', 'p0'], order()
                assert row('p0').get_attribute('aria-current') == 'true'
                assert page.locator('.project-heading h2').inner_text() == '项目0'
                assert row('p0').bounding_box()['height'] <= 54
                page.reload();row('p0').wait_for()
                assert order()[:3] == ['p1', 'p2', 'p0']
                row('p0').focus();page.keyboard.press('Alt+ArrowUp');settle()
                assert order()[:3] == ['p1', 'p0', 'p2']
                assert row('p0').evaluate('n=>n===document.activeElement')
                before, data = order(), Path(server.DATA_FILE).read_bytes()
                page.route('**/projects/reorder', lambda route: route.fulfill(status=500, json={'error': '测试保存失败'}))
                page.keyboard.press('Alt+ArrowUp');settle()
                assert order() == before and Path(server.DATA_FILE).read_bytes() == data
                assert '未保存' in page.locator('#saveStatus').inner_text()
                page.unroute('**/projects/reorder')
                count = len(requests)
                begin('p0', 'p3');page.evaluate('renderPending=true');page.keyboard.press('Escape');page.mouse.up();settle()
                assert order() == before and len(requests) == count
                # Dragging near the bottom scrolls a long sidebar; no click switches project.
                a = row('p0').bounding_box();box=page.locator('#projectList').bounding_box()
                page.mouse.move(a['x']+30,a['y']+20);page.mouse.down()
                page.mouse.move(box['x']+30,box['y']+box['height']-3,steps=10)
                page.wait_for_function('document.getElementById("projectList").scrollTop>30')
                page.keyboard.press('Escape');page.mouse.up();settle()
                assert order() == before
                assert server.read_data()['tasks'] == fixture['tasks']
                assert server.read_data()['milestones'] == fixture['milestones']
                # Project context menu continues to work alongside drag/click handling.
                row('p0').click(button='right');menu=page.get_by_role('menu',name='项目操作')
                assert menu.get_by_role('menuitem').all_text_contents()==['编辑','删除']
                page.keyboard.press('Escape');assert row('p0').evaluate('n=>n===document.activeElement')
                # Right-click title or milestone heading gives explicit actions, never an immediate edit.
                title=page.locator('[data-milestone-id=m] .milestone-title')
                title.click(button='right');menu=page.get_by_role('menu',name='里程碑操作')
                assert menu.get_by_role('menuitem').all_text_contents()==['编辑','删除']
                assert not page.locator('#entityDialog').is_visible()
                menu.get_by_role('menuitem',name='编辑',exact=True).click()
                assert page.locator('#entityTitle').inner_text()=='编辑里程碑'
                page.locator('#entityCancel').click();settle()
                title.focus();page.keyboard.press('Shift+F10')
                assert menu.is_visible()
                page.keyboard.press('Escape');assert title.evaluate('n=>n===document.activeElement'), page.evaluate('({active:document.activeElement.outerHTML, pending:renderPending, hidden:document.getElementById("projectContextMenu").hidden, anchor:projectContextAnchor?.outerHTML})')
                title.click();assert page.locator('#entityTitle').inner_text()=='编辑里程碑'
                page.locator('#entityCancel').click();settle()
                page.once('dialog',lambda dialog:dialog.dismiss())
                title.click(button='right');menu.get_by_role('menuitem',name='删除',exact=True).click();settle()
                assert not server.read_data()['milestones'][0].get('deleted_at')
                page.route('**/milestones/m',lambda route:route.fulfill(status=500,json={'error':'删除失败'}))
                page.once('dialog',lambda dialog:dialog.accept())
                title.click(button='right');menu.get_by_role('menuitem',name='删除',exact=True).click();settle()
                assert not server.read_data()['milestones'][0].get('deleted_at') and title.is_visible()
                page.unroute('**/milestones/m')
                page.once('dialog',lambda dialog:dialog.accept())
                title.click(button='right');menu.get_by_role('menuitem',name='删除',exact=True).click();settle()
                assert server.read_data()['milestones'][0]['deleted_at']
                assert server.read_data()['tasks']==fixture['tasks']
                assert page.locator('#projectDetail ul[data-milestone-id=""] [data-id=t]').is_visible()
                # The existing restore API revives the original relation.
                assert page.request.post(f'http://127.0.0.1:{httpd.server_port}/milestones/m/restore').ok
                page.reload();title.wait_for()
                assert page.locator('#projectDetail ul[data-milestone-id=m] [data-id=t]').is_visible()
                assert not errors, errors
                print(f'PASS {engine}: sidebar drag/persist/selection/keyboard/rollback/cancel/autoscroll/project menu; milestone context edit/delete/cancel/failure/task retention/restore')
                browser.close()
        finally:
            httpd.shutdown();httpd.server_close()

if __name__=='__main__': main()
