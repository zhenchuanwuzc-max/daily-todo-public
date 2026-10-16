"""Multiple owners remain one shared task and separate directory people."""
import os
from pathlib import Path
import sys,tempfile,threading
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

def main():
    engine=os.environ.get('TODO_TEST_BROWSER','chromium')
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR=server.APP_SUPPORT_DIR=tmp;server.DATA_FILE=str(Path(tmp)/'todos.json')
        server.BACKUP_DIR=str(Path(tmp)/'backups');server.STATE_FILE=str(Path(tmp)/'state.json');server.schedule_sync=lambda:None
        os.environ['TODO_DISABLE_SYNC']='1';os.environ['TODO_DISABLE_UPDATE']='1'
        server.write_data({'projects':[{'id':'p','name':'项目','owner':'Ocean'}],
          'milestones':[{'id':'m','name':'准备','project_id':'p','owner':'Ocean','due':'2099-12-31'}],
          'people':[{'id':'a','name':'甲','employee_id':'001'},{'id':'b','name':'乙','employee_id':'002'}],
          'tasks':[{'id':'t','text':'多人事项','owner':'Ocean','deadline':'2099-12-31','project_id':'p','milestone_id':'m','priority':'P1','done':False}]})
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=httpd.serve_forever,daemon=True).start()
        assert httpd.server_port!=8766
        try:
            with sync_playwright() as playwright:
                browser=getattr(playwright,engine).launch();page=browser.new_page(viewport={'width':1000,'height':900})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                page.goto(f'http://127.0.0.1:{httpd.server_port}');page.wait_for_function('state.tasks?.length===1')
                def settle():page.wait_for_function('mutationCount===0 && !renderPending')
                def task():return next(t for t in server.read_data()['tasks'] if t['id']=='t')
                row=page.locator('#groups [data-id=t]');toggle=row.locator('.person-picker-toggle')
                toggle.click();popup=page.locator('.person-options');box=page.get_by_role('listbox',name='选择负责人')
                assert box.get_attribute('aria-multiselectable')=='true'
                box.get_by_role('option',name='甲',exact=True).click();box.get_by_role('option',name='乙',exact=True).click()
                assert task()['owner']=='Ocean','Selection should wait for confirmation'
                assert box.get_by_role('option',name='甲',exact=True).get_attribute('aria-selected')=='true'
                page.keyboard.press('Escape');assert task()['owner']=='Ocean'
                toggle.click();box.get_by_role('option',name='甲',exact=True).click();box.get_by_role('option',name='乙',exact=True).click()
                popup.get_by_role('button',name='确认负责人',exact=True).click();settle()
                assert task()['owner']=='Ocean、甲、乙'
                assert row.locator('.owner-input').input_value()=='Ocean、甲、乙'
                page.locator('#projectsTab').click();row=page.locator('#projectDetail [data-id=t]');toggle=row.locator('.person-picker-toggle')
                assert row.locator('.owner-input').input_value()=='Ocean、甲、乙'
                toggle.click()
                for name in ['Ocean','甲','乙']:box.get_by_role('option',name=name,exact=True).click()
                assert popup.get_by_role('button',name='确认负责人',exact=True).is_disabled()
                box.get_by_role('option',name='乙',exact=True).click();popup.get_by_role('button',name='确认负责人',exact=True).click();settle()
                assert task()['owner']=='乙'
                field=row.locator('.owner-input');field.fill('甲，乙、甲');field.press('Enter');settle();assert task()['owner']=='甲、乙'
                assert sorted(page.evaluate('knownPeople().map(p=>p.name)'))==sorted(['Ocean','甲','乙'])
                # Failed save keeps the complete owner selection for retry.
                page.route('**/todos/t',lambda route:route.fulfill(status=500,content_type='application/json',body='{"error":"test failure"}'))
                toggle.click();box.get_by_role('option',name='Ocean',exact=True).click();popup.get_by_role('button',name='确认负责人',exact=True).click();settle()
                assert field.input_value()=='甲、乙、Ocean' and 'save-failed' in field.get_attribute('class')
                assert task()['owner']=='甲、乙'
                page.unroute('**/todos/t');field.focus();field.press('Enter');settle();assert task()['owner']=='甲、乙、Ocean'
                page.locator('.milestone-title').first.click(button='right');page.get_by_role('menuitem',name='新增 1 个事项',exact=True).click();form=page.locator('.milestone-add').first
                form.locator('[name=text]').fill('多人新增')
                form.locator('.person-picker-toggle').click();box.get_by_role('option',name='甲',exact=True).click();popup.get_by_role('button',name='确认负责人',exact=True).click()
                form.get_by_role('button',name='添加',exact=True).click();settle()
                assert next(t for t in server.read_data()['tasks'] if t['text']=='多人新增')['owner']=='Ocean、甲'
                page.locator('#thingsTab').click();page.locator('#newOwner').locator('..').locator('.person-picker-toggle').click()
                box.get_by_role('option',name='乙',exact=True).click();popup.get_by_role('button',name='确认负责人',exact=True).click()
                assert page.locator('#newOwner').input_value()=='Ocean、乙'
                page.reload();page.wait_for_function('state.tasks?.length===2')
                assert page.locator('#groups [data-id=t] .owner-input').input_value()=='甲、乙、Ocean'
                # Project and milestone forms use the same multi-select picker in the modal layer.
                page.locator('#projectsTab').click()
                for target in ['.project-heading h2','.milestone-title']:
                    page.locator(target).first.click()
                    form=page.locator('#entityForm');form.locator('.person-picker-toggle').click()
                    box.get_by_role('option',name='甲',exact=True).click()
                    page.get_by_role('button',name='确认负责人',exact=True).click()
                    assert form.locator('[name=owner]').input_value()=='Ocean、甲'
                    form.locator('.person-picker-toggle').click();page.keyboard.press('Escape')
                    assert page.locator('#entityDialog').is_visible(),'Escape should close the picker before the dialog'
                    form.get_by_role('button',name='保存',exact=True).click()
                    page.locator('#entityDialog').wait_for(state='hidden');settle()
                data=server.read_data();assert data['projects'][0]['owner']=='Ocean、甲' and data['milestones'][0]['owner']=='Ocean、甲'
                page.locator('#projectDetail [data-id=t] .owner-input').fill('Ocean、甲')
                popup=page.locator('.person-options');box=page.get_by_role('listbox',name='选择负责人')
                assert box.get_by_role('option').count()==1 and box.get_by_role('option',name='甲',exact=True).is_visible()
                assert next(t for t in server.read_data()['tasks'] if t['id']=='t')['owner']=='甲、乙、Ocean'
                box.get_by_role('option',name='甲',exact=True).click()
                popup.get_by_label('筛选负责人').fill('002')
                assert box.get_by_role('option').count()==1 and box.get_by_role('option',name='乙',exact=True).is_visible()
                box.get_by_role('option',name='乙',exact=True).click()
                popup.get_by_label('筛选负责人').fill('oCeAn')
                assert box.get_by_role('option',name='Ocean',exact=True).get_attribute('aria-selected')=='true'
                popup.get_by_role('button',name='确认负责人',exact=True).click();settle()
                assert next(t for t in server.read_data()['tasks'] if t['id']=='t')['owner']=='Ocean、甲、乙'
                field=page.locator('#projectDetail [data-id=t] .owner-input');field.fill('新同事')
                assert popup.locator('.person-no-match').is_visible()
                field.press('Enter');settle();assert next(t for t in server.read_data()['tasks'] if t['id']=='t')['owner']=='新同事'
                assert not errors,errors
                print(f'PASS {engine}: multi-select/confirm/cancel/deselect/required owner/free text dedup/separate directory entries/failed-save retention/retry/dual views/both add forms/reload')
                browser.close()
        finally:httpd.shutdown();httpd.server_close()
if __name__=='__main__':main()
