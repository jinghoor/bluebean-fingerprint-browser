"""Real process reload checks in disposable homes; never touches an installed home."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bridge_runtime import source_revision
from _runtime_state import process_identity


def main():
    out=ROOT/'.work/bridge-reload'/uuid.uuid4().hex[:10]
    out.mkdir(parents=True)
    identities={}
    direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result={'passed':False}

    def environment(home):
        return dict(os.environ,BLUEBEAN_HOME=str(ROOT),BLUEBEAN_DATA=str(home),
            BLUEBEAN_PROFILE=str(home/'BrowserProfile'),BLUEBEAN_PROFILE_ROOT=str(home),
            BLUEBEAN_PROXY_CONFIG=str(home/'proxies.json'),DSH_HOME=str(home/'DSH'))

    def config(home):
        return json.loads((home/'.lan-bridge.json').read_text(encoding='utf-8'))

    def health(home):
        saved=config(home)
        request=urllib.request.Request(f"http://127.0.0.1:{saved['port']}/v1/health",
            headers={'Authorization':'Bearer '+saved['token']})
        with direct.open(request,timeout=2) as response:return json.load(response)

    def remember(pid):
        identity=process_identity(pid)
        assert identity.get('alive') and identity.get('started'),identity
        identities[pid]=identity

    def spawn_previous_generation(home,busy=False):
        home.mkdir(parents=True)
        script="import lan_bridge;lan_bridge.LOADED_RUNTIME_REVISION='fixture-previous-generation';"
        if busy:script+="lan_bridge.Bridge.execution_busy=lambda self:True;"
        script+="raise SystemExit(lan_bridge.main())"
        with (home/'old-service.log').open('ab') as log:
            child=subprocess.Popen([sys.executable,'-B','-c',script,'--port','0'],cwd=ROOT,
                env=environment(home),stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW)
        remember(child.pid)
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            assert child.poll() is None,'fixture service exited'
            try:
                if health(home).get('runtime_revision')=='fixture-previous-generation':return child
            except (OSError,ValueError):pass
            time.sleep(.05)
        raise AssertionError('fixture service did not become ready')

    def ensure(home):
        execution=subprocess.run([sys.executable,'-B','-c',
            'import json,_ensure_bridge;print(json.dumps(_ensure_bridge.ensure_bridge(False)))'],
            cwd=ROOT,env=environment(home),capture_output=True,text=True,timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert execution.returncode==0,execution.stderr[-1000:]
        reply=json.loads(execution.stdout.strip().splitlines()[-1])
        if reply.get('started') and reply.get('ok'):remember(reply['pid'])
        return reply

    try:
        home=out/'idle'
        old=spawn_previous_generation(home)
        before=config(home)
        with ThreadPoolExecutor(max_workers=3) as pool:replies=list(pool.map(ensure,[home]*3))
        assert all(r.get('ok') and r.get('runtime_current') for r in replies),replies
        assert len({r['pid'] for r in replies})==1,replies
        assert sum(bool(r.get('started')) for r in replies)==1,replies
        old.wait(timeout=5)
        after=config(home)
        assert after['pid']!=before['pid']
        assert after['token']==before['token'] and after['port']==before['port']
        assert health(home)['runtime_revision']==source_revision(ROOT)
        result.update(idle_old_runtime_replaced=True,endpoint_and_token_preserved=True,
            concurrent_replacement_single_instance=True,new_loaded_revision_verified=True)
        reused=ensure(home)
        assert reused['pid']==after['pid'] and not reused['started'],reused
        result['current_runtime_reused']=True

        busy_home=out/'busy'
        busy=spawn_previous_generation(busy_home,True)
        reply=ensure(busy_home)
        assert reply.get('reload_pending') and not reply.get('started'),reply
        assert busy.poll() is None and health(busy_home)['pid']==busy.pid
        result['busy_runtime_preserved']=True
        result['passed']=True
    finally:
        for pid,identity in identities.items():
            current=process_identity(pid)
            if current.get('alive') and current.get('started')==identity['started']:
                subprocess.run(['taskkill','/PID',str(pid),'/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        (out/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result),flush=True)
        print(str(out/'report.json'),flush=True)


if __name__=='__main__':main()
