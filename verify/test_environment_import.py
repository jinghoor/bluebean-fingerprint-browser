"""Import regressions use disposable profiles and the native-command fixture only."""
import json
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from verify.test_env_management import fixture
from environment_import import EnvironmentImports
from env_management import EnvironmentManagement, ManagementError, atomic_json, definition


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bluebean-import-')
        self.bridge = fixture(self.tmp.name)
        self.m = self.bridge.env_management
        self.service = self.m.imports
        self.baseline = self.bridge.backend.env_state()

    def tearDown(self): self.tmp.cleanup()

    def preview(self, kind='environments', **values):
        return self.service.dispatch({'action': 'preview', 'request_id': secrets.token_hex(16),
                                      'kind': kind, **values})['job']

    def finish(self, token):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            j = self.service.dispatch({'job_id': token})['job']
            if j['status'] not in ('running', 'cancelling'): return j
            time.sleep(.01)
        self.fail('Import did not finish')

    def apply(self, j, action='apply'):
        self.service.dispatch({'action': action, 'job_id': j['id']})
        return self.finish(j['id'])

    def test_generate_preview_is_readonly_and_fingerprints_are_isolated(self):
        j = self.preview(generate={'prefix': '运营-', 'start': 20, 'count': 6})
        self.assertEqual(self.bridge.backend.env_state(), self.baseline)
        raw = self.service.jobs[j['id']]['rows']
        self.assertEqual(len({r['config']['env_id'] for r in raw}), 6)
        self.assertEqual(len({r['config']['fingerprint_seed'] for r in raw}), 6)
        self.assertTrue(all(len(r['config']['fingerprint_seed']) == 64 for r in raw))
        self.assertNotIn('fingerprint_seed', json.dumps(j))
        self.assertEqual(self.apply(j)['done'], 6)
        state = self.bridge.backend.env_state()
        self.assertEqual(state['active'], self.baseline['active'])
        self.assertEqual(len(state['envs']), 9)
        self.assertEqual(self.apply(j)['done'], 6)
        self.assertEqual(self.bridge.backend.env_state(), state)
        self.assertEqual(self.m.dispatch('import', {'job_id': j['id']})['job']['status'], 'complete')

    def test_csv_tsv_bom_quotes_and_validation(self):
        j = self.preview(text='\ufeff环境名称,分组,代理\n"运营,甲",销售,direct\n运营乙,销售,system\n')
        self.assertEqual(j['invalid'], 0)
        self.assertEqual(j['rows'][0]['label'], '运营,甲')
        self.assertEqual(self.apply(j)['done'], 2)
        j = self.preview(text='环境名称\t分组\n表格\t运营\n')
        self.assertEqual(self.apply(j)['done'], 1)
        for text in ('环境名称,环境名称\na,b', '名称\na', '环境名称,分组\na,b,c', '环境名称\n"bad'):
            with self.subTest(text=text), self.assertRaises(ValueError): self.preview(text=text)
        j = self.preview(text='环境名称,代理\n重复,direct\n重复,direct\n坏代理,http://user:never-leak@localhost:80')
        self.assertEqual(j['invalid'], 2)
        self.assertNotIn('never-leak', json.dumps(j))
        with self.assertRaises(ValueError): self.apply(j)
        for count in (0, 201, True):
            with self.assertRaises(ValueError): self.preview(generate={'prefix': '批量', 'count': count})
        with self.assertRaises(ValueError): self.preview(generate={'prefix':'x','count':2,'proxies':'direct'})

    def test_proxy_revision_conflicts_and_no_implicit_overwrite(self):
        j = self.preview('proxies', text='环境,代理\nfb-008,socks5://localhost:1080\nfb-020,http://localhost:8090')
        original = next(r for r in self.m.list()['envs'] if r['env_id']=='fb-008')
        self.m.save({**definition(original), 'revision':original['revision'], 'notes':'并发修改'})
        done = self.apply(j)
        self.assertEqual((done['done'], done['failed']), (1, 1))
        rows = {r['env_id']:r for r in self.bridge.backend.env_state()['envs']}
        self.assertEqual(rows['fb-008']['proxy'], 'direct')
        self.assertEqual(rows['fb-020']['proxy'], 'http://localhost:8090')
        self.assertEqual(self.apply(j)['done'], 1)
        j = self.preview('proxies', text='环境,代理\nfb-008,direct\nfb-008,system')
        self.assertEqual(j['invalid'], 1)

    def test_accounts_many_encrypted_and_retry_deduplicated(self):
        secret = 'password-never-plaintext'
        otp = 'JBSWY3DPEHPK3PXP'
        j = self.preview('accounts', text=f'环境,平台,登录账号,密码,2FA密钥\nfb-008,Facebook,a@example.test,{secret},{otp}\nfb-008,instagram,b@example.test,other-secret,')
        self.assertEqual(j['invalid'], 0)
        self.assertNotIn(secret, json.dumps(j)); self.assertNotIn(otp, json.dumps(j))
        disk = ''.join(p.read_text(encoding='utf-8') for p in Path(self.tmp.name).rglob('*.json'))
        self.assertNotIn(secret, disk); self.assertNotIn(otp, disk)
        store = self.m._workspace(); original = store.save; calls = []
        def flaky(env, a):
            calls.append(a['login'])
            if a['platform_id'] == 'instagram': raise OSError('secret transport detail')
            return original(env, a)
        with patch.object(store, 'save', flaky): done = self.apply(j)
        self.assertEqual((done['done'], done['failed']), (1, 1))
        self.assertNotIn('secret transport detail', json.dumps(done))
        self.assertEqual(self.apply(j, 'retry')['done'], 2)
        records = store.records('fb-008')
        self.assertEqual(len(records), 2)
        self.assertEqual(len({r['session_env_id'] for r in records}), 2)
        self.assertEqual(records[0]['password'], secret)
        reloaded = EnvironmentImports(self.m)
        self.assertEqual(reloaded.dispatch({'action':'retry','job_id':j['id']})['job']['done'],2)
        self.assertEqual(len(store.list('fb-008')), 2)
        duplicate = self.preview('accounts', text='环境,平台,登录账号\nfb-008,facebook,a@example.test')
        self.assertEqual(duplicate['invalid'], 1)
        self.assertEqual(self.bridge.backend.env_state(), self.baseline)
        disk = ''.join(p.read_text(encoding='utf-8') for p in Path(self.tmp.name).rglob('*.json'))
        for value in (secret, otp, 'other-secret'): self.assertNotIn(value, disk)

    def test_cancel_resume_lost_response_and_restart_readback(self):
        j = self.preview(generate={'prefix': 'cancel-', 'count': 4})
        started = threading.Event(); release = threading.Event(); base = self.m._command
        calls = []
        def command(c):
            calls.append(c['env_id']); result = base(c)
            if len(calls) == 1:
                started.set(); release.wait(3)
                raise TimeoutError('response lost after native commit')
            return result
        self.m._command = command
        self.service.dispatch({'action':'apply','job_id':j['id']})
        self.assertTrue(started.wait(3)); self.assertTrue(self.service.busy())
        self.service.dispatch({'action':'apply','job_id':j['id']})
        self.service.dispatch({'action':'cancel','job_id':j['id']})
        release.set(); done = self.finish(j['id'])
        self.assertEqual((done['status'],done['done']),('cancelled',1))
        self.assertEqual(len(calls),1); self.assertFalse(self.service.busy())
        self.assertEqual(self.service.dispatch({'action':'apply','job_id':j['id']})['job']['status'],'cancelled')
        # Simulate a persisted 'running' row after process interruption. Native result is durable.
        raw=self.service.jobs[j['id']];raw['status']='running';raw['rows'][0]['status']='running';self.service._write(raw)
        self.m.imports=EnvironmentImports(self.m);self.service=self.m.imports
        self.assertEqual(self.service.dispatch({'job_id':j['id']})['job']['status'],'interrupted')
        self.assertEqual(self.apply(j, 'retry')['done'],4)
        self.assertEqual(len(calls),4, 'readback must not recreate the committed first row')
        state=self.bridge.backend.env_state()
        self.assertEqual(state['active'],self.baseline['active'])
        self.assertEqual(len(state['envs']),7)

    def test_preview_request_id_and_plan_survive_reload(self):
        rid=secrets.token_hex(16)
        payload={'action':'preview','kind':'environments','request_id':rid,'generate':{'prefix':'safe-','count':2}}
        first=self.service.dispatch(payload)['job']
        self.m.imports=EnvironmentImports(self.m); self.service=self.m.imports
        self.assertEqual(first,self.service.dispatch(payload)['job'])
        self.assertEqual(self.apply(first)['done'],2)
        self.assertIsNone(self.service.dispatch({'action':'discard','job_id':rid})['job'])
        with self.assertRaises(ValueError):self.service.dispatch({'job_id':'../../envs'})

    def test_history_recovers_without_window_token_and_never_exposes_credentials(self):
        self.assertEqual(self.service.dispatch({'action':'history'})['jobs'], [])
        j = self.preview('accounts', text='环境,平台,登录账号,密码,2FA密钥\nfb-008,facebook,history-private-login,password-hidden,JBSWY3DPEHPK3PXP')
        self.assertEqual(self.apply(j)['done'], 1)
        reloaded = EnvironmentImports(self.m)
        entries = reloaded.dispatch({'action':'history'})['jobs']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['id'], j['id'])
        self.assertEqual(entries[0]['done'], 1)
        for text in ('history-private-login', 'password-hidden', 'JBSWY3DPEHPK3PXP', 'rows'):
            self.assertNotIn(text, json.dumps(entries))
        self.assertEqual(len(reloaded.jobs), 0, 'history must not keep unrelated decrypted plans in memory')
        reloaded.dispatch({'action':'discard','job_id':j['id']})
        self.assertEqual(len(self.m._workspace().list('fb-008')), 1)
        self.assertEqual(self.bridge.backend.env_state(), self.baseline)
        broken = secrets.token_hex(16)
        (self.service.directory/(broken+'.json')).write_text('not valid encrypted data',encoding='utf-8')
        self.assertEqual(reloaded.dispatch({'action':'history'})['jobs'][0]['status'], 'unavailable')
        reloaded.dispatch({'action':'discard','job_id':broken})
        self.assertEqual(reloaded.dispatch({'action':'history'})['jobs'], [])

    def test_editing_a_historical_preview_restores_its_own_encrypted_input(self):
        # A quoted comma in the password is preserved exactly, including spaces.
        original='环境,平台,登录账号,密码\nfb-008,facebook,specific-batch-login," leading,password "\n'
        j=self.preview('accounts',text=original)
        self.assertEqual(j['invalid'],0)
        other=self.preview('proxies',text='环境,代理\nfb-012,direct')
        self.assertNotIn(' leading,password ',json.dumps(j))
        reloaded=EnvironmentImports(self.m)
        self.assertEqual(reloaded.dispatch({'action':'source','job_id':j['id']})['source']['text'],original)
        disk=(self.service.directory/(j['id']+'.json')).read_text(encoding='utf-8')
        self.assertNotIn('specific-batch-login',disk)
        self.apply(j)
        with self.assertRaises(ValueError):self.service.dispatch({'action':'source','job_id':j['id']})
        self.assertNotIn('source',self.service.jobs[j['id']])
        self.assertEqual(self.m._workspace().records('fb-008')[0]['password'],' leading,password ')
        generated=self.preview(generate={'prefix':'history-','count':2})
        self.assertEqual(self.service.dispatch({'action':'source','job_id':generated['id']})['source']['generate']['start'],1)

    def test_failed_task_start_preserves_preview_and_never_leaves_false_busy_state(self):
        j=self.preview(generate={'prefix':'start-','count':2})
        with patch.object(self.service,'_write',side_effect=OSError('disk full private info')):
            with self.assertRaisesRegex(ValueError,'未启动'):
                self.service.dispatch({'action':'apply','job_id':j['id']})
        self.assertFalse(self.service.busy())
        self.assertEqual(self.service.dispatch({'job_id':j['id']})['job']['status'],'preview')
        self.assertIsNotNone(self.service.dispatch({'action':'source','job_id':j['id']})['source'])
        with patch('environment_import.threading.Thread.start',side_effect=RuntimeError('cannot start worker')):
            with self.assertRaisesRegex(ValueError,'未启动'):
                self.service.dispatch({'action':'apply','job_id':j['id']})
        self.assertFalse(self.service.busy())
        self.assertEqual(self.apply(j)['done'],2)

    def test_failure_reasons_distinguish_retryable_connection_from_configuration_conflict(self):
        j = self.preview('proxies', text='环境,代理\nfb-008,http://localhost:8070\nfb-012,http://localhost:8071')
        row = next(r for r in self.m.list()['envs'] if r['env_id']=='fb-008')
        self.m.save({**definition(row), 'revision':row['revision'], 'notes':'不允许覆盖'})
        with patch.object(self.m, '_command', side_effect=ManagementError('private-host-password',503)):
            done = self.apply(j)
        self.assertEqual((done['processed'], done['resumable'], done['failed']), (2,1,2))
        self.assertEqual(done['rows'][0]['error_code'], 'config_conflict')
        self.assertFalse(done['rows'][0]['retryable'])
        self.assertEqual(done['rows'][1]['error_code'], 'connection')
        self.assertTrue(done['rows'][1]['retryable'])
        self.assertNotIn('private-host-password', json.dumps(done))
        done = self.apply(j, 'retry')
        self.assertEqual((done['done'], done['failed'], done['resumable']), (1,1,0))
        self.assertEqual(next(r for r in self.m.list()['envs'] if r['env_id']=='fb-008')['notes'], '不允许覆盖')
        with self.assertRaises(ValueError): self.apply(j,'retry')

    def test_large_preview_reads_account_session_metadata_once_and_keeps_target_resolution(self):
        state=self.bridge.backend.env_state()
        state['envs'] += [definition({'env_id':'large-'+str(i),'display_name':'大批量-'+str(i),'proxy':'direct'}) for i in range(1000)]
        atomic_json(self.bridge.backend.path,state)
        text='环境,代理\n'+'\n'.join('大批量-'+str(i)+',direct' for i in range(200))
        with patch.object(self.m,'internal_session_ids',wraps=self.m.internal_session_ids) as reads:
            j=self.preview('proxies',text=text)
        self.assertEqual(reads.call_count,1)
        self.assertEqual((j['total'],j['invalid']),(200,0))
        self.assertEqual(j['rows'][-1]['env_id'],'large-199')
        self.assertEqual(self.bridge.backend.env_state(),state)
        for generate in ('wrong', [1], True):
            with self.assertRaises(ValueError):self.preview(generate=generate)


if __name__ == '__main__': unittest.main()
