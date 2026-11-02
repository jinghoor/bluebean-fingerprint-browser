"""Installed DSH + real model + isolated bridge; never touches the product profile/UI."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_client import DshRuntime
from _nowindow import no_window

ROOT=Path(__file__).resolve().parents[1]
CASE=ROOT/'.work/environment-agent-api'
CASE.mkdir(parents=True,exist_ok=True)

def main():
    with tempfile.TemporaryDirectory(prefix='bluebean-dsh-agent-test-') as folder:
        home=Path(folder);port_file=home/'bridge.json'
        server=subprocess.Popen([sys.executable,str(ROOT/'verify/environment_agent_server.py'),str(port_file)],
            cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=no_window())
        runtime=None
        old={k:os.environ.get(k) for k in ('LAN_BRIDGE_URL','LAN_BRIDGE_TOKEN')}
        try:
            end=time.monotonic()+10
            while not port_file.exists() and time.monotonic()<end: time.sleep(.05)
            cfg=json.loads(port_file.read_text());port=cfg['port']
            os.environ['LAN_BRIDGE_URL']='http://127.0.0.1:'+str(port)
            os.environ['LAN_BRIDGE_TOKEN']='isolated-agent-tests'
            profile=home/'profiles/sdk';profile.mkdir(parents=True)
            (profile/'cordis.patch.yml').write_text("- insert:\n    - id: lan-browser\n      name: 'E:/FingerprintBrowser/lan_plugin/index.js'\n",encoding='utf-8')
            runtime=DshRuntime(home,log=False)
            runtime.initialize(str(home),'deepseek-official','deepseek-flash',max_tokens=2048)
            sid='environment-agent-integration'
            runtime.prompt(sid,'这是临时profile接口验收。只能使用environment_*工具，禁止shell、浏览器、脚本。请用environment_catalog查看能力，再新建一个名为 DSH接口验收 的环境，不要打开环境。读取它的详情并添加Facebook账号 integration@example.test（不填密码）。然后用environment_delete尝试删除刚建的环境，验证原生风险确认；若确认通道不可用或拒绝就停止删除，不绕过、不重试。最后简述实际结果。')
            events=[];end=time.monotonic()+120;done=False
            while time.monotonic()<end and not done:
                for event in runtime.drain_events(.5):
                    events.append(event)
                    p=event.get('params',{})
                    if event.get('method')=='session.status' and p.get('status') in ('idle','failed'): done=True
            calls=[];outcomes=[];approvals=[]
            for event in events:
                e=event.get('params',{}).get('event',{})
                if e.get('type')=='tool/call': calls.append(e.get('data',{}).get('name'))
                if e.get('type')=='approval/decided': approvals.append(e.get('data',{}).get('outcome'))
                if e.get('type')=='tool/result': outcomes.append(e.get('data',{}).get('name'))
            req=urllib.request.Request(os.environ['LAN_BRIDGE_URL']+'/v1/lan/manage/list',data=b'{}',headers={'Authorization':'Bearer isolated-agent-tests','Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=5) as response: state=json.load(response)
            created=[r for r in state['envs'] if r['display_name']=='DSH接口验收']
            report={'ok':done and len(created)==1 and any(a['login']=='integration@example.test' for r in created for a in r.get('accounts',[])) and 'environment_delete' in calls and bool(approvals)
                and all(o in ('rejected','unavailable','cancelled') for o in approvals) and state['active']=='fb-020' and all((n or '').startswith('environment_') for n in calls),
                'completed':done,'tool_calls':calls,'approval_outcomes':approvals,'created_environments':len(created),
                'account_count':sum(len(r.get('accounts',[])) for r in created),'active_preserved':state['active']=='fb-020',
                'profile':'temporary','event_types':sorted({e.get('params',{}).get('event',{}).get('type','') for e in events})}
            # Save only controlled metadata, never raw prompts, credentials or host logs.
            (CASE/'runtime-integration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=True))
            if not report['ok']: raise AssertionError('Installed DSH integration incomplete; inspect controlled report')
        finally:
            if runtime: runtime.close()
            server.terminate();server.wait(timeout=5)
            for key,value in old.items():
                if value is None: os.environ.pop(key,None)
                else: os.environ[key]=value

if __name__=='__main__': main()
