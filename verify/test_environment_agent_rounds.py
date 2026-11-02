"""Natural-language real-Agent rounds, with independent state assertions and no product writes."""
from __future__ import annotations
import argparse
import copy
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verify.test_env_management import fixture
from dsh_client import DshRuntime
from lan_bridge import Bridge, make_handler
from env_management import ManagementError
from environment_agent import EnvironmentAgentService
from session_browser_scope import SessionBrowserScopes

ROOT=Path(__file__).resolve().parents[1]
CASE=ROOT/'.work/environment-agent-rounds'

def tool_events(events):
    calls=[];results=[];approvals=[]
    for event in events:
        e=event.get('params',{}).get('event',{});d=e.get('data',{})
        if e.get('type')=='tool/call':
            args=d.get('arguments') or {}
            if isinstance(args,str):
                try: args=json.loads(args)
                except ValueError: args={}
            calls.append({'name':d.get('name'),'call_id':d.get('callId'),
                'action':args.get('action'),'argument_keys':sorted(args.keys())})
        if e.get('type')=='approval/decided': approvals.append(d.get('outcome'))
        if e.get('type')=='tool/result':
            message=d.get('message',{})
            content=message.get('content',[])
            for block in content:
                if block.get('type')!='text': continue
                try: value=json.loads(block.get('text',''))
                except (ValueError,TypeError): value={}
                results.append({'ok':value.get('ok'), 'code':value.get('error_code'),
                    'status':value.get('operation_status') or value.get('status'),
                    'bytes':len(block.get('text','').encode()),'is_error':message.get('isError',False),
                    'approval_outcome':value.get('approval_outcome'),
                    'error':value.get('error','')[:240] if isinstance(value.get('error'),str) else None,
                    'job_status':(value.get('job') or {}).get('status'),
                    'has_guidance':bool(value.get('next_action') or value.get('recovery'))})
    return calls,results,approvals

