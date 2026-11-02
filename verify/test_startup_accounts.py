"""Startup account portal: scoped metadata, local TOTP and verified parent reuse."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from env_startup_check import StartupChecks, make_server, SITES, EXIT_SOURCES
from environment_accounts import AccountStore, totp
from local_account_vault import LocalAccountVault

KEY = 'a' * 32
SECRET = 'JBSWY3DPEHPK3PXP'
PROBE_COUNT = len(SITES)+len(EXIT_SOURCES)

class StartupAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bluebean-startup-accounts-')
        self.root = Path(self.temp.name)
        self.store = AccountStore(LocalAccountVault(self.root/'env-account-vault.dpapi.json'))
        self.account = self.store.save('parent', {'platform_id':'facebook', 'login':'test@example.test',
            'password':'never-expose-this', 'otp_secret':SECRET})
        self.other = self.store.save('other', {'platform_id':'tiktok', 'login':'other@example.test'})
        self.rows = {'parent': {'ok':True, 'env_id':'parent', 'proxy':'http://127.0.0.1:9001',
            'expected_exit_ip':'203.0.113.1', 'ip_mode':'static', 'unlocked':False}}
        self.calls = []; self.open_calls = []
        self.checks = StartupChecks(self.root, self.command, self.probe, self.store, self.open_account)
    def tearDown(self): self.temp.cleanup()
    def command(self, payload):
        if payload.get('key') != KEY or payload.get('env_id') not in self.rows:
            return {'ok':False,'error':'expired'}
        row = self.rows[payload['env_id']]
        if payload['action'] == 'unlock': row['unlocked'] = True
        return copy.deepcopy(row)
    def probe(self, proxy, url):
        self.calls.append((proxy,url))
        return {'ok':True,'status':200,'body':'{"ip":"203.0.113.1"}','latency_ms':12}
    def open_account(self, payload):
        self.open_calls.append(payload)
        return {'ok':True,'session_env_id':self.account['session_env_id']}
    def finish(self, env='parent'):
        self.checks.start(env,KEY)
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            row = self.checks.status(env,KEY)
            if row.get('unlocked'): return row
            time.sleep(.01)
        self.fail('check did not unlock')
    def test_metadata_omits_password_secret_and_code(self):
        row=self.checks.accounts('parent',KEY)
        text=json.dumps(row)
        self.assertNotIn('never-expose-this',text); self.assertNotIn(SECRET,text)
        self.assertNotIn('otp_secret',text); self.assertNotIn('code',row['accounts'][0])
        self.assertTrue(row['accounts'][0]['has_password'])
        self.assertEqual(len(row['accounts']),1)
        self.assertTrue(row['accounts'][0]['has_otp'])
        self.assertTrue(row['accounts'][0]['icon'].startswith('data:image/svg+xml'))
        self.assertFalse(row['allowed']); self.assertEqual(self.calls,[])
    def test_local_totp_is_scoped_and_has_expiry(self):
        row=self.checks.account_action('parent',KEY,'totp',self.account['account_id'])
        self.assertEqual(row['code'],totp(SECRET,now=row['server_time'])['code'])
        self.assertTrue(0 < row['expires_at']-row['server_time'] <= 30)
        self.assertNotIn(SECRET,json.dumps(row)); self.assertEqual(self.calls,[])
    def test_foreign_account_and_wrong_capability_rejected(self):
        for action in ('open','totp','credential'):
            with self.assertRaises(ValueError): self.checks.account_action('parent',KEY,action,self.other['account_id'])
        with self.assertRaises(ValueError): self.checks.accounts('parent','b'*32)
        self.assertEqual(self.open_calls,[])
    def test_password_is_single_field_expiring_scoped_and_never_persisted(self):
        # Network risk confirmation gates navigation, not local credential access.
        result=self.checks.account_action('parent',KEY,'credential',self.account['account_id'])
        self.assertEqual(result['value'],'never-expose-this')
        self.assertEqual(result['field'],'password')
        self.assertEqual(result['expires_at']-result['server_time'],15)
        self.assertNotIn(SECRET,json.dumps(result))
        self.assertEqual(self.calls,[]); self.assertEqual(self.open_calls,[])
        with self.assertRaises(ValueError):
            self.checks.account_action('parent',KEY,'credential',self.account['account_id'],field='otp_secret')
        with self.assertRaises(ValueError):
            self.checks.account_action('parent','b'*32,'credential',self.account['account_id'])
        self.assertFalse((self.root/'env-check').exists())
    def test_child_portal_only_lists_and_reveals_its_own_account(self):
        sibling=self.store.save('parent',{'platform_id':'tiktok','login':'sibling','password':'sibling-pass','otp_secret':SECRET})
        child=self.account['session_env_id'];self.rows[child]={**self.rows['parent'],'env_id':child}
        self.assertEqual([a['account_id'] for a in self.checks.accounts(child,KEY)['accounts']],[self.account['account_id']])
        self.assertEqual(self.checks.account_action(child,KEY,'credential',self.account['account_id'])['value'],'never-expose-this')
        for action in ('open','totp','credential'):
            with self.assertRaises(ValueError):self.checks.account_action(child,KEY,action,sibling['account_id'])
    def test_no_password_and_expired_environment_fail_closed(self):
        no=self.store.save('parent',{'platform_id':'tiktok','login':'no-password'})
        with self.assertRaisesRegex(ValueError,'尚未保存密码'):
            self.checks.account_action('parent',KEY,'credential',no['account_id'])
        del self.rows['parent']
        with self.assertRaises(ValueError):
            self.checks.account_action('parent',KEY,'credential',self.account['account_id'])
    def test_http_secrets_require_scoped_post_and_same_origin_and_are_not_cached(self):
        server=make_server(self.root,command=self.command,probe=self.probe,account_store=self.store,account_command=self.open_account)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
        def post(path,data,origin=None):
            connection.request('POST',path,json.dumps(data),{'Content-Type':'application/json',
                'Origin':origin or f'http://127.0.0.1:{server.server_port}'})
            response=connection.getresponse();return response,response.read()
        payload={'env':'parent','key':KEY,'account_id':self.account['account_id'],'action':'credential'}
        try:
            response,body=post('/accounts',payload)
            self.assertEqual(response.status,200);self.assertNotIn(b'never-expose-this',body);self.assertNotIn(SECRET.encode(),body)
            response,body=post('/account',payload,origin='https://foreign.example')
            self.assertEqual(response.status,403);self.assertNotIn(b'never-expose-this',body)
            response,body=post('/account',{**payload,'key':'b'*32})
            self.assertEqual(response.status,409);self.assertNotIn(b'never-expose-this',body)
            response,body=post('/account',{**payload,'field':'otp_secret'})
            self.assertEqual(response.status,409);self.assertNotIn(SECRET.encode(),body)
            response,body=post('/account',payload)
            self.assertEqual(response.status,200);self.assertEqual(response.getheader('Cache-Control'),'no-store')
            self.assertEqual(json.loads(body)['value'],'never-expose-this');self.assertNotIn(SECRET.encode(),body)
            response,body=post('/account',{**payload,'action':'totp'})
            self.assertEqual(response.status,200);self.assertNotIn(b'never-expose-this',body);self.assertNotIn(SECRET.encode(),body)
        finally:
            connection.close();server.shutdown();server.server_close();thread.join()
    def test_locked_open_never_dispatches(self):
        with self.assertRaises(ValueError): self.checks.account_action('parent',KEY,'open',self.account['account_id'])
        self.assertEqual(self.open_calls,[])
    def test_open_uses_existing_account_session_service(self):
        self.finish()
        self.checks.account_action('parent',KEY,'open',self.account['account_id'])
        self.assertEqual(self.open_calls,[{'env_id':'parent','account_id':self.account['account_id'],'action':'open'}])
    def test_account_partition_reuses_only_matching_parent_check(self):
        self.finish(); before=len(self.calls)
        child=self.account['session_env_id']; self.rows[child]={**self.rows['parent'],'env_id':child,'unlocked':False}
        row=self.finish(child)
        self.assertTrue(row['inherited']); self.assertEqual(row['auto_open_url'],'https://www.facebook.com/')
        self.assertEqual(len(self.calls),before)
    def test_changed_account_proxy_does_not_inherit(self):
        self.finish(); child=self.account['session_env_id']
        self.rows[child]={**self.rows['parent'],'env_id':child,'proxy':'http://127.0.0.1:9002','unlocked':False}
        row=self.finish(child)
        self.assertFalse(row.get('inherited',False));self.assertEqual(len(self.calls),2*PROBE_COUNT)
    def test_closed_parent_does_not_prevent_independent_child_check(self):
        self.finish(); child=self.account['session_env_id']
        self.rows[child]={**self.rows['parent'],'env_id':child,'unlocked':False};del self.rows['parent']
        row=self.finish(child)
        self.assertFalse(row.get('inherited',False)); self.assertEqual(len(self.calls),2*PROBE_COUNT)
    def test_refresh_and_service_restart_preserve_existing_native_approval(self):
        self.finish();self.assertEqual(len(self.calls),PROBE_COUNT)
        self.checks.start('parent',KEY);self.assertEqual(len(self.calls),PROBE_COUNT)
        fresh=StartupChecks(self.root,self.command,self.probe,self.store,self.open_account)
        row=fresh.start('parent',KEY)
        self.assertTrue(row['unlocked']);self.assertEqual(len(self.calls),PROBE_COUNT)
        fresh.start('parent',KEY,force=True)
        deadline=time.monotonic()+3
        while len(self.calls)<2*PROBE_COUNT and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(len(self.calls),2*PROBE_COUNT)
    def test_no_totp_secret_has_clear_error(self):
        no=self.store.save('parent',{'platform_id':'tiktok','login':'no-2fa'})
        with self.assertRaisesRegex(ValueError,'尚未设置'):
            self.checks.account_action('parent',KEY,'totp',no['account_id'])

if __name__ == '__main__': unittest.main()
