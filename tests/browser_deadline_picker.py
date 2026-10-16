"""Date control interactions against disposable data; never mutates the live store."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from datetime import date,timedelta
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

def main():
    engine=os.environ.get('TODO_TEST_BROWSER','chromium')
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR=server.APP_SUPPORT_DIR=tmp
        server.DATA_FILE=str(Path(tmp)/'todos.json');server.BACKUP_DIR=str(Path(tmp)/'backups');server.STATE_FILE=str(Path(tmp)/'state.json')
        server.schedule_sync=lambda:None
        os.environ['TODO_DISABLE_SYNC']='1';os.environ['TODO_DISABLE_UPDATE']='1'
        server.write_data({'projects':[{'id':'p','name':'项目','owner':'Ocean'}],
            'milestones':[{'id':'m','name':'准备','project_id':'p'}],
            'tasks':[{'id':'t','text':'日期验收','owner':'Ocean','deadline':None,'due':'2030-01-01','project_id':'p','milestone_id':'m','priority':'P1','done':False}]})
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        assert httpd.server_port!=8766
        try:
            with sync_playwright() as playwright:
                browser=getattr(playwright,engine).launch()
                # Date shortcuts must use Beijing even when the browser is elsewhere.
                page=browser.new_page(viewport={'width':745,'height':954},timezone_id='America/Los_Angeles')
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                page.goto(f'http://127.0.0.1:{httpd.server_port}');page.wait_for_function('state.tasks?.length===1')
                def settle():page.wait_for_function('mutationCount===0 && !renderPending')
                def task():return server.read_data()['tasks'][0]
                row=page.locator('#groups [data-id=t]');trigger=row.get_by_role('button',name='截止日期',exact=True)
                assert trigger.inner_text()=='设置日期'
                trigger.click();popup=page.get_by_role('dialog',name='选择截止日期')
                bounds=popup.bounding_box();assert bounds['x']>=0 and bounds['x']+bounds['width']<=745
                assert bounds['y']>=0 and bounds['y']+bounds['height']<=954
                popup.get_by_role('button',name='下个月',exact=True).click()
                page.keyboard.press('Escape');popup.wait_for(state='detached');assert task()['deadline'] is None
                assert trigger.evaluate('node=>node===document.activeElement')
                trigger.click();popup.get_by_role('button',name='明天',exact=True).click()
                popup.wait_for(state='detached');settle()
                tomorrow=(date.fromisoformat(server.today_str())+timedelta(days=1)).isoformat()
                assert task()['deadline']==tomorrow and task()['due']=='2030-01-01'
                assert trigger.inner_text()==tomorrow.replace('-','/')
                page.locator('#projectsTab').click()
                row=page.locator('#projectDetail [data-id=t]');trigger=row.get_by_role('button',name='截止日期',exact=True)
                assert trigger.inner_text()==tomorrow.replace('-','/')
                trigger.click();popup.get_by_label('输入截止日期').fill('2026-02-30');popup.get_by_role('button',name='确定',exact=True).click()
                assert '有效日期' in popup.locator('.dp-error').inner_text();assert task()['deadline']==tomorrow
                popup.get_by_label('输入截止日期').fill('2099-12-31')
                route='**/todos/t'
                page.route(route,lambda route:route.fulfill(status=500,content_type='application/json',body='{"error":"test failure"}'))
                popup.get_by_role('button',name='确定',exact=True).click()
                popup.locator('.dp-error').get_by_text('保存失败，日期已保留，请重试',exact=True).wait_for()
                assert popup.get_by_label('输入截止日期').input_value()=='2099-12-31'
                assert task()['deadline']==tomorrow
                page.unroute(route);popup.get_by_role('button',name='确定',exact=True).click()
                popup.wait_for(state='detached');settle();assert task()['deadline']=='2099-12-31'
                trigger.click();assert '2099 年 12 月' in popup.inner_text()
                popup.get_by_role('button',name='上个月',exact=True).click()
                popup.get_by_role('button',name='2099-11-15',exact=True).click()
                popup.wait_for(state='detached');settle();assert task()['deadline']=='2099-11-15'
                trigger.click();popup.get_by_label('输入截止日期').fill('2020-01-01');popup.get_by_role('button',name='确定',exact=True).click()
                popup.wait_for(state='detached');settle();assert task()['deadline']=='2020-01-01'
                assert row.locator('.deadline-wrap').inner_text()=='2020/01/01'
                assert row.locator('.date-status,.deadline-wrap.overdue,.deadline-wrap.due-today').count()==0
                trigger.click();page.keyboard.press('ArrowRight');assert page.evaluate('document.activeElement.dataset.date')=='2020-01-02'
                page.keyboard.press('Enter');popup.wait_for(state='detached');settle();assert task()['deadline']=='2020-01-02'
                trigger.click();page.locator('#projectsTab').click();popup.wait_for(state='detached');assert task()['deadline']=='2020-01-02'
                page.reload();page.locator('#projectDetail [data-id=t] .deadline-button').wait_for()
                assert page.locator('#projectDetail [data-id=t] .deadline-button').inner_text()=='2020/01/02'
                assert page.locator('input[type=date]').count()==0
                page.locator('.milestone-title').first.click()
                form=page.locator('#entityForm');due=form.locator('[name=due]');due.click()
                calendar=page.get_by_role('dialog',name='选择目标日期',exact=True)
                calendar.get_by_role('button',name='明天',exact=True).click()
                assert due.input_value()==tomorrow and not server.read_data()['milestones'][0].get('due')
                due.click();calendar.get_by_role('button',name='清空',exact=True).click();assert due.input_value()==''
                due.click();calendar.get_by_role('button',name='今天',exact=True).click()
                form.get_by_role('button',name='保存',exact=True).click();page.locator('#entityDialog').wait_for(state='hidden');settle()
                assert server.read_data()['milestones'][0]['due']==server.today_str()
                page.locator('.project-launch-date').click();launch=form.locator('[name=launch_date]');launch.click()
                calendar=page.get_by_role('dialog',name='选择预计上线时间',exact=True)
                calendar.get_by_role('button',name='下周',exact=True).click()
                assert launch.input_value()==(date.fromisoformat(server.today_str())+timedelta(days=7)).isoformat()
                page.locator('#entityCancel').click();settle();assert not server.read_data()['projects'][0].get('launch_date')
                page.locator('#projectAddToggle').click();add=page.locator('.milestone-add').first;add.locator('[name=deadline]').click()
                page.get_by_role('dialog',name='选择截止日期',exact=True).get_by_role('button',name='明天',exact=True).click()
                assert add.locator('[name=deadline]').input_value()==tomorrow
                page.locator('#thingsTab').click();page.locator('#newDeadline').click()
                page.get_by_role('dialog',name='选择截止日期',exact=True).get_by_role('button',name='今天',exact=True).click()
                assert page.locator('#newDeadline').input_value()==server.today_str()
                assert not errors,errors
                print(f'PASS {engine}: compact bounds/shortcuts Beijing date/escape/no accidental save/dual views/invalid dates/retry retains input/far future/past dates/month navigation/keyboard/reload/planned date unchanged')
                browser.close()
        finally:httpd.shutdown();httpd.server_close()

if __name__=='__main__':main()
