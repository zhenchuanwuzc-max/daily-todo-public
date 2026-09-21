import sys, tempfile, pathlib, threading, json, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server
from http.server import HTTPServer
from playwright.sync_api import sync_playwright
with tempfile.TemporaryDirectory() as tmp:
    server.DATA_DIR = tmp
    server.DATA_FILE = tmp + '/todos.json'
    server.BACKUP_DIR = tmp + '/backups'
    server.schedule_sync = lambda: None
    server.write_data({'tasks': [
        {'id': f't{i}', 'text': f'Test project {i}', 'priority': 'P0', 'created': f'2026-09-{21-i:02}T09:00:00'} for i in range(5)
    ] + [{'id':'p1', 'text':'Other priority', 'priority':'P1'}]})
    httpd = HTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_playwright() as p:
        browser = getattr(p, os.environ.get("TODO_TEST_BROWSER", "chromium")).launch(headless=True)
        page = browser.new_page(viewport={'width': 1000, 'height': 850})
        errors=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{httpd.server_port}')
        page.wait_for_selector('.P0 li .drag-handle')
        order=lambda: page.locator('.P0 li').evaluate_all('(rows) => rows.map(r => r.dataset.id)')
        assert order() == ['t0','t1','t2','t3','t4'], order()
        def drag(source, target, bottom=True):
            s=page.locator(f'li[data-id="{source}"] .drag-handle').bounding_box()
            t=page.locator(f'li[data-id="{target}"]').bounding_box()
            page.mouse.move(s['x']+s['width']/2, s['y']+s['height']/2)
            page.mouse.down()
            page.mouse.move(t['x']+20, t['y']+t['height']*(0.9 if bottom else 0.1), steps=12)
            with page.expect_response(lambda r: r.url.endswith('/todos/reorder')) as response:
                page.mouse.up()
            assert response.value.status == 200
            page.wait_for_function('!dirty')
        drag('t0','t3')
        assert order() == ['t1','t2','t3','t0','t4'], order()
        page.reload(); page.wait_for_selector('.P0 li')
        assert order() == ['t1','t2','t3','t0','t4'], order()
        drag('t4','t1',False)
        assert order() == ['t4','t1','t2','t3','t0'], order()
        # Cross-group dragging remains confined to P0.
        drag('t4','p1')
        assert page.locator('.P1 li').count() == 1
        assert order() == ['t1','t2','t3','t0','t4'], order()
        page.locator('li[data-id="t4"] .drag-handle').focus()
        with page.expect_response(lambda r: r.url.endswith('/todos/reorder')):
            page.keyboard.press('ArrowUp')
        page.wait_for_function('!dirty')
        assert order() == ['t1','t2','t3','t4','t0'], order()
        # Failed persistence restores the last saved order and exposes failure.
        page.route('**/todos/reorder', lambda route: route.fulfill(status=500, json={'error':'Test save failure'}))
        dialogs=[]
        page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        page.locator('li[data-id="t0"] .drag-handle').focus()
        page.keyboard.press('ArrowUp')
        page.wait_for_function('!dirty')
        assert order() == ['t1','t2','t3','t4','t0'], order()
        assert dialogs and '排序未保存' in dialogs[0]
        assert not errors, errors
        print('PASS: mouse drag both directions, reload persistence, group boundary, keyboard, failure rollback; no JS errors')
        browser.close()
    httpd.shutdown()
