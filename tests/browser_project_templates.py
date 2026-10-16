"""Template, resource links and readable owner-picker checks with disposable data."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server


def main():
    engine=os.environ.get('TODO_TEST_BROWSER','chromium')
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR=server.APP_SUPPORT_DIR=tmp
        server.DATA_FILE=str(Path(tmp)/'todos.json')
        server.BACKUP_DIR=str(Path(tmp)/'backups')
        server.STATE_FILE=str(Path(tmp)/'state.json')
        server.schedule_sync=lambda:None
        os.environ['TODO_DISABLE_SYNC']='1'
        os.environ['TODO_DISABLE_UPDATE']='1'
        original={'id':'task','text':'模板事项','owner':'原负责人','deadline':'2026-10-18','project_id':'source','milestone_id':'m','priority':'P1','done':False}
        server.write_data({'projects':[{'id':'source','name':'来源项目','owner':'Ocean'}],
                          'milestones':[{'id':'m','name':'前期准备','project_id':'source','due':'2026-10-18','owner':'原负责人'}],
                          'tasks':[original], 'people':[{'id':'p','name':'王同事','employee_id':'00123'}]})
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        url=f'http://127.0.0.1:{httpd.server_port}'
        assert httpd.server_port!=8766
        try:
            with sync_playwright() as playwright:
                browser=getattr(playwright,engine).launch()
                page=browser.new_page(viewport={'width':1190,'height':954})
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def settle():
                    page.evaluate('document.activeElement?.blur()')
                    page.wait_for_function('mutationCount===0 && !renderPending')
                def menu(name):
                    page.locator('#projectDetail .action-menu summary').click()
                    page.locator('#projectDetail .action-menu').get_by_role('button',name=name,exact=True).click()
                page.add_init_script("localStorage.setItem('tracking-time-range','all');localStorage.setItem('tracking-task-sort','manual')")
                page.goto(url)
                page.wait_for_function('state.projects?.length===1')
                page.locator('#projectsTab').click()
                menu('保存为任务模板')
                settle()
                assert server.read_data()['projects'][0]['template_snapshot']['tasks'][0]['text']=='模板事项'
                page.locator('#newProjectBtn').click()
                form=page.locator('#entityForm')
                assert form.locator('[name=template_id]').input_value()=='source'
                assert '1 个里程碑 · 1 项' in page.locator('#entityTemplateHint').inner_text()
                form.locator('[name=name]').fill('新项目')
                form.locator('[name=prd_url]').fill('javascript:alert(1)')
                form.get_by_role('button',name='保存',exact=True).click()
                assert 'http' in page.locator('#entityError').inner_text()
                assert form.locator('[name=name]').input_value()=='新项目'
                form.locator('[name=prd_url]').fill('https://example.com/prd?a=1&b=2')
                form.locator('[name=materials_url]').fill('https://example.com/material')
                form.get_by_role('button',name='保存',exact=True).click()
                page.locator('#entityDialog').wait_for(state='hidden')
                settle()
                data=server.read_data()
                project=next(p for p in data['projects'] if p['name']=='新项目')
                task=next(t for t in data['tasks'] if t['project_id']==project['id'])
                assert task['owner']=='Ocean' and task['deadline'] is None and not task['done']
                assert next(t for t in data['tasks'] if t['id']=='task')==original
                link=page.locator('a.project-resource').filter(has_text='PRD')
                assert link.get_attribute('href')==project['prd_url'] and link.get_attribute('rel')=='noopener noreferrer'
                page.reload()
                page.locator(f'#projectList [data-project-id="{project["id"]}"]').click()
                assert page.locator('a.project-resource').count()==2
                page.locator('.project-heading h2').click()
                server.patch_entity('projects',project['id'],{'name':'另一端项目名','owner':'另一端负责人','goal':'另一端目标','core':True})
                form.locator('[name=prd_url]').fill('')
                form.get_by_role('button',name='保存',exact=True).click()
                page.locator('#entityDialog').wait_for(state='hidden')
                settle()
                saved=next(p for p in server.read_data()['projects'] if p['id']==project['id'])
                assert [saved[k] for k in ('name','owner','goal','core')]==['另一端项目名','另一端负责人','另一端目标',True]
                page.locator('button.project-resource').filter(has_text='PRD').click()
                assert form.locator('[name=prd_url]').evaluate('node=>node===document.activeElement')
                page.locator('#entityCancel').click()
                settle()
                row=page.locator(f'#projectDetail [data-id="{task["id"]}"]')
                trigger=row.locator('.person-picker-toggle')
                for width in (1190,696):
                    page.set_viewport_size({'width':width,'height':954})
                    page.evaluate('() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    trigger.click()
                    popup=page.get_by_role('listbox',name='选择负责人')
                    bounds=popup.bounding_box()
                    assert bounds['width']>=200 and bounds['x']>=0 and bounds['x']+bounds['width']<=width,bounds
                    assert trigger.bounding_box()['width']>=36
                    assert popup.get_by_role('option',name='王同事',exact=True).bounding_box()['height']>=36
                    page.keyboard.press('Escape')
                    popup.wait_for(state='detached')
                trigger.click()
                page.get_by_role('button',name='确认负责人',exact=True).focus()
                page.keyboard.press('Tab')
                assert row.locator('.deadline-input').evaluate('node=>node===document.activeElement')
                trigger.click()
                page.keyboard.press('Shift+Tab')
                assert row.locator('.owner-input').evaluate('node=>node===document.activeElement')
                trigger.click()
                page.get_by_role('option',name='Ocean',exact=True).click()
                page.get_by_role('option',name='王同事',exact=True).click()
                page.get_by_role('button',name='确认负责人',exact=True).click()
                settle()
                assert next(t for t in server.read_data()['tasks'] if t['id']==task['id'])['owner']=='王同事'
                field=row.locator('[data-field=owner]')
                field.fill('自由输入同事')
                field.press('Enter')
                settle()
                assert next(t for t in server.read_data()['tasks'] if t['id']==task['id'])['owner']=='自由输入同事'
                page.locator('#thingsTab').click()
                assert page.locator('#groups .inline-task .text').first.evaluate('node=>getComputedStyle(node).fontSize')=='13px'
                assert page.locator('#groups .owner-input').first.evaluate('node=>getComputedStyle(node).fontSize')=='13px'
                assert not errors,errors
                print(f'PASS {engine}: frozen template/new project/links and validation/clear/refresh/owner input and spacious picker/696px bounds/uniform row fonts')
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

if __name__=='__main__':
    main()
