"""DSH management security/reliability tests; every write uses a temporary profile."""
import copy
import hashlib
import hmac
import http.client
import json
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from verify.test_env_management import fixture
from env_management import ManagementError, revision
from environment_agent import EnvironmentAgentService
from environment_api import TOOLS, catalogue, openapi
from session_browser_scope import SessionBrowserScopes


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bluebean-agent-test-')
        self.bridge=fixture(self.tmp.name,probe=lambda p:{'ok':True,'exit_ip':'203.0.113.2','latency_ms':37,'city':'Fixture','timezone':'Etc/UTC'})
        self.bridge.permissions={'kill_switch':False}
        self.bridge.execution_lock=threading.RLock()
        self.bridge.browser_scopes=SessionBrowserScopes(Path(self.tmp.name)/'scopes.json')
        self.service=EnvironmentAgentService(self.bridge,'test-key')
        self.manager=self.bridge.env_management
        self.session='test-session'
    def tearDown(self): self.tmp.cleanup()
    def prepare(self,tool,args=None,rid=None):
        return self.service.dispatch({'phase':'prepare','tool':tool,'arguments':args or {},'session_id':self.session,'request_id':rid or secrets.token_hex(32)})
    def phase(self,plan,phase='execute',approve=False,**extra):
        payload={'phase':phase,'session_id':self.session,'request_id':plan['request_id'],**extra}
        if approve:
            message=json.dumps([self.session,plan['request_id'],plan['digest'],'allow'],ensure_ascii=False,separators=(',',':')).encode()
            payload['approval_grant']=hmac.new(b'test-key',message,hashlib.sha256).hexdigest()
        return self.service.dispatch(payload)
    def run_tool(self,tool,args=None,approve=False):
        plan=self.prepare(tool,args)
        return plan if plan['status']=='complete' else self.phase(plan,approve=approve)
    def row(self,env='fb-008'): return next(r for r in self.manager.list()['envs'] if r['env_id']==env)
    def account(self):
        reply=self.run_tool('environment_accounts',{'action':'save','env_id':'fb-008','accounts':[{'platform_id':'facebook','login':'agent@example.test','password':'never-show-this-secret','otp_secret':'JBSWY3DPEHPK3PXP'}]})
        self.assertTrue(reply['ok'],reply)
        return reply['result']['results'][0]['account']
    def wait_import(self,job):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            reply=self.run_tool('environment_import',{'action':'status','job_id':job})['result']['job']
            if reply['status'] not in ('running','cancelling'): return reply
            time.sleep(.02)
        self.fail('import stalled')

    def test_catalogue_and_generated_schemas_match(self):
        generated=json.loads((Path(__file__).resolve().parents[1]/'lan_plugin/environment-tools.json').read_text(encoding='utf-8'))
        self.assertEqual(generated,TOOLS)
        self.assertEqual(len(TOOLS),22)
        self.assertEqual(len(openapi()['components']['schemas']),22)
        self.assertIn('accounts',catalogue()['import_examples'])
        self.assertTrue(catalogue()['proxy_auth'])
    def test_query_pagination_get_and_no_secrets(self):
        self.account()
        page=self.run_tool('environment_list',{'limit':1,'include_accounts':False})['result']
        self.assertEqual(page['total'],3);self.assertEqual(page['next_offset'],1)
        self.assertNotIn('accounts',page['envs'][0])
        data=self.run_tool('environment_get',{'env_id':'fb-008'})['result']
        self.assertNotIn('never-show-this-secret',json.dumps(data))
        self.assertEqual(data['environment']['env_id'],'fb-008')
        for tool in ('environment_catalog','environment_status','environment_maintenance'):
            self.assertTrue(self.run_tool(tool)['ok'])
    def test_create_is_independent_idempotent_and_does_not_activate(self):
        rid=secrets.token_hex(32)
        plan=self.prepare('environment_save',{'display_name':'Agent new'},rid)
        first=self.phase(plan);second=self.phase(plan)
        self.assertTrue(first['ok'],first);self.assertEqual(first['result'],second['result'])
        self.assertEqual(self.bridge.backend.env_state()['active'],'fb-020')
        self.assertEqual(len(self.manager._state()['envs']),4)
        self.assertEqual(self.prepare('environment_save',{'display_name':'Agent new'},rid)['status'],'complete')
        with self.assertRaises(ManagementError): self.prepare('environment_save',{'display_name':'different'},rid)
        other=self.run_tool('environment_save',{'display_name':'Another'})
        rows=[r for r in self.manager._state()['envs'] if r['env_id'].startswith('env-')]
        self.assertTrue(other['ok']);self.assertNotEqual(rows[0]['fingerprint_seed'],rows[1]['fingerprint_seed'])
        self.assertEqual(self.bridge.browser_scopes.get(self.session)['env_ids'],[])
    def test_simple_edit_and_bulk_update(self):
        row=self.row()
        self.assertTrue(self.run_tool('environment_save',{'env_id':row['env_id'],'revision':row['revision'],'notes':'new note'})['ok'])
        reply=self.run_tool('environment_bulk_update',{'env_ids':['fb-008','fb-012'],'fields':{'group':'Agent group','tags':'AI'}})
        self.assertTrue(reply['ok'],reply);self.assertEqual(len(reply['result']['results']),2)

    def test_dynamic_ip_change_requires_user_approval(self):
        row = self.row()
        plan = self.prepare('environment_save', {'env_id': row['env_id'],
                            'revision': row['revision'], 'ip_mode': 'dynamic'})
        self.assertIn('dynamic_ip', [risk['code'] for risk in plan['risks']])
        with self.assertRaises(ManagementError):
            self.phase(plan)
        reply = self.phase(plan, approve=True)
        self.assertTrue(reply['ok'])
        self.assertEqual(self.row()['ip_mode'], 'dynamic')
    def test_create_with_initial_accounts_returns_verified_configuration(self):
        plan=self.prepare('environment_save',{'display_name':'Combined','initial_accounts':[
            {'platform_id':'Facebook','login':'fb@example.test','password':'initial-secret'},
            {'platform_id':'IG','login':'ig@example.test'}]})
        reply=self.phase(plan);self.assertTrue(reply['ok'],reply)
        row=reply['result']['environment']
        self.assertEqual({a['platform_id'] for a in row['accounts']},{'facebook','instagram'})
        self.assertEqual(row['revision'],revision(next(r for r in self.manager._state()['envs'] if r['env_id']==row['env_id'])))
        self.assertNotIn('initial-secret',json.dumps(reply))
        self.assertEqual(self.phase(plan)['result'],reply['result'])
        self.assertEqual(len(self.manager._state()['envs']),4)
    def test_invalid_initial_accounts_never_leave_an_empty_environment(self):
        before=self.manager._state()
        for accounts in ([{'platform_id':'missing','login':'bad'}],
            [{'platform_id':'facebook','login':'same'}]*2,
            [{'platform_id':'facebook','login':'otp','otp_secret':'123456'}]):
            with self.assertRaises(ManagementError): self.prepare('environment_save',{'display_name':'Invalid','initial_accounts':accounts})
        self.assertEqual(self.manager._state(),before)
        row=self.row()
        with self.assertRaises(ManagementError): self.prepare('environment_save',{'env_id':'fb-008','revision':row['revision'],
            'initial_accounts':[{'platform_id':'facebook','login':'new'}]})
    def test_initial_account_partial_keeps_env_and_successful_account(self):
        plan=self.prepare('environment_save',{'display_name':'Partial initial','initial_accounts':[
            {'platform_id':'facebook','login':'first'},{'platform_id':'instagram','login':'second'}]})
        store=self.manager._workspace();save=store.save
        def fail_second(env,a):
            if a['login']=='second': raise OSError('disk full')
            return save(env,a)
        with patch.object(store,'save',side_effect=fail_second): reply=self.phase(plan)
        result=reply['result'];self.assertFalse(reply['ok']);self.assertTrue(result['config_saved'])
        self.assertEqual(result['error_code'],'PARTIAL_RESULT')
        self.assertEqual(len(result['environment']['accounts']),1)
        self.assertEqual([r['ok'] for r in result['initial_account_results']],[True,False])
        self.assertEqual(self.phase(plan)['result'],result)
    def test_agent_lists_omit_repeated_platforms_and_version_history(self):
        row=self.row();self.manager.save({'env_id':'fb-008','revision':row['revision'],'notes':'change'})
        page=self.run_tool('environment_list')['result']
        self.assertNotIn('platforms',page)
        self.assertTrue(all('versions' not in r for r in page['envs']))
        self.assertNotIn('versions',self.run_tool('environment_get',{'env_id':'fb-008'})['result']['environment'])
        self.assertTrue(self.run_tool('environment_history',{'env_id':'fb-008'})['result']['versions'])
    def test_platform_aliases_for_separate_multi_account_save(self):
        reply=self.run_tool('environment_accounts',{'env_id':'fb-008','action':'save','accounts':[
            {'platform_id':'脸书','login':'alias'},{'platform_id':'Twitter','login':'tweet'}]})
        self.assertTrue(reply['ok'])
        self.assertEqual({a['platform_id'] for a in self.manager._workspace().list('fb-008')},{'facebook','x'})
    def test_delete_requires_real_bound_grant_not_confirm_boolean(self):
        row=self.row();plan=self.prepare('environment_delete',{'env_id':row['env_id'],'revision':row['revision']})
        self.assertTrue(plan['requires_approval'])
        for extra in ({'confirm':True},{'approval_grant':'forged'}):
            with self.assertRaises(ManagementError) as caught: self.phase(plan,**extra)
            self.assertEqual(caught.exception.status,403)
        self.assertEqual(len(self.manager._state()['envs']),3)
        self.assertTrue(self.phase(plan,approve=True)['ok'])
        self.assertEqual(len(self.manager._state()['envs']),2)
        self.assertTrue(list(self.manager.backup_dir.glob('*.json')))
    def test_decline_cancel_and_cross_session_protection(self):
        row=self.row();plan=self.prepare('environment_delete',{'env_id':row['env_id'],'revision':row['revision']})
        with self.assertRaises(ManagementError): self.phase(plan,'status',session_id='other')
        self.assertEqual(self.phase(plan,'cancel')['status'],'cancelled')
        self.assertFalse(self.phase(plan,approve=True)['ok'])
        self.assertEqual(len(self.manager._state()['envs']),3)
    def test_approval_signature_cannot_be_reused_for_another_plan(self):
        row=self.row();args={'env_id':row['env_id'],'revision':row['revision']}
        first=self.prepare('environment_delete',args);second=self.prepare('environment_delete',args)
        saved=self.service._read(first['request_id'],self.session)
        with self.assertRaises(ManagementError): self.phase(second,approval_grant=self.service._grant(saved))
        self.assertEqual(len(self.manager._state()['envs']),3)
    def test_revision_rechecked_after_approval(self):
        row=self.row();plan=self.prepare('environment_delete',{'env_id':row['env_id'],'revision':row['revision']})
        self.manager.save({'env_id':row['env_id'],'revision':row['revision'],'notes':'external edit'})
        reply=self.phase(plan,approve=True)
        self.assertFalse(reply['ok']);self.assertTrue(reply['result']['conflict'])
        self.assertEqual(self.row()['notes'],'external edit')
    def test_fingerprint_and_site_changes_are_risky(self):
        row=self.row()
        plan=self.prepare('environment_save',{'env_id':row['env_id'],'revision':row['revision'],'fingerprint_seed':'different'})
        self.assertTrue(plan['requires_approval'])
        a=self.account()
        plan=self.prepare('environment_accounts',{'env_id':'fb-008','action':'save','accounts':[{'account_id':a['account_id'],'revision':a['revision'],'url':'https://other.example.test'}]})
        self.assertIn('account_site_change',[r['code'] for r in plan['risks']])
    def test_account_edit_delete_and_sensitive_result_one_delivery(self):
        a=self.account()
        plan=self.prepare('environment_accounts',{'env_id':'fb-008','action':'credential','account_id':a['account_id'],'field':'password'})
        with self.assertRaises(ManagementError): self.phase(plan)
        reply=self.phase(plan,approve=True)
        self.assertEqual(reply['result']['value'],'never-show-this-secret')
        for response in (self.phase(plan),self.phase(plan,'status'),self.run_tool('environment_operation_status',{'request_id':plan['request_id']})):
            self.assertNotIn('never-show-this-secret',json.dumps(response))
        raw=''.join(p.read_text() for p in self.service.directory.glob('*.json'))
        self.assertNotIn('never-show-this-secret',raw)
        self.assertNotIn('never-show-this-secret',self.manager.path.read_text())
        edit=self.prepare('environment_accounts',{'env_id':'fb-008','action':'save','accounts':[{'account_id':a['account_id'],'revision':a['revision'],'password_action':'clear'}]})
        self.assertTrue(edit['requires_approval']);self.assertTrue(self.phase(edit,approve=True)['ok'])
        a=self.manager._workspace().list('fb-008')[0]
        delete=self.run_tool('environment_accounts',{'env_id':'fb-008','action':'delete','account_id':a['account_id'],'revision':a['revision']},approve=True)
        self.assertTrue(delete['ok']);self.assertEqual(self.manager._workspace().list('fb-008'),[])
    def test_multi_account_partial_results_preserved(self):
        reply=self.run_tool('environment_accounts',{'env_id':'fb-008','action':'save','accounts':[
            {'platform_id':'instagram','login':'ok@example.test'},{'platform_id':'missing','login':'bad@example.test'}]})
        self.assertFalse(reply['ok']);self.assertEqual([r['ok'] for r in reply['result']['results']],[True,False])
    def test_account_revision_and_identity_gate(self):
        a=self.account()
        plan=self.prepare('environment_accounts',{'env_id':'fb-008','action':'save','accounts':[{'account_id':a['account_id'],'revision':a['revision'],'login':'new@example.test'}]})
        self.assertTrue(plan['requires_approval'])
        self.assertTrue(self.phase(plan,approve=True)['ok'])
        current=self.manager._workspace().list('fb-008')[0]
        self.assertFalse(current['has_password']);self.assertNotEqual(current['session_env_id'],a['session_env_id'])
        with self.assertRaises(ManagementError): self.prepare('environment_accounts',{'env_id':'fb-008','action':'delete','account_id':a['account_id'],'revision':a['revision']})
    def test_browser_scope_denied_and_revoked_before_execute(self):
        with self.assertRaises(ManagementError): self.prepare('environment_open',{'env_id':'fb-008'})
        a=self.account()
        with self.assertRaises(ManagementError): self.prepare('environment_accounts',{'env_id':'fb-008','action':'fill','account_id':a['account_id']})
        self.bridge.browser_scopes.set(self.session,['fb-008'],{'fb-008'})
        plan=self.prepare('environment_open',{'env_id':'fb-008'})
        self.bridge.browser_scopes.set(self.session,[],{'fb-008'})
        with patch.object(self.manager,'dispatch') as dispatch:
            reply=self.phase(plan);dispatch.assert_not_called()
        self.assertFalse(reply['ok']);self.assertTrue(reply['result']['scope_denied'])
    def test_account_selection_is_per_chat_and_open_ignores_other_ui_selection(self):
        first=self.account();store=self.manager._workspace()
        second=store.save('fb-008',{'platform_id':'instagram','login':'second@example.test'})
        scopes=self.bridge.browser_scopes;self.bridge.ensure_live=lambda env:None
        for sid,account in ((self.session,first),('other-chat',second)):
            scopes.set(sid,['fb-008'],{'fb-008'})
            scopes.bind_account(sid,'fb-008',account['session_env_id'])
        meta=self.manager._meta();meta['envs'].setdefault('fb-008',{})['selected_account_session']=second['session_env_id'];self.manager._write_meta(meta)
        result=self.run_tool('environment_open',{'env_id':'fb-008'})
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['result']['session_env_id'],first['session_env_id'])
        self.assertEqual(self.manager.agent_session(scopes,'other-chat','fb-008'),second['session_env_id'])

    def test_deleted_account_binding_fails_without_falling_back(self):
        a=self.account();scopes=self.bridge.browser_scopes;store=self.manager._workspace()
        scopes.set(self.session,['fb-008'],{'fb-008'});scopes.bind_account(self.session,'fb-008',a['session_env_id'])
        store.delete('fb-008',a['account_id'],a['revision'])
        with self.assertRaises(ManagementError):self.manager.agent_session(scopes,self.session,'fb-008')
        self.bridge.ensure_live=lambda env:self.fail('Must not activate a fallback account')
        self.assertFalse(self.run_tool('environment_open',{'env_id':'fb-008'})['ok'])

    def test_changed_identity_invalidates_bound_browser_target(self):
        a=self.account();scopes=self.bridge.browser_scopes
        scopes.set(self.session,['fb-008'],{'fb-008'});scopes.bind_account(self.session,'fb-008',a['session_env_id'])
        self.manager._workspace().save('fb-008',{'account_id':a['account_id'],'revision':a['revision'],
            'login':'changed@example.test','confirm_identity_change':True})
        with self.assertRaises(ManagementError):self.manager.agent_session(scopes,self.session,'fb-008')

    def test_browser_tasks_keep_chat_target_and_stop_after_account_removed(self):
        from lan_bridge import Bridge
        a=self.account();store=self.manager._workspace();scopes=self.bridge.browser_scopes
        other=store.save('fb-008',{'platform_id':'facebook','login':'another@example.test'})
        b=Bridge.__new__(Bridge);b.__dict__.update(self.bridge.__dict__);calls=[]
        b.ensure_live=lambda env:calls.append(env)
        b.run_step=lambda env,step,sid:{'step':'read'}
        for sid,account in ((self.session,a),('other',other)):
            scopes.set(sid,['fb-008'],{'fb-008'});scopes.bind_account(sid,'fb-008',account['session_env_id'])
        with patch('lan_bridge.audit'):
            for sid,account in ((self.session,a),('other',other),(self.session,a)):
                self.assertTrue(b.run_task({'env':'fb-008','steps':[{'read':{}}]},session_id=sid)['ok'])
                self.assertEqual(calls[-1],account['session_env_id'])
            def remove_after_first(env,step,sid):
                store.delete('fb-008',a['account_id'],a['revision']);return {'step':'read'}
            b.run_step=remove_after_first
            result=b.run_task({'env':'fb-008','steps':[{'read':{}},{'read':{}}]},session_id=self.session)
            self.assertFalse(result['ok'])
            before=len(calls)
            result=b.run_task({'env':'fb-008','steps':[{'read':{}}]},session_id=self.session)
            self.assertEqual(len(calls),before)
            self.assertEqual(result['per_env']['fb-008']['error_code'],'ACCOUNT_TARGET_CHANGED')

    def test_fill_binds_chat_target_and_updates_actual_ui_selection(self):
        a=self.account();scopes=self.bridge.browser_scopes
        scopes.set(self.session,['fb-008'],{'fb-008'});self.bridge.ensure_live=lambda env:None
        self.bridge.backend._cdp=lambda env:SimpleNamespace(evaluate=lambda script:'https://www.facebook.com/login/' if script=='location.href' else {'filled':2,'status':'login_required'})
        result=self.run_tool('environment_accounts',{'env_id':'fb-008','action':'fill','account_id':a['account_id']})
        self.assertTrue(result['ok'],result)
        self.assertEqual(scopes.selected_account(self.session,'fb-008'),a['session_env_id'])
        self.assertEqual(self.manager.selected_session('fb-008'),a['session_env_id'])

    def test_kill_switch_and_expiry(self):
        plan=self.prepare('environment_save',{'display_name':'blocked'})
        self.bridge.permissions['kill_switch']=True
        self.assertFalse(self.phase(plan)['ok'])
        self.assertTrue(self.run_tool('environment_list')['ok'])
        with self.assertRaises(ManagementError): self.prepare('environment_save',{'display_name':'no write'})
        self.bridge.permissions['kill_switch']=False
        saved=self.service._read(plan['request_id'],self.session);saved['status']='prepared';saved['expires_at']=0;self.service._write(saved)
        with self.assertRaises(ManagementError): self.phase(plan)
    def test_uncertain_restart_never_replays(self):
        plan=self.prepare('environment_save',{'display_name':'unconfirmed'})
        saved=self.service._read(plan['request_id'],self.session);saved['status']='executing';self.service._write(saved)
        self.service=EnvironmentAgentService(self.bridge,'test-key')
        with patch.object(self.manager,'dispatch') as dispatch:
            reply=self.phase(plan);dispatch.assert_not_called()
        self.assertEqual(reply['status'],'uncertain');self.assertTrue(reply['outcome_unknown'])
    def test_failed_durable_plan_does_not_mutate(self):
        with patch.object(self.service,'_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.prepare('environment_save',{'display_name':'not saved'})
        self.assertFalse(self.service.busy());self.assertEqual(len(self.manager._state()['envs']),3)
    def test_result_record_failure_becomes_uncertain_without_replay(self):
        plan=self.prepare('environment_save',{'display_name':'Saved before disk failure'})
        original=self.service._write
        def write(p):
            if p['status']=='complete': raise OSError('disk full')
            return original(p)
        with patch.object(self.service,'_write',side_effect=write): reply=self.phase(plan)
        self.assertTrue(reply['result']['outcome_unknown'])
        status=self.phase(plan,'status');self.assertEqual(status['status'],'uncertain')
        self.assertEqual(len(self.manager._state()['envs']),4)
        self.assertEqual(self.phase(plan)['status'],'uncertain')
        self.assertEqual(len(self.manager._state()['envs']),4)
    def test_slow_probe_does_not_block_gateway_status(self):
        plan=self.prepare('environment_save',{'display_name':'Pending'})
        entered=threading.Event();release=threading.Event();replies=[]
        def probe(p): entered.set();release.wait(3);return {'ok':True,'exit_ip':'203.0.113.2','latency_ms':30}
        self.manager._probe=probe
        worker=threading.Thread(target=lambda:replies.append(self.run_tool('environment_proxy_test',{'proxy':'http://127.0.0.1:8080'})))
        worker.start();self.assertTrue(entered.wait(2))
        try:
            started=time.monotonic();self.assertTrue(self.service.busy())
            self.assertEqual(self.phase(plan,'status')['status'],'prepared')
            self.assertLess(time.monotonic()-started,.5)
        finally: release.set();worker.join(3)
        self.assertTrue(replies[0]['ok'])
    def test_pending_execution_is_not_replayed_by_second_thread(self):
        plan=self.prepare('environment_save',{'display_name':'once'})
        entered=threading.Event();release=threading.Event();original=self.manager.dispatch
        def slow(*args): entered.set();release.wait(3);return original(*args)
        responses=[]
        with patch.object(self.manager,'dispatch',side_effect=slow):
            worker=threading.Thread(target=lambda:responses.append(self.phase(plan)));worker.start()
            self.assertTrue(entered.wait(2));self.assertTrue(self.service.busy())
            second=self.phase(plan);self.assertEqual(second['status'],'executing')
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(len(self.manager._state()['envs']),4)
    def test_proxy_real_adapter_and_job(self):
        row=self.row();self.manager.save({'env_id':'fb-008','revision':row['revision'],'proxy':'http://127.0.0.1:8080'})
        result=self.run_tool('environment_proxy_test',{'env_id':'fb-008'})['result']
        self.assertEqual(result['latency_ms'],37);self.assertEqual(result['timezone'],'Etc/UTC')
        job=self.run_tool('environment_proxy_job',{'action':'start','env_ids':['fb-008']})['result']['job']
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=self.run_tool('environment_proxy_job',{'action':'status','job_id':job['id']})['result']['job']
            if status['status']=='complete': break
            time.sleep(.02)
        self.assertEqual(status['done'],1)
    def test_proxy_wait_returns_terminal_result_without_locking_workers(self):
        row=self.row();self.manager.save({'env_id':'fb-008','revision':row['revision'],'proxy':'http://127.0.0.1:8080'})
        self.manager._probe=lambda p:(time.sleep(.1) or {'ok':True,'exit_ip':'203.0.113.2','latency_ms':40})
        result=self.run_tool('environment_proxy_job',{'action':'start','env_ids':['fb-008'],'wait_ms':1000})['result']
        self.assertTrue(result['task_complete']);self.assertTrue(result['all_succeeded'])
        self.assertEqual(result['job']['done'],1)
    def test_proxy_wait_zero_gives_actionable_poll_and_failure_is_not_success(self):
        row=self.row();self.manager.save({'env_id':'fb-008','revision':row['revision'],'proxy':'http://127.0.0.1:8080'})
        self.manager._probe=lambda p:(time.sleep(.15) or {'ok':False,'error':'fixture unreachable'})
        result=self.run_tool('environment_proxy_job',{'action':'start','env_ids':['fb-008'],'wait_ms':0})['result']
        self.assertFalse(result['task_complete']);self.assertFalse(result['all_succeeded'])
        self.assertEqual(result['next_action']['tool'],'environment_proxy_job')
        final=self.run_tool('environment_proxy_job',{'action':'status','job_id':result['job']['id'],'wait_ms':1000})['result']
        self.assertTrue(final['task_complete']);self.assertFalse(final['all_succeeded'])
    def test_progress_read_error_preserves_started_job_and_poll_target(self):
        original=self.manager.dispatch
        def status_fails(action,args):
            if action=='import' and args.get('action')=='status': raise ConnectionError('fixture progress failure')
            return original(action,args)
        preview=self.run_tool('environment_import',{'action':'preview','generate':{'prefix':'Wait','count':1}})['result']['job']
        entered=threading.Event();release=threading.Event();apply=self.manager.imports._apply
        def slow(*args): entered.set();release.wait(2);return apply(*args)
        try:
            with patch.object(self.manager.imports,'_apply',side_effect=slow),patch.object(self.manager,'dispatch',side_effect=status_fails):
                result=self.run_tool('environment_import',{'action':'apply','job_id':preview['id'],'wait_ms':150})['result']
                self.assertTrue(entered.is_set());self.assertTrue(result['ok'])
                self.assertFalse(result['task_complete']);self.assertEqual(result['next_action']['arguments']['job_id'],preview['id'])
                self.assertIn('progress_warning',result)
        finally: release.set()
        self.assertEqual(self.wait_import(preview['id'])['status'],'complete')
    def test_structured_conflict_and_scope_recovery(self):
        error=ManagementError('changed',409,conflict=True)
        reply=self.service.failure(error,'environment_save',{'env_id':'fb-008','password':'do-not-echo'})
        self.assertEqual(reply['error_code'],'REVISION_CONFLICT')
        self.assertEqual(reply['next_action']['tool'],'environment_get')
        self.assertNotIn('do-not-echo',json.dumps(reply))
    def test_batch_create_and_accounts_import(self):
        for kind,extra in [('environments',{'generate':{'prefix':'Batch','count':3}}),('accounts',{'text':'environment,platform_id,login,password\nfb-008,facebook,bulk@example.test,bulk-secret'})]:
            preview=self.run_tool('environment_import',{'action':'preview','kind':kind,**extra})['result']['job']
            reply=self.run_tool('environment_import',{'action':'apply','job_id':preview['id']})
            self.assertTrue(reply['ok'],reply)
            final=self.wait_import(preview['id']);self.assertEqual(final['status'],'complete',final)
        created=[r for r in self.manager._state()['envs'] if r['display_name'].startswith('Batch')]
        self.assertEqual(len({r['fingerprint_seed'] for r in created}),3)
        self.assertEqual(len(self.manager._workspace().list('fb-008')),1)
    def test_proxy_import_source_and_apply_gated(self):
        preview=self.run_tool('environment_import',{'action':'preview','kind':'proxies','text':'environment,proxy\nfb-008,http://127.0.0.1:8080'})['result']['job']
        source=self.prepare('environment_import',{'action':'source','job_id':preview['id']})
        self.assertTrue(source['requires_approval'])
        plan=self.prepare('environment_import',{'action':'apply','job_id':preview['id']})
        self.assertTrue(plan['requires_approval'])
        with self.assertRaises(ManagementError): self.phase(plan)
        self.assertTrue(self.phase(plan,approve=True)['ok'])
        self.assertEqual(self.wait_import(preview['id'])['status'],'complete')
    def test_import_preparation_does_not_invert_config_lock_order(self):
        held=threading.Event();release=threading.Event();finished=threading.Event()
        def other():
            with self.manager.imports.lock:
                held.set();release.wait(2)
                with self.manager.lock: pass
        first=threading.Thread(target=other);first.start();self.assertTrue(held.wait(2))
        def prepare():
            try: self.run_tool('environment_import',{'action':'history'})
            finally: finished.set()
        second=threading.Thread(target=prepare);second.start();release.set()
        self.assertTrue(finished.wait(3));first.join(3);second.join(3)
    def test_backup_restore_history_draft_and_transfer(self):
        backup=self.run_tool('environment_backup')['result']['backup']
        row=self.row();self.manager.save({'env_id':'fb-008','revision':row['revision'],'notes':'changed'})
        preview=self.run_tool('environment_restore',{'action':'preview','backup_id':backup['id']})['result']
        reply=self.run_tool('environment_restore',{'action':'apply','backup_id':backup['id'],'preview_token':preview['preview_token'],'env_ids':['fb-008']},approve=True)
        self.assertTrue(reply['ok'],reply);self.assertEqual(self.row()['notes'],'')
        self.assertTrue(self.run_tool('environment_history',{'env_id':'fb-008'})['ok'])
        draft=self.prepare('environment_draft',{'action':'save','draft':{'password':'draft-secret'}})
        self.assertTrue(draft['requires_approval']);self.assertTrue(self.phase(draft,approve=True)['ok'])
        read=self.run_tool('environment_draft',{'action':'read'},approve=True)
        self.assertEqual(read['result']['draft']['password'],'draft-secret')
        exported=self.run_tool('environment_account_transfer',{'action':'export','password':'transfer-password'},approve=True)
        self.assertTrue(exported['ok'],exported)
        self.assertTrue(self.run_tool('environment_account_transfer',{'action':'preview','password':'transfer-password','document':exported['result']['document']})['ok'])
    def test_schema_rejects_model_control_of_confirmation_or_session(self):
        for args in ({'display_name':'bad','session_id':'other'},{'display_name':'bad','confirm':True},{'display_name':'bad','approval_grant':'forged'}):
            with self.assertRaises(ManagementError): self.prepare('environment_save',args)
        with self.assertRaises(ManagementError): self.prepare('environment_list',{'limit':True})
    def test_http_gateway_auth_limits_and_documentation(self):
        from http.server import ThreadingHTTPServer
        from lan_bridge import make_handler
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.bridge,'test-key'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,body='',auth=True,length=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if auth: headers['Authorization']='Bearer test-key'
            if length is not None: headers['Content-Length']=str(length)
            c.request(method,path,body,headers);r=c.getresponse();data=json.loads(r.read());c.close();return r.status,data
        try:
            self.assertEqual(request('POST','/v1/session/manage','{}',False)[0],401)
            self.assertEqual(request('POST','/v1/session/manage','[]')[0],400)
            self.assertEqual(request('POST','/v1/session/manage','',length=4194305)[0],413)
            body=json.dumps({'phase':'prepare','tool':'environment_catalog','arguments':{},'session_id':self.session,'request_id':secrets.token_hex(32)})
            status,data=request('POST','/v1/session/manage',body);self.assertEqual(status,200);self.assertTrue(data['result']['ok'])
            status,doc=request('GET','/v1/lan/manage/openapi');self.assertEqual(status,200)
            self.assertEqual(doc['servers'][0]['url'],'http://127.0.0.1:'+str(server.server_port))
        finally: server.shutdown();server.server_close();thread.join()

if __name__=='__main__': unittest.main()