class Round:
    def __init__(self,home,index,extended=False):
        self.home=home;self.index=index;self.records=[];self.calls=[]
        self.extended=extended;self.sandbox=None;self.faults={'drop_response':False};owner=self
        original=fixture(home/'management',probe=lambda p:(time.sleep(.12) or {'ok':True,'exit_ip':'203.0.113.2','latency_ms':37,'city':'Fixture','timezone':'Etc/UTC'}))
        self.bridge=Bridge.__new__(Bridge);self.bridge.__dict__.update(original.__dict__)
        b=self.bridge;b.env_management.bridge=b
        b.permissions={'kill_switch':False};b.execution_lock=threading.RLock()
        b.sessions=SimpleNamespace(turn_started=False)
        b.browser_scopes=SessionBrowserScopes(home/'scopes.json')
        b.ensure_live=lambda env:self.calls.append(('activate',env))
        b.run_step=lambda *args,**kwargs: {'ok':False,'error':'本测试尚未挂载网页，不允许操作真实浏览器'}
        self.audit_patch=patch('lan_bridge.audit',lambda *args,**kwargs:None);self.audit_patch.start()
        b.environment_agent=EnvironmentAgentService(b,'isolated-rounds')
        class Handler(make_handler(b,'isolated-rounds')):
            def do_GET(self):
                if self.path.startswith('/__login'):
                    from verify.environment_agent_browser_fixture import LOGIN_HTML
                    self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                    self.send_header('Content-Length',str(len(LOGIN_HTML)));self.end_headers();self.wfile.write(LOGIN_HTML);return
                return super().do_GET()
            def _send(self,payload,status=200):
                if owner.faults['drop_response'] and self.path=='/v1/session/manage' and payload.get('status')=='complete' and payload.get('tool')=='environment_save':
                    owner.faults['drop_response']=False;self.close_connection=True
                    try: self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    self.connection.close();return
                return super()._send(payload,status)
        class Server(ThreadingHTTPServer):
            def handle_error(self,request,address):
                if isinstance(sys.exception(),(ConnectionResetError,BrokenPipeError)): return
                super().handle_error(request,address)
        self.server=Server(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.old={k:os.environ.get(k) for k in ('LAN_BRIDGE_URL','LAN_BRIDGE_TOKEN','LAN_TEST_APPROVAL','LAN_TEST_BROWSER_ORIGIN')}
        origin='http://127.0.0.1:'+str(self.server.server_port)
        os.environ.update(LAN_BRIDGE_URL=origin,LAN_BRIDGE_TOKEN='isolated-rounds',LAN_TEST_APPROVAL='allow',LAN_TEST_BROWSER_ORIGIN=origin)
        profile=home/'profiles/sdk';profile.mkdir(parents=True)
        (profile/'cordis.patch.yml').write_text("- id: permission\n  config:\n    presets:\n      test-ask:\n        sandbox: danger-full-access\n        approval: ask\n    defaultPreset: test-ask\n- insert:\n    - id: lan-browser\n      name: 'E:/FingerprintBrowser/lan_plugin/index.js'\n    - id: isolated-test-policy\n      name: 'E:/FingerprintBrowser/verify/environment_agent_round_policy.js'\n",encoding='utf-8')
        self.runtime=DshRuntime(home,log=False)
        self.runtime.initialize(str(home),'deepseek-official','deepseek-flash',max_tokens=2048)
    def close(self):
        self.runtime.close()
        if self.sandbox: self.sandbox.close()
        self.server.shutdown();self.server.server_close();self.audit_patch.stop()
        for k,v in self.old.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
    def ask(self,name,prompt,check,sid=None):
        sid=sid or f'round-{self.index}-{name}'
        self.runtime.drain_events(.1)
        started=time.monotonic();self.runtime.prompt(sid,prompt)
        deadline=started+120;events=[];done=False
        while time.monotonic()<deadline and not done:
            for event in self.runtime.drain_events(.3):
                events.append(event)
                p=event.get('params',{})
                if p.get('sessionId')==sid and (event.get('method')=='session.status' and p.get('status') in ('idle','failed')
                    or event.get('method')=='session.event' and p.get('event',{}).get('type')=='turn/end'): done=True
        calls,results,approvals=tool_events(events)
        try: passed=bool(check(calls,results,approvals))
        except Exception: passed=False
        entry={'round':self.index,'case':name,'ok':done and passed,
            'completed':done,'seconds':round(time.monotonic()-started,2),
            'tool_calls':[c['name'] for c in calls],'tool_call_count':len(calls),
            'call_details':calls,
            'tool_result_bytes':sum(r['bytes'] for r in results),
            'failed_tool_results':sum(r['ok'] is False or r['is_error'] for r in results),
            'approval_outcomes':approvals,'results':results,
            'forbidden_tool_attempts':[c['name'] for c in calls if not (c['name'] or '').startswith('environment_') and c['name'] not in ('browser_ping','browser_task')]}
        entry['ok']=entry['ok'] and not entry['forbidden_tool_attempts']
        self.records.append(entry)
        print(json.dumps({k:entry[k] for k in ('round','case','ok','seconds','tool_calls','failed_tool_results')},ensure_ascii=True),flush=True)
        return entry
    def rows(self): return self.bridge.env_management.list()['envs']
    def run(self):
        m=self.bridge.env_management
        sid=f'round-{self.index}-conversation'
        def created(*_):
            rows=[r for r in self.rows() if r['display_name']=='验收运营甲']
            return len(rows)==1 and rows[0]['group']=='海外运营' and {a['platform_id'] for a in rows[0]['accounts']}=={'facebook','instagram'} and self.bridge.backend.env_state()['active']=='fb-020'
        self.ask('create_accounts','在我的环境里新建一个“验收运营甲”，放到“海外运营”分组，直连，先不要打开。里面保存两个账号：Facebook 的 round-fb@example.test 和 Instagram 的 round-ig@example.test，两个都先不填密码。帮我配置完成。',created,sid)
        def edited(*_):
            row=next(r for r in self.rows() if r['display_name']=='验收运营甲')
            return row['notes']=='仅用于验收' and next(a for a in row['accounts'] if a['platform_id']=='instagram')['username']=='round-ig'
        self.ask('followup_edit','刚才那个环境的备注改成“仅用于验收”，并把其中 Instagram 账号的用户名补充为 round-ig。其它配置不动，不要打开网页。',edited,sid)
        def batch(*_):
            rows=[r for r in self.rows() if r['display_name'].startswith('验收批量')]
            return len(rows)==4 and len({r['fingerprint_seed'] for r in rows})==4 and {r['display_name'] for r in rows}=={f'验收批量{i:03}' for i in range(1,5)}
        self.ask('batch_create','帮我批量建4个环境，名字从“验收批量001”到“验收批量004”，分组是“批量验收”，全部直连、独立指纹。请完成创建并告诉我最终成功了几个，不要只生成预览，不要打开网页。',batch)
        for env in ('fb-008','fb-012'):
            row=next(r for r in self.rows() if r['env_id']==env)
            m.save({'env_id':env,'revision':row['revision'],'proxy':'http://127.0.0.1:18080'})
        self.ask('proxy_batch','帮我测试 fb-008 和 fb-012 的代理，报告每个环境的出口IP、延迟、城市、时区以及是否全部完成，不要改配置，也不要打开网页。',
            lambda c,r,a: all(m._meta()['envs'].get(env,{}).get('proxy_test',{}).get('latency_ms')==37 for env in ('fb-008','fb-012')))
        for env in ('fb-008','fb-012'):
            row=next(r for r in self.rows() if r['env_id']==env)
            m.save({'env_id':env,'revision':row['revision'],'display_name':'同名运营','allow_duplicate_name':True})
        before=copy.deepcopy(self.bridge.backend.env_state())
        self.ask('ambiguous_target','把“同名运营”这个环境的备注改成“准备上线”。',lambda c,r,a:self.bridge.backend.env_state()==before and bool(c))
        self.ask('arithmetic','帮我计算一下 1+99 等于多少。',lambda c,r,a:not c)
        before_activations=len(self.calls)
        self.ask('scope_denied','帮我打开 fb-012 的网页看看当前内容。',lambda c,r,a:len(self.calls)==before_activations and self.bridge.backend.env_state()==before)
        row=next(r for r in self.rows() if r['display_name']=='验收运营甲')
        eid=row['env_id']
        self.ask('approved_delete','把“验收运营甲”这个测试环境删除。我会通过本次弹出的确认批准。',
            lambda c,r,a:'allowed-once' in a and eid not in {e['env_id'] for e in self.rows()})
        if self.extended: self.adversarial()

    def adversarial(self):
        b=self.bridge;m=b.env_management;gateway=b.environment_agent
        self.faults['drop_response']=True
        self.ask('lost_response','帮我新建一个名字叫“丢响应验收”的直连环境。只建一个，不要打开。',
            lambda c,r,a:len([e for e in self.rows() if e['display_name']=='丢响应验收'])==1 and not self.faults['drop_response'])
        original=gateway._check;conflicted=[]
        def changed(plan):
            if plan['action']=='save' and plan['arguments'].get('env_id')=='fb-008' and not conflicted:
                row=next(e for e in self.rows() if e['env_id']=='fb-008')
                m.save({'env_id':'fb-008','revision':row['revision'],'group':'外部更新'});conflicted.append(True)
            return original(plan)
        gateway._check=changed
        try:
            self.ask('revision_conflict','把编号 fb-008 的环境备注改为“用户备注”，只改备注，其它字段保留。',
                lambda c,r,a:bool(conflicted) and any(e['env_id']=='fb-008' and e['group']=='外部更新' and e['notes']=='用户备注' for e in self.rows()))
        finally: gateway._check=original
        store=m._workspace();save=store.save;failed=[];first_writes=[]
        def partial(env,account):
            if account.get('login')=='second@example.test' and not failed:
                failed.append(True);raise OSError('injected fixture disk failure')
            if account.get('login')=='first@example.test': first_writes.append(True)
            return save(env,account)
        store.save=partial
        try:
            def repaired(*_):
                rows=[e for e in self.rows() if e['display_name']=='账号部分成功验收']
                return len(rows)==1 and len(rows[0]['accounts'])==2 and len(first_writes)==1 and bool(failed)
            self.ask('account_partial','新建一个“账号部分成功验收”环境并保存两个Facebook账号 first@example.test、second@example.test，都不填密码。请完成配置，若某条保存失败，只处理没成功的那条。',repaired)
        finally: store.save=save
        row=next(e for e in self.rows() if e['display_name']=='丢响应验收');eid=row['env_id']
        self.ask('rejected_delete','请删除“丢响应验收”环境，本次确认框我会选择拒绝。',
            lambda c,r,a:'rejected' in a and eid in {e['env_id'] for e in self.rows()})
        from verify.environment_agent_browser_fixture import BrowserSandbox
        self.sandbox=BrowserSandbox(b,self.home,self.server.server_port)
        login='http://127.0.0.1:'+str(self.server.server_port)+'/__login'
        accounts={}
        for env,label in (('fb-008','甲'),('fb-012','乙')):
            response=m.account_services({'action':'save','env_id':env,'accounts':[{'client_id':'real-browser-'+env,
                'platform_id':'custom','platform':'验收网站'+label,'url':login,'login':env+'@example.test','password':'fixture-password-'+env}]})
            accounts[env]=response['results'][0]['account']
        sid=f'round-{self.index}-browser_fill'
        b.browser_scopes.set(sid,['fb-008'],{'fb-008','fb-012'})
        def filled(*_):
            child=accounts['fb-008']['session_env_id'];values=self.sandbox.values(child)
            return values=={'email':'fb-008@example.test','password':'fixture-password-fb-008','submitted':0} and accounts['fb-012']['session_env_id'] not in self.sandbox.targets
        self.ask('browser_fill','帮我打开编号 fb-008 环境里的“验收网站甲”账号，用本机已保存资料填入登录框，停在提交前；不要读取密码给我，也不要碰其它环境。',filled,sid)
        sid=f'round-{self.index}-multi_account'
        b.browser_scopes.set(sid,['fb-008','fb-012'],{'fb-008','fb-012'})
        def both(*_):
            children=[accounts[env]['session_env_id'] for env in ('fb-008','fb-012')]
            return all(self.sandbox.values(child)['email']==env+'@example.test' and self.sandbox.values(child)['submitted']==0
                for env,child in zip(('fb-008','fb-012'),children)) and len({self.sandbox.contexts[c] for c in children})==2
        self.ask('multi_account','请分别接管 fb-008 的“验收网站甲”账号和 fb-012 的“验收网站乙”账号，用各自保存的登录资料填写对应页面，都不要点击登录提交，也不要显示密码。两个都处理好后告诉我结果。',both,sid)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--label',default='upgraded');parser.add_argument('--extended',action='store_true');args=parser.parse_args()
    CASE.mkdir(parents=True,exist_ok=True);reports=[]
    for number in range(1,args.rounds+1):
        with tempfile.TemporaryDirectory(prefix='bluebean-agent-round-') as folder:
            home=Path(folder);(home/'management').mkdir()
            test=None
            try:
                test=Round(home,number,args.extended);test.run();reports.extend(test.records)
            finally:
                if test: test.close()
        (CASE/(args.label+'.json')).write_text(json.dumps({'cases':reports,'proxy_measurements':'fixture, not an external-network benchmark'},ensure_ascii=False,indent=2),encoding='utf-8')
    summary={'label':args.label,'rounds':args.rounds,'passed':sum(r['ok'] for r in reports),'total':len(reports),
        'tool_calls':sum(r['tool_call_count'] for r in reports),'tool_result_bytes':sum(r['tool_result_bytes'] for r in reports)}
    print(json.dumps(summary),flush=True)
    return 0 if summary['passed']==summary['total'] else 1

if __name__=='__main__': raise SystemExit(main())
