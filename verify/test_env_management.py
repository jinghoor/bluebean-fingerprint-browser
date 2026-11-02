"""Isolated regression suite. Native commands write only a temporary profile."""
import copy
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env_management import EnvironmentManagement, ManagementError, definition, revision, atomic_json
from local_account_vault import LocalAccountVault


class Backend:
    def __init__(self, root):
        self.path = Path(root) / 'envs.json'
        self.offline = False
        self._live_cache = None
        atomic_json(self.path, {'active': 'fb-020', 'envs': [
            {**definition({'env_id': name, 'display_name': name, 'proxy': 'direct'}), 'used': True}
            for name in ['fb-008', 'fb-012', 'fb-020']]})

    def env_state(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def live_state(self, max_age=3):
        if self.offline:
            raise ConnectionError('offline')
        return self.env_state()

    def runtime_state(self, max_age=1):
        if self.offline:
            raise ConnectionError('offline')
        state = self.env_state()
        return {'ok': True, 'active': state.get('active', ''),
                'running_envs': [row['env_id'] for row in state['envs'] if row.get('tabs')]}

    def command(self, command):
        state = self.env_state()
        env_id = command['env_id']
        if command['command'] == 'create':
            old = next((r for r in state['envs'] if r['env_id'] == env_id), {})
            row = {**old, **definition(command)}
            state['envs'] = [r for r in state['envs'] if r['env_id'] != env_id] + [row]
        elif command['command'] == 'delete':
            if state['active'] == env_id:
                return {'ok': False}
            state['envs'] = [r for r in state['envs'] if r['env_id'] != env_id]
        atomic_json(self.path, state)
        return {'ok': True}


def fixture(root, probe=None):
    backend = Backend(root)
    bridge = SimpleNamespace(backend=backend, account_vault=LocalAccountVault(Path(root) / 'accounts.json'))
    bridge.env_management = EnvironmentManagement(bridge, command=backend.command, probe=probe, background_live=False)
    return bridge


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bluebean-manage-test-')
        self.bridge = fixture(self.tmp.name, probe=lambda p: {'ok': True, 'exit_ip': '203.0.113.1',
            'latency_ms': 37, 'city': 'Test City', 'timezone': 'Etc/UTC', 'geo_source': 'fixture'})
        self.manager = self.bridge.env_management
        self.baseline = self.bridge.backend.env_state()

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, env_id='fb-008', **changes):
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == env_id)
        return self.manager.save({**definition(row), 'revision': row['revision'], **changes})

    def finished_job(self, job_id):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            job = self.manager.job({'job_id':job_id})['job']
            if job['status'] not in ('running','cancelling'): return job
            time.sleep(.02)
        self.fail('Proxy job did not finish')

    def test_persistence_and_active_preservation(self):
        result = self.edit(group='运营', tags='AI,海外', notes='保留', locale='zh-CN')
        self.assertTrue(result['ok'])
        disk = self.bridge.backend.env_state()
        self.assertEqual(disk['active'], 'fb-020')
        self.assertTrue(all(r['used'] for r in disk['envs']))
        self.assertEqual(next(r for r in disk['envs'] if r['env_id']=='fb-008')['group'], '运营')
        reloaded = EnvironmentManagement(self.bridge, command=self.bridge.backend.command)
        self.assertTrue(reloaded.list()['envs'])

    def test_validation_and_conflict_do_not_write(self):
        for changes in [{'start_url':'javascript:alert(1)'}, {'locale':'bad_lang'},
                        {'expected_exit_ip':'bad'}, {'proxy':'http://u:secret@host:80'},
                        {'display_name':''}, {'account':{'otp_secret':'123456'}}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ManagementError): self.edit(**changes)
                self.assertEqual(self.bridge.backend.env_state(), self.baseline)
        with self.assertRaises(ManagementError) as caught:
            self.manager.save({'env_id':'fb-008','display_name':'x','revision':'stale'})
        self.assertTrue(caught.exception.reply['conflict'])

    def test_http_auth_payload_errors_and_api_version(self):
        from http.server import ThreadingHTTPServer
        from lan_bridge import make_handler
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.bridge,'test-only'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(action,body,auth=True,length=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if auth: headers['Authorization']='Bearer test-only'
            if length is not None: headers['Content-Length']=str(length)
            conn.request('POST','/v1/lan/manage/'+action,body,headers)
            reply=conn.getresponse();status=reply.status;data=json.loads(reply.read());conn.close()
            return status,data
        try:
            self.assertEqual(post('list','{}',False)[0],401)
            status,data=post('list','{}');self.assertEqual(status,200);self.assertEqual(data['api_version'],2)
            self.assertEqual(post('list','[]')[0],400)
            self.assertEqual(post('save','{bad')[0],400)
            self.assertEqual(post('missing','{}')[0],404)
            self.assertEqual(post('save','',length=4*1024*1024+1)[0],413)
            self.assertEqual(post('proxy-job','{"action":"start","env_ids":[{}]}')[0],400)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_duplicate_names_and_ids(self):
        with self.assertRaises(ManagementError):
            self.manager.save({'env_id':'fb-008','display_name':'new','create_only':True})
        with self.assertRaises(ManagementError): self.edit(display_name='fb-012')
        self.assertTrue(self.edit(display_name='fb-012', allow_duplicate_name=True)['ok'])

    def test_lost_native_response_reads_back_without_replay(self):
        sends=[]
        def lost(command):
            sends.append(command)
            self.bridge.backend.command(command)
            raise TimeoutError('response lost after commit')
        self.manager._command=lost
        self.assertTrue(self.edit(notes='committed once')['ok'])
        self.assertEqual(len(sends),1)

    def test_offline_save_never_queues_a_native_mutation(self):
        self.manager._command=None
        self.bridge.backend.offline=True
        with patch('env_cli.send') as send:
            with self.assertRaises(ManagementError) as caught: self.edit(notes='do not queue')
        self.assertEqual(caught.exception.status,503)
        self.assertFalse(caught.exception.reply['outcome_unknown'])
        send.assert_not_called()
        self.assertEqual(self.bridge.backend.env_state(),self.baseline)

    def test_audit_failure_does_not_report_credentials_unsaved(self):
        with patch.object(self.manager,'_record',side_effect=OSError('disk full')):
            reply=self.edit(account={'password':'stored-once'})
        self.assertTrue(reply['ok'])
        self.assertTrue(reply['account_saved'])
        self.assertTrue(reply['warning'])

    def test_account_partial_retry_and_ciphertext(self):
        account = {'username':'local-user-unique','password':'local-secret-unique','otp_secret':'JBSWY3DPEHPK3PXP'}
        with patch.object(self.bridge.account_vault, 'save', side_effect=OSError('disk')):
            result = self.edit(notes='committed', account=account)
        self.assertTrue(result['config_saved'])
        self.assertTrue(result['partial'])
        self.assertFalse(result['ok'])
        self.assertTrue(self.manager.account({'env_id':'fb-008','account':account})['ok'])
        ciphertext = self.bridge.account_vault.path.read_text(encoding='utf-8')
        for value in account.values(): self.assertNotIn(value, ciphertext)
        summary = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
        self.assertTrue(summary['has_account'])
        self.assertNotIn('password', summary)
        self.assertNotIn('otp_secret', summary)
        self.manager.account({'env_id':'fb-008','account':{'username':''}})
        self.assertEqual(self.bridge.account_vault._read()['fb-008']['username'], '')
        self.assertEqual(self.bridge.account_vault._read()['fb-008']['password'], account['password'])
        self.manager.account({'env_id':'fb-008','action':'clear'})
        self.assertFalse(self.bridge.account_vault.summaries(['fb-008'])['fb-008']['has_account'])

    def test_offline_and_unreadable_vault_preserve_list(self):
        self.bridge.backend.offline = True
        with patch.object(self.bridge.account_vault, 'summaries', side_effect=OSError()):
            result = self.manager.list()
        self.assertEqual(len(result['envs']), 3)
        self.assertTrue(result['warning'])
        self.assertTrue(result['account_warning'])

    def test_account_only_save_works_offline_without_native_write(self):
        row = self.bridge.backend.env_state()['envs'][0]
        native_before = self.bridge.backend.path.read_bytes()
        self.manager._command = None;self.bridge.backend.offline = True
        with patch('env_cli.send') as send, patch.object(self.manager,'_send') as native:
            result = self.manager.save({**row,'revision':revision(row),'account':{'username':'offline-local','password':'local-only'}})
            native.assert_not_called();send.assert_not_called()
        self.assertTrue(result['ok']);self.assertFalse(result['config_changed'])
        self.assertTrue(result['account_saved']);self.assertEqual(result['restart_required'],[])
        self.assertEqual(result['revision'],revision(row))
        self.assertEqual(self.bridge.backend.path.read_bytes(),native_before)
        self.assertEqual(self.bridge.account_vault._read()['fb-008']['username'],'offline-local')
        self.assertEqual([e['action'] for e in self.manager._meta()['events']],['account_save'])

    def test_noop_save_has_no_native_or_audit_or_vault_writes(self):
        row = self.bridge.backend.env_state()['envs'][0]
        with patch.object(self.manager,'_send') as send, patch.object(self.manager,'_write_meta') as write, \
             patch.object(self.manager,'_record') as record, patch.object(self.bridge.account_vault,'save') as vault:
            result = self.manager.save({**row,'revision':revision(row)})
            send.assert_not_called();write.assert_not_called();record.assert_not_called();vault.assert_not_called()
        self.assertTrue(result['ok']);self.assertFalse(result['config_changed'])
        self.assertEqual(self.bridge.backend.env_state(),self.baseline)

    def test_account_only_failure_is_retryable_without_config_write(self):
        row = self.bridge.backend.env_state()['envs'][0]
        with patch.object(self.manager,'_send') as send, patch.object(self.bridge.account_vault,'save',side_effect=OSError('disk')):
            result = self.manager.save({**row,'revision':revision(row),'account':{'password':'pending'}})
            send.assert_not_called()
        self.assertFalse(result['ok']);self.assertTrue(result['partial'])
        self.assertFalse(result['config_changed']);self.assertTrue(result['config_saved'])
        self.assertFalse(result['account_saved']);self.assertIn('环境配置未变',result['error'])
        self.assertEqual(self.bridge.backend.env_state(),self.baseline)
        self.assertTrue(self.manager.account({'env_id':'fb-008','account':{'password':'pending'}})['ok'])

    def test_account_only_stale_revision_does_not_write_credentials(self):
        row = self.bridge.backend.env_state()['envs'][0]
        self.edit(notes='another editor')
        with patch.object(self.bridge.account_vault,'save') as vault:
            with self.assertRaises(ManagementError) as caught:
                self.manager.save({**row,'revision':revision(row),'account':{'password':'must-not-write'}})
            vault.assert_not_called()
        self.assertTrue(caught.exception.reply['conflict'])

    def test_existing_allowed_duplicate_name_does_not_block_account_only_save(self):
        self.edit(display_name='fb-012',allow_duplicate_name=True)
        self.bridge.backend.offline=True
        with patch.object(self.manager,'_send') as send:
            result=self.edit(account={'username':'local-name'})
            send.assert_not_called()
        self.assertTrue(result['ok']);self.assertFalse(result['config_changed'])

    def wait_live(self):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with self.manager._live_guard:
                if not self.manager._live_running: return
            time.sleep(.01)
        self.fail('background state refresh did not finish')

    def test_slow_native_does_not_block_list_and_reconnects(self):
        entered,release=threading.Event(),threading.Event()
        calls=[]
        def slow(max_age=3):
            calls.append(1);entered.set();release.wait(3)
            raise ConnectionError('native timeout')
        self.manager.background_live=True
        self.bridge.backend.live_state=slow
        try:
            start=time.monotonic()
            for _ in range(12):
                reply=self.manager.list()
                self.assertEqual(len(reply['envs']),3)
            self.assertLess(time.monotonic()-start,.5)
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(calls),1)
            self.assertEqual(reply['live_status'],'connecting')
        finally:
            release.set();self.wait_live()
        self.assertEqual(self.manager.list()['live_status'],'offline')
        self.assertEqual(len(calls),1,'offline polling must back off')
        self.bridge.backend.live_state=lambda max_age=3:self.bridge.backend.env_state()
        with self.manager._live_guard: self.manager._live_finished=0
        self.manager.list();self.wait_live()
        self.assertEqual(self.manager.list()['live_status'],'ready')

    def test_old_background_result_cannot_overwrite_a_saved_definition(self):
        entered,release=threading.Event(),threading.Event()
        snapshot=self.bridge.backend.env_state()
        def slow(max_age=3):
            entered.set();release.wait(3);return snapshot
        self.manager.background_live=True;self.bridge.backend.live_state=slow
        self.manager.list();self.assertTrue(entered.wait(1))
        try:
            row=snapshot['envs'][0]
            self.assertTrue(self.manager.save({**row,'revision':revision(row),'notes':'newer save'})['ok'])
        finally:
            release.set();self.wait_live()
        self.assertIsNone(self.manager._live_snapshot)
        self.assertEqual(next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')['notes'],'newer save')
        self.wait_live()

    def test_summary_cache_has_no_secrets_and_detects_external_changes(self):
        vault=self.bridge.account_vault
        vault.save('fb-008',{'username':'before','password':'unique-secret-cache-test'})
        vault._summary_cache=None
        with patch.object(vault,'_read',wraps=vault._read) as read:
            a=vault.summaries(['fb-008']);a['fb-008']['username']='tampered'
            self.assertEqual(vault.summaries(['fb-008'])['fb-008']['username'],'before')
            self.assertEqual(read.call_count,1)
            external=LocalAccountVault(vault.path)
            external.save('fb-008',{'username':'after'})
            self.assertEqual(vault.summaries(['fb-008'])['fb-008']['username'],'after')
            self.assertEqual(read.call_count,2)
            self.assertNotIn('unique-secret-cache-test',json.dumps(vault._summary_cache))
            self.assertNotIn('password',vault._summary_cache['fb-008'])
            vault.path.write_text('corrupt',encoding='utf-8')
            with self.assertRaises(ValueError): vault.summaries(['fb-008'])

    def test_usage_record_write_failure_does_not_hide_environment_list(self):
        with patch.object(self.manager,'_write_meta',side_effect=OSError('disk full')):
            reply=self.manager.list()
        self.assertTrue(reply['ok'])
        self.assertEqual(len(reply['envs']),3)
        self.assertIn('写入失败',reply['warning'])

    def test_schema_migration_and_future_rejection(self):
        atomic_json(self.manager.path, {'envs': {'fb-008': {'created_at':123}}})
        self.edit(notes='migration')
        self.assertEqual(self.manager._meta()['schema_version'], 1)
        self.assertEqual(self.manager._meta()['envs']['fb-008']['created_at'],123)
        atomic_json(self.manager.path, {'schema_version':99,'envs':{},'events':[]})
        before = self.bridge.backend.env_state()
        with self.assertRaises(ManagementError): self.edit(notes='do not write')
        self.assertEqual(before, self.bridge.backend.env_state())

    def test_restore_preview_conflict_selected_merge_and_no_secrets(self):
        self.edit(account={'username':'secret-user', 'password':'secret-password'})
        backup = self.manager.backup({})
        self.assertNotIn('secret-user', json.dumps(backup))
        self.assertNotIn('secret-password', json.dumps(backup))
        self.edit(notes='new')
        source = {'backup_id':backup['backup']['id']}
        preview = self.manager.restore(source)
        self.edit('fb-012', notes='after preview')
        with self.assertRaises(ManagementError):
            self.manager.restore({**source,'action':'apply','preview_token':preview['preview_token'],'env_ids':['fb-008']})
        preview = self.manager.restore(source)
        result = self.manager.restore({**source,'action':'apply','preview_token':preview['preview_token'],'env_ids':['fb-008']})
        self.assertTrue(result['ok'])
        state = self.bridge.backend.env_state()
        self.assertEqual(state['active'],'fb-020')
        self.assertEqual(next(r for r in state['envs'] if r['env_id']=='fb-012')['notes'],'after preview')
        self.assertTrue(self.bridge.account_vault.summaries(['fb-008'])['fb-008']['has_account'])

    def test_remove_guard_and_retained_files(self):
        marker = Path(self.tmp.name)/'Cookies'
        marker.write_bytes(b'untouched-cookie-marker')
        with self.assertRaises(ManagementError):
            self.manager.remove({'env_id':'fb-020','revision':revision(self.baseline['envs'][2])})
        self.edit(account={'password':'keep'})
        row = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
        self.assertTrue(self.manager.remove(row)['storage_preserved'])
        self.assertEqual(marker.read_bytes(), b'untouched-cookie-marker')
        self.assertTrue(self.bridge.account_vault.summaries(['fb-008'])['fb-008']['has_account'])
        self.assertTrue(self.manager.maintenance()['backups'])

    def test_proxy_outcomes_and_stale_results(self):
        self.edit(proxy='http://127.0.0.1:9988')
        result = self.manager.test_proxy({'env_id':'fb-008','proxy':'http://127.0.0.1:9988'})
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['latency_ms'],37)
        self.assertEqual(result['city'],'Test City')
        self.assertEqual(result['timezone'],'Etc/UTC')
        self.assertEqual(result['geo_source'],'fixture')
        self.edit(proxy='http://127.0.0.1:9989')
        row = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
        self.assertTrue(row['proxy_test']['stale'])
        self.assertEqual(self.manager.test_proxy({'proxy':'socks5://127.0.0.1:1'})['status'],'success')
        self.assertEqual(self.manager.test_proxy({'proxy':'direct'})['status'],'not_required')

    def test_proxy_test_from_editor_attaches_to_saved_environment(self):
        self.edit(proxy='http://127.0.0.1:9988')
        checked=self.manager.test_proxy({'proxy':'http://127.0.0.1:9988'})
        self.assertTrue(checked['receipt'])
        with self.assertRaises(ManagementError):
            self.manager.attach_proxy_test({'env_id':'fb-012','receipt':checked['receipt']})
        # A rejected association must not consume a valid result for the intended environment.
        self.manager.attach_proxy_test({'env_id':'fb-008','receipt':checked['receipt']})
        with self.assertRaises(ManagementError):
            self.manager.attach_proxy_test({'env_id':'fb-008','receipt':checked['receipt']})
        row=next(e for e in self.manager.list()['envs'] if e['env_id']=='fb-008')
        self.assertEqual(row['proxy_test']['status'],'success')
        self.assertFalse(row['proxy_test'].get('stale',False))

    def test_account_portable_package_is_encrypted_and_import_is_idempotent(self):
        accounts=self.manager._workspace()
        accounts.save_many('fb-008',[{'client_id':'portable-one','platform_id':'facebook',
            'login':'portable@example.test','password':'portable-password','password_action':'replace',
            'otp_secret':'JBSWY3DPEHPK3PXP','otp_secret_action':'replace'}])
        password='migration-passphrase-42'
        package=self.manager.portable({'action':'export','password':password})['document']
        encoded=json.dumps(package)
        for secret in ('portable-password','JBSWY3DPEHPK3PXP','portable@example.test'):
            self.assertNotIn(secret,encoded)
        imported_tmp=tempfile.TemporaryDirectory(prefix='bluebean-import-test-')
        try:
            target=fixture(imported_tmp.name).env_management
            with self.assertRaisesRegex(ValueError,'密码不正确'):
                target.portable({'action':'preview','password':'wrong-password-42','document':package})
            preview=target.portable({'action':'preview','password':password,'document':package})
            self.assertEqual(preview['environments'],[{'env_id':'fb-008','count':1,'exists':True}])
            result=target.portable({'action':'import','password':password,'document':package})
            self.assertTrue(result['ok'])
            record=target._workspace().records('fb-008')[0]
            self.assertEqual(record['password'],'portable-password')
            duplicate=target.portable({'action':'import','password':password,'document':package})
            self.assertFalse(duplicate['ok'])
            self.assertIn('已有同编号账号',duplicate['results'][0]['error'])
        finally:
            imported_tmp.cleanup()

    def test_internal_account_sessions_are_hidden_from_list_and_config_backup(self):
        row=self.manager._workspace().save_many('fb-008',[{'client_id':'internal-session',
            'platform_id':'facebook','login':'internal@example.test'}])['results'][0]['account']
        child=row['session_env_id']
        state=self.bridge.backend.env_state()
        state['envs'].append({**definition({'env_id':child,'display_name':'private account session'}),'used':True})
        atomic_json(self.bridge.backend.path,state)
        self.assertNotIn(child,{e['env_id'] for e in self.manager.list()['envs']})
        backup=self.manager.backup({})['document']
        self.assertNotIn(child,{e['env_id'] for e in backup['envs']})
        package=self.manager.portable({'action':'export','password':'migration-passphrase-42'})['document']
        self.assertNotIn(child,json.dumps(package))

    def test_history_restore_and_partial_restore_report_each_committed_row(self):
        self.edit('fb-008',group='before')
        self.edit('fb-008',group='after')
        versions=self.manager.history({'env_id':'fb-008'})['versions']
        restored=self.manager.history({'action':'restore','env_id':'fb-008',
            'version_id':versions[0]['id'],'revision':next(e for e in self.manager.list()['envs'] if e['env_id']=='fb-008')['revision']})
        self.assertTrue(restored['ok'])
        self.assertEqual(next(e for e in self.manager.list()['envs'] if e['env_id']=='fb-008')['group'],'before')

        backup=self.manager.backup({})['document']
        for env in ('fb-008','fb-012','fb-020'):
            self.edit(env,notes='changed-'+env)
        preview=self.manager.restore({'document':backup})
        selected=[change['env_id'] for change in preview['changes']]
        original_save=self.manager.save
        def fail_second(payload):
            if payload['env_id']=='fb-008': raise OSError('injected final restore failure')
            return original_save(payload)
        with patch.object(self.manager,'save',side_effect=fail_second):
            outcome=self.manager.restore({'document':backup,'action':'apply',
                'preview_token':preview['preview_token'],'env_ids':selected})
        self.assertFalse(outcome['ok'])
        self.assertEqual(len(outcome['results']),3)
        self.assertEqual([r['ok'] for r in outcome['results']],[True,True,False])
        self.assertEqual(outcome['results'][-1]['env_id'],'fb-008')

    def test_proxy_expected_mismatch_and_batch_result(self):
        self.edit(proxy='http://127.0.0.1:9988', expected_exit_ip='203.0.113.2')
        result = self.manager.test_proxy({'env_id':'fb-008', 'proxy':'http://127.0.0.1:9988'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'mismatch')
        self.assertEqual(result['exit_ip'], '203.0.113.1')
        self.assertEqual(result['expected_exit_ip'], '203.0.113.2')
        row = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
        self.assertEqual(row['proxy_test']['status'], 'mismatch')
        self.assertFalse(row['proxy_test'].get('stale', False))
        job = self.manager.job({'action':'start', 'env_ids':['fb-008']})['job']
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            job = self.manager.job({'job_id':job['id']})['job']
            if job['status']=='complete': break
            time.sleep(.02)
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['results'][0]['status'], 'mismatch')

    def test_changed_expected_ip_invalidates_proxy_result(self):
        self.edit(proxy='http://127.0.0.1:9988', expected_exit_ip='203.0.113.1')
        self.assertTrue(self.manager.test_proxy({'env_id':'fb-008', 'proxy':'http://127.0.0.1:9988'})['ok'])
        self.edit(expected_exit_ip='203.0.113.2')
        row = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
        self.assertTrue(row['proxy_test']['stale'])

    def test_ipv6_expected_comparison_and_freshness_are_semantic(self):
        self.edit(proxy='http://127.0.0.1:9988', expected_exit_ip='2001:0DB8:0000:0000:0000:0000:0000:0001')
        self.manager._probe = lambda p: {'ok':True, 'exit_ip':'2001:db8::1'}
        result = self.manager.test_proxy({'env_id':'fb-008', 'proxy':'http://127.0.0.1:9988',
                                          'expected_exit_ip':'2001:db8::1'})
        self.assertTrue(result['ok'])
        row = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
        self.assertFalse(row['proxy_test'].get('stale', False))

    def test_config_changed_during_proxy_probe_does_not_store_result(self):
        for changes in ({'expected_exit_ip':'203.0.113.2'}, {'proxy':'http://127.0.0.1:9989'}):
            with self.subTest(changes=changes):
                self.edit(proxy='http://127.0.0.1:9988', expected_exit_ip='203.0.113.1')
                def probe(proxy):
                    self.edit(**changes)
                    return {'ok':True, 'exit_ip':'203.0.113.1'}
                self.manager._probe = probe
                self.manager.test_proxy({'env_id':'fb-008', 'proxy':'http://127.0.0.1:9988'})
                row = next(r for r in self.manager.list()['envs'] if r['env_id']=='fb-008')
                self.assertNotIn('proxy_test', row)

    def test_invalid_expected_ip_does_not_probe_or_write(self):
        before = self.bridge.backend.env_state()
        with patch.object(self.manager, '_probe') as probe:
            with self.assertRaises(ManagementError) as caught:
                self.manager.test_proxy({'proxy':'http://127.0.0.1:9988', 'expected_exit_ip':'invalid'})
            probe.assert_not_called()
        self.assertIn('expected_exit_ip', caught.exception.reply['field_errors'])
        self.assertEqual(self.bridge.backend.env_state(), before)

    def test_cancel_bounded_concurrency_and_no_duplicate_job(self):
        entered, release = threading.Event(), threading.Event()
        running = [0,0]
        guard = threading.Lock()
        def probe(proxy):
            with guard:
                running[0] += 1; running[1] = max(running)
            entered.set(); release.wait(3)
            with guard: running[0] -= 1
            return {'ok':True,'exit_ip':'203.0.113.1'}
        self.manager._probe = probe
        for i in range(10):
            self.manager.save({'env_id':f't-{i}','display_name':f'Test {i}','proxy':'http://127.0.0.1:1','create_only':True})
        job = self.manager.job({'action':'start','env_ids':[f't-{i}' for i in range(10)]})['job']
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ManagementError): self.manager.job({'action':'start','env_ids':['t-0']})
        self.manager.job({'action':'cancel','job_id':job['id']}); release.set()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.manager.job({'job_id':job['id']})['job']
            if result['status']=='cancelled': break
            time.sleep(.02)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['done'],10)
        self.assertLessEqual(running[1],4)
        self.assertTrue(any(r['status']=='cancelled' for r in result['results']))

    def test_proxy_job_request_replay_does_not_repeat_probe(self):
        self.edit(proxy='http://127.0.0.1:9988')
        entered, release = threading.Event(), threading.Event()
        calls = []
        def probe(proxy):
            calls.append(proxy); entered.set(); release.wait(3)
            return {'ok':True, 'exit_ip':'203.0.113.1'}
        self.manager._probe = probe
        body = {'action':'start', 'env_ids':['fb-008'], 'request_id':'replay-test'}
        job = self.manager.job(body)['job']
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.manager.job(body)['job']['id'], job['id'])
        finally: release.set()
        self.finished_job(job['id'])
        self.assertEqual(self.manager.job(body)['job']['id'], job['id'])
        self.assertEqual(len(calls),1)

    def test_existing_proxy_job_discovery_and_scope_are_explicit(self):
        entered, release = threading.Event(), threading.Event()
        self.edit(proxy='http://127.0.0.1:9988')
        def probe(proxy):
            entered.set(); release.wait(3)
            return {'ok':True, 'exit_ip':'203.0.113.1'}
        self.manager._probe = probe
        job = self.manager.job({'action':'start', 'env_ids':['fb-008']})['job']
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ManagementError) as caught:
                self.manager.job({'action':'start','env_ids':['fb-012']})
            self.assertEqual(caught.exception.status,409)
            self.assertEqual(caught.exception.reply['job']['env_ids'],['fb-008'])
            current = self.manager.job({})['job']
            self.assertEqual(current['id'],job['id'])
            current['env_ids'].append('fake-id')
            self.assertEqual(self.manager.job({})['job']['env_ids'],['fb-008'])
            self.assertIsNone(self.manager.job({'job_id':'expired'})['job'])
        finally: release.set()
        self.finished_job(job['id'])

    def test_proxy_job_bad_actions_are_rejected_without_start(self):
        for payload in ({'action':'bad'}, {'job_id':[]}, {'action':'start','request_id':{},'env_ids':['fb-008']},
                        {'action':'start','env_ids':['missing']}):
            with self.subTest(payload=payload):
                with self.assertRaises(ManagementError): self.manager.job(payload)
        self.assertEqual(self.manager.jobs,{})

    def test_record_write_failure_preserves_proxy_result_and_job_completion(self):
        self.edit(proxy='http://127.0.0.1:9988')
        with patch.object(self.manager,'_write_meta',side_effect=OSError('disk full')):
            result = self.manager.test_proxy({'env_id':'fb-008','proxy':'http://127.0.0.1:9988'})
        self.assertTrue(result['ok']);self.assertEqual(result['exit_ip'],'203.0.113.1')
        self.assertTrue(result['warning'])
        with patch.object(self.manager,'_record',side_effect=OSError('disk full')):
            job = self.manager.job({'action':'start','env_ids':['fb-008','fb-012']})['job']
            done = self.finished_job(job['id'])
        self.assertEqual(done['status'],'complete');self.assertEqual(done['done'],2)
        self.assertTrue(done['warning'])
        self.assertEqual(next(r for r in done['results'] if r['env_id']=='fb-008')['status'],'success')

    def test_one_proxy_failure_does_not_stop_other_checks_or_leak_exception(self):
        self.edit(proxy='http://127.0.0.1:9988')
        self.edit('fb-012',proxy='http://127.0.0.1:9989')
        def probe(proxy):
            if proxy.endswith('9988'): raise OSError('private-proxy-secret')
            return {'ok':True,'exit_ip':'203.0.113.1'}
        self.manager._probe = probe
        job = self.manager.job({'action':'start','env_ids':['fb-008','fb-012']})['job']
        done = self.finished_job(job['id'])
        self.assertEqual(done['done'],2);self.assertEqual(done['status'],'complete')
        self.assertEqual({r['status'] for r in done['results']},{'success','failed'})
        self.assertNotIn('private-proxy-secret',json.dumps(done))


if __name__ == '__main__': unittest.main(verbosity=2)
