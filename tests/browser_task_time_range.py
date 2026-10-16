"""Three-day task view, deadline sorting and independent project progress."""
import os
from pathlib import Path
import sys,tempfile,threading
from datetime import date,timedelta
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

def main():
    engine=os.environ.get('TODO_TEST_BROWSER','chromium')
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA_DIR=server.APP_SUPPORT_DIR=tmp;server.DATA_FILE=str(Path(tmp)/'todos.json')
        server.BACKUP_DIR=str(Path(tmp)/'backups');server.STATE_FILE=str(Path(tmp)/'state.json');server.schedule_sync=lambda:None
        os.environ['TODO_DISABLE_SYNC']='1';os.environ['TODO_DISABLE_UPDATE']='1'
        today=date.fromisoformat(server.today_str())
        def task(i,offset,**extra):return {'id':i,'text':i,'owner':'Ocean','priority':'P1','project_id':'p','milestone_id':'m','done':False,'deadline':(today+timedelta(days=offset)).isoformat() if offset is not None else None,'created':today.isoformat()+'T10:00:00+08:00',**extra}
        fixture=[task('d2',2,sort_order=0),task('d0',0,sort_order=4),task('d1',1,sort_order=2),task('d3',3),task('late',-1),task('undated',None),task('recurring',None,recur_source='seed',project_id=None,milestone_id=None),task('done',-3,done=True,done_at=today.isoformat()+'T11:00:00+08:00'),task('gone',0,deleted_at='2020-01-01')]
        server.write_data({'projects':[{'id':'p','name':'项目','owner':'Ocean'}],'milestones':[{'id':'m','name':'全部事项','project_id':'p'}],'tasks':fixture})
        before=Path(server.DATA_FILE).read_bytes()
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser=getattr(playwright,engine).launch();page=browser.new_page(viewport={'width':1064,'height':954},timezone_id='America/Los_Angeles')
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{httpd.server_port}');page.locator('#group-P1').wait_for()
                def ids():return page.locator('#group-P1 li').evaluate_all('nodes=>nodes.map(n=>n.dataset.id)')
                def settle():page.wait_for_function('mutationCount===0 && !renderPending')
                assert page.locator('#taskTimeRange').input_value()=='near'
                assert page.locator('#taskSort').input_value()=='deadline'
                assert ids()==['d0','recurring','d1','d2'],ids()
                assert page.locator('#group-P1 .drag-handle').count()==0
                assert page.locator('#groups [data-id=d0] .association-project-label').inner_text()=='项目'
                assert page.locator('#group-DN [data-id=done]').count()==1
                page.locator('#taskTimeRange').select_option('overdue');settle();assert ids()==['late']
                page.locator('#taskTimeRange').select_option('undated');settle();assert ids()==['undated']
                page.locator('#taskTimeRange').select_option('all');settle();assert ids()==['late','d0','recurring','d1','d2','d3','undated']
                page.locator('#taskSort').select_option('manual');settle();assert page.locator('#group-P1 .drag-handle').count()==7
                page.locator('#taskSort').select_option('deadline');page.locator('#taskTimeRange').select_option('near');settle()
                page.locator('#projectsTab').click()
                assert page.locator('#projectDetail li.inline-task').count()==6
                assert '1/7' in page.locator('#projectDetail .project-progress').inner_text()
                page.locator('#thingsTab').click();assert ids()==['d0','recurring','d1','d2']
                page.reload();page.locator('#group-P1').wait_for();assert ids()==['d0','recurring','d1','d2']
                assert Path(server.DATA_FILE).read_bytes()==before,'Filters must not migrate or overwrite any data'
                # Window follows Beijing date rollover even with unchanged data revision.
                tomorrow=(today+timedelta(days=1)).isoformat()
                page.evaluate('(value)=>{todayStr=()=>value;requestRender();}',tomorrow)
                assert page.locator('#taskTimeRange').input_value()=='near'
                assert ids()==['d1','d2','d3'],ids()
                assert not errors,errors
                print(f'PASS {engine}: default today+2/deadline sort/manual mode/overdue+undated access/recurring/today completed/project progress/reload/midnight/zero data writes')
                browser.close()
        finally:httpd.shutdown();httpd.server_close()
if __name__=='__main__':main()
