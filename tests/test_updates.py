"""Update contracts: architecture selection, duplicate requests, atomic app rollback."""
import importlib
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import server


class ReleaseSelectionTests(unittest.TestCase):
    def test_selects_compatible_zip_instead_of_first_architecture(self):
        choose = getattr(server, 'select_release_asset', None)
        self.assertIsNotNone(choose, 'Old updater blindly takes the first ZIP')
        assets = [{'name': 'app-x86_64.zip'}, {'name': 'app-arm64.zip'}]
        self.assertEqual(choose({'assets': assets}, machine='arm64')['name'], 'app-arm64.zip')
        self.assertEqual(choose({'assets': assets}, machine='x86_64')['name'], 'app-x86_64.zip')
        assets.append({'name': 'app-universal.zip'})
        self.assertEqual(choose({'assets': assets}, machine='arm64')['name'], 'app-universal.zip')
        with self.assertRaises(ValueError):
            choose({'assets': assets[:1]}, machine='arm64')

    def test_busy_update_does_not_start_another_download(self):
        with patch.object(server, 'RELEASE_CHANNEL', 'stable'), patch.dict(os.environ, {'TODO_DISABLE_UPDATE':'0', 'TODO_PREVIEW':'0'}):
            server._set_progress(status='downloading')
            try:
                with patch.object(server, 'fetch_latest_release', side_effect=AssertionError('must not fetch twice')):
                    result = server.install_update()
                self.assertTrue(result['started'])
                self.assertTrue(result['already_running'])
            finally:
                server._set_progress(status='idle')

    def test_new_release_channel_allows_updates_and_preview_still_blocks(self):
        self.assertEqual(server.RELEASE_CHANNEL, 'stable')
        with patch.dict(os.environ, {'TODO_DISABLE_UPDATE':'1'}):
            with self.assertRaises(ValueError):
                server.install_update()


class AppTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.plan={'app':str(root/'Applications/每日待办.app'),
                   'staged_app':str(root/'stage/每日待办.app'),
                   'backup_app':str(root/'backup/每日待办.app'),
                   'data_dir':str(root/'data'), 'version':'1.2.0', 'port':18766,
                   'home':str(root), 'server_pid':99999999}
        for key,version in [('app','1.1.4'),('backup_app','1.1.4'),('staged_app','1.2.0')]:
            app=Path(self.plan[key]); (app/'Contents').mkdir(parents=True)
            (app/'marker').write_text(version)
            with (app/'Contents/Info.plist').open('wb') as f:
                plistlib.dump({'CFBundleIdentifier':'com.ocean.dailytodo','CFBundleShortVersionString':version},f)
        data=Path(self.plan['data_dir']); data.mkdir()
        (data/'todos.json').write_text('{"tasks":[{"id":"keep"}],"projects":[{"id":"project"}]}')
        self.original=(data/'todos.json').read_bytes()

    def module(self):
        try:
            return importlib.import_module('app_update')
        except ModuleNotFoundError:
            self.fail('Missing transactional updater; old updater deletes the only App')

    def test_success_retains_previous_app_and_data(self):
        module=self.module(); system=FakeSystem(self.plan)
        result=module.apply_update(self.plan,system=system)
        self.assertEqual(result['status'],'installed')
        self.assertEqual((Path(self.plan['app'])/'marker').read_text(),'1.2.0')
        self.assertEqual((Path(self.plan['backup_app'])/'marker').read_text(),'1.1.4')
        self.assertEqual((Path(self.plan['data_dir'])/'todos.json').read_bytes(),self.original)

    def test_unhealthy_new_app_restores_app_and_plist_without_reverting_data(self):
        module=self.module(); system=FakeSystem(self.plan, unhealthy=True)
        plist=Path(self.plan['home'])/'Library/LaunchAgents/com.ocean.daily-todo-server.plist'
        plist.parent.mkdir(parents=True); plist.write_bytes(b'original configuration')
        with self.assertRaisesRegex(RuntimeError,'rolled back'):
            module.apply_update(self.plan,system=system,attempts=2)
        self.assertEqual((Path(self.plan['app'])/'marker').read_text(),'1.1.4')
        self.assertEqual(plist.read_bytes(),b'original configuration')
        self.assertIn(b'late-user-edit',(Path(self.plan['data_dir'])/'todos.json').read_bytes())

    def test_snapshot_failure_reopens_original_app(self):
        module=self.module(); system=FakeSystem(self.plan)
        original_write=Path.write_bytes
        def fail_snapshot(path, body):
            if path.name=='todos.json' and path.parent.name=='configuration':
                raise OSError('disk full')
            return original_write(path,body)
        with patch.object(Path,'write_bytes',fail_snapshot):
            with self.assertRaisesRegex(RuntimeError,'rolled back'):
                module.apply_update(self.plan,system=system)
        self.assertIn('1.1.4',system.opened)

    def test_success_restores_previously_loaded_periodic_sync(self):
        module=self.module(); system=FakeSystem(self.plan)
        plist=Path(self.plan['home'])/'Library/LaunchAgents/com.ocean.daily-todo-sync.plist'
        plist.parent.mkdir(parents=True); plist.write_bytes(b'sync config')
        module.apply_update(self.plan,system=system)
        self.assertIn(plist,system.resumed)

    def test_failed_service_restore_is_reported_as_incomplete_rollback(self):
        module=self.module(); system=FakeSystem(self.plan, unhealthy=True)
        plist=Path(self.plan['home'])/'Library/LaunchAgents/com.ocean.daily-todo-sync.plist'
        plist.parent.mkdir(parents=True); plist.write_bytes(b'sync config')
        system.resume=lambda _: (_ for _ in ()).throw(RuntimeError('bootstrap failed'))
        with self.assertRaisesRegex(RuntimeError,'rollback needs attention'):
            module.apply_update(self.plan,system=system,attempts=1)
        self.assertIn('bootstrap failed',(Path(self.plan['backup_app']).parent/'error.txt').read_text())

    def test_migration_must_commit_before_updater_reports_success(self):
        module=self.module(); system=FakeSystem(self.plan)
        healthy=system.health(self.plan['port'])
        calls=[]
        def health(port):
            calls.append(port)
            return healthy if len(calls)==1 else {**healthy, 'version':'1.1.4'}
        system.health=health
        with self.assertRaisesRegex(RuntimeError,'migration did not commit'):
            module.apply_update(self.plan,system=system,attempts=1)
        self.assertEqual((Path(self.plan['app'])/'marker').read_text(),'1.1.4')
        self.assertFalse((Path(self.plan['backup_app']).parent/'receipt.json').exists())

    def test_wrong_bundle_is_rejected_before_stopping_services(self):
        module=self.module(); system=FakeSystem(self.plan)
        p=Path(self.plan['staged_app'])/'Contents/Info.plist'
        with p.open('wb') as f: plistlib.dump({'CFBundleIdentifier':'other.app','CFBundleShortVersionString':'1.2.0'},f)
        with self.assertRaises(ValueError): module.apply_update(self.plan,system=system)
        self.assertFalse(system.stopped)
        self.assertEqual((Path(self.plan['app'])/'marker').read_text(),'1.1.4')


class FakeSystem:
    """Replace only OS process/signature boundaries; transaction files remain real."""
    def __init__(self,plan,unhealthy=False): self.plan=plan; self.unhealthy=unhealthy; self.stopped=False; self.opened=[]; self.resumed=[]
    def assert_quiet(self,plan): pass
    def verify_signature(self,app): pass
    def stop(self,plan): self.stopped=True
    def running_labels(self): return {"com.ocean.daily-todo-sync"}
    def open(self,app):
        self.opened.append((app/"marker").read_text())
        if self.unhealthy and (app/'marker').read_text()=='1.2.0':
            p=Path(self.plan['home'])/'Library/LaunchAgents/com.ocean.daily-todo-server.plist'
            p.write_bytes(b'new configuration')
            (Path(self.plan['data_dir'])/'todos.json').write_text('{"tasks":[{"id":"late-user-edit"}]}')
    def health(self,port):
        return {'ok':True,'app':'daily-todo','version':'wrong' if self.unhealthy else '1.2.0',
                'channel':'stable','preview':False,'data_file':self.plan['data_dir']+'/todos.json'}
    def sleep(self,seconds): pass
    def resume(self,plist): self.resumed.append(plist)


if __name__=='__main__': unittest.main()
