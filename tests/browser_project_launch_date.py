"""Project launch date create/edit/clear/retry checks with isolated storage."""
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
        milestone={'id':'m','project_id':'p','name':'原里程碑','due':'2026-10-21'}
        task={'id':'t','project_id':'p','milestone_id':'m','text':'原事项','owner':'Ocean','deadline':'2026-10-22','priority':'P1','done':False}
        server.write_data({'projects':[{'id':'p','name':'原项目','owner':'Ocean'}],'milestones':[milestone],'tasks':[task]})
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=httpd.serve_forever,daemon=True).start()
        assert httpd.server_port!=8766
        try:
            with sync_playwright() as playwright:
                browser=getattr(playwright,engine).launch();page=browser.new_page(viewport={'width':1064,'height':954})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{httpd.server_port}');page.wait_for_function('state.projects?.length===1');page.locator('#projectsTab').click()
                def settle():page.wait_for_function('mutationCount===0 && !renderPending')
                def project():return next(p for p in server.read_data()['projects'] if p['id']=='p')
                field=page.locator('#entityForm [name=launch_date]');form=page.locator('#entityForm')
                assert page.locator('.project-launch-date').inner_text()=='预计上线：未设'
                page.locator('.project-launch-date').click();field.fill('2026-11-01')
                page.route('**/projects/p',lambda route:route.fulfill(status=500,json={'error':'test failure'}))
                form.get_by_role('button',name='保存',exact=True).click()
                page.locator('#entityError').get_by_text('保存失败，内容已保留，请重试。',exact=True).wait_for()
                assert field.input_value()=='2026-11-01' and not project().get('launch_date')
                page.unroute('**/projects/p');form.get_by_role('button',name='保存',exact=True).click()
                page.locator('#entityDialog').wait_for(state='hidden');settle()
                assert project()['launch_date']=='2026-11-01'
                assert page.locator('.project-launch-date').inner_text()=='预计上线：2026/11/01'
                page.reload();page.locator('.project-launch-date').wait_for()
                assert page.locator('.project-launch-date').inner_text()=='预计上线：2026/11/01'
                page.locator('.project-heading h2').click()
                server.patch_entity('projects','p',{'launch_date':'2026-11-02'})
                form.locator('[name=goal]').fill('仅更新目标');form.get_by_role('button',name='保存',exact=True).click()
                page.locator('#entityDialog').wait_for(state='hidden');settle()
                assert project()['launch_date']=='2026-11-02'
                page.locator('.project-launch-date').click();field.fill('');form.get_by_role('button',name='保存',exact=True).click()
                try:page.locator('#entityDialog').wait_for(state='hidden',timeout=5000)
                except Exception:
                    print(page.evaluate("({error:document.getElementById('entityError').textContent,status:document.getElementById('saveStatus').textContent,invalid:[...document.getElementById('entityForm').elements].filter(e=>e.validity&&!e.validity.valid).map(e=>({name:e.name,value:e.value,message:e.validationMessage}))})"));raise
                settle();assert project()['launch_date'] is None
                page.locator('.milestone-title').click();assert page.locator('#entityLaunchDateLabel').is_hidden()
                page.locator('#entityCancel').click();settle()
                page.locator('#newProjectBtn').click();form.locator('[name=name]').fill('带上线时间的项目');field.fill('2026-12-15')
                form.get_by_role('button',name='保存',exact=True).click();page.locator('#entityDialog').wait_for(state='hidden');settle()
                assert next(p for p in server.read_data()['projects'] if p['name']=='带上线时间的项目')['launch_date']=='2026-12-15'
                assert server.read_data()['tasks']==[task] and server.read_data()['milestones']==[milestone]
                assert not errors,errors
                print(f'PASS {engine}: optional project launch date/create/edit/header/reload/failure retry/clear/concurrent update/unrelated dates unchanged')
                browser.close()
        finally:httpd.shutdown();httpd.server_close()
if __name__=='__main__':main()
