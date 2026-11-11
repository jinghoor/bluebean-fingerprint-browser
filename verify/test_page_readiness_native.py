"""Exercise production waits/input on localhost, private desktop, disposable profile.

Run directly; never switches the user's desktop or launches an installed profile.
"""
from __future__ import annotations
import base64
import argparse
import ast
import ctypes as c
from ctypes import wintypes as w
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse, parse_qs
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _browser_launch import compatible_browser_argv
from browser_backend import BrowserBackend, BrowserCdp
from lan_bridge import Bridge

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/pV8AAAAASUVORK5CYII=')
DELAY = 3


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path)
        if path.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/case?mode=plain')
            self.end_headers()
            return
        if path.path == '/slow.png':
            time.sleep(DELAY)
            body, mime = PIXEL, 'image/png'
        else:
            mode = parse_qs(path.query).get('mode', ['plain'])[0]
            script = {
                'delayed': "const b=document.querySelector('#submit');b.remove();setTimeout(()=>{document.body.append(b);b.disabled=true;setTimeout(()=>b.disabled=false,250)},250);",
                'overlay': "const d=document.createElement('div');d.id='cover';d.style='position:fixed;inset:0;background:#aaa;z-index:9';document.body.append(d);setTimeout(()=>d.remove(),300);",
                'covered': "const d=document.createElement('div');d.style='position:fixed;inset:0;background:#aaa;z-index:9';document.body.append(d);",
                'moving': "const b=document.querySelector('#submit');b.style.position='relative';let n=0;const t=setInterval(()=>{b.style.left=(++n*5)+'px';if(n===8)clearInterval(t)},30);",
                'partial': "document.querySelector('#submit').style='position:fixed;left:-190px;top:80px;width:240px;height:40px';",
                'readonly': "document.querySelector('#field').readOnly=true;",
                'spa': "document.querySelector('#submit').addEventListener('click',()=>history.pushState({},'', '/spa-done'));",
                'large': "const f=document.createDocumentFragment();for(let n=0;n<1000;n++){const d=document.createElement('div');const b=document.createElement('button');b.textContent='Fixture row '+n;d.append(b);f.append(d)}document.body.append(f);",
                'whitespace': "const b=document.querySelector('#submit');b.removeAttribute('aria-label');b.textContent='   ';b.title='Whitespace target';",
            }.get(mode, '')
            body = ('''<!doctype html><meta charset=utf-8><title>Readiness fixture</title>
<style>body{font:18px sans-serif;padding:20px}button,input{padding:12px;margin:4px}</style>
<input id=field aria-label="Fixture input" oninput="window.inputTrusted=event.isTrusted">
<button id=submit aria-label="Fixture submit" onclick="window.clicks++;window.trusted=event.isTrusted;setTimeout(()=>document.querySelector('#status').textContent='BUSINESS_OK',150)">Submit</button>
<p id=status>Waiting</p><script>window.clicks=0;''' + script + '</script>' +
                ('<img src=/slow.png width=1 height=1>' if mode == 'slow' else '')).encode()
            mime = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            # Test-owned Chromium may close a keepalive during process cleanup.
            pass


class SI(c.Structure):
    _fields_ = [('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),
        ('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),('dwYSize',w.DWORD),
        ('dwXCountChars',w.DWORD),('dwYCountChars',w.DWORD),('dwFillAttribute',w.DWORD),
        ('dwFlags',w.DWORD),('wShowWindow',w.WORD),('cbReserved2',w.WORD),
        ('lpReserved2',c.c_void_p),('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]


class PI(c.Structure):
    _fields_ = [('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('chrome',nargs='?',default=str(ROOT/'chromium/out/Release-pgo/chrome.exe'))
    parser.add_argument('--baseline-dir',type=Path,help='Read-only source backup to compare against')
    args=parser.parse_args()
    chrome = Path(args.chrome).resolve()
    assert chrome.is_file(), chrome
    out = ROOT/'.work/page-readiness'/uuid.uuid4().hex[:10]
    out.mkdir(parents=True)
    user,kernel=c.WinDLL('user32',use_last_error=True),c.WinDLL('kernel32',use_last_error=True)
    user.CreateDesktopW.argtypes=[w.LPCWSTR,w.LPCWSTR,c.c_void_p,w.DWORD,w.DWORD,c.c_void_p]
    user.CreateDesktopW.restype=w.HANDLE
    user.CloseDesktop.argtypes=[w.HANDLE]
    user.OpenInputDesktop.argtypes=[w.DWORD,w.BOOL,w.DWORD]
    user.OpenInputDesktop.restype=w.HANDLE
    user.GetUserObjectInformationW.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)]
    kernel.CreateProcessW.argtypes=[w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(SI),c.POINTER(PI)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
    kernel.GetExitCodeProcess.argtypes=[w.HANDLE,c.POINTER(w.DWORD)]

    def input_desktop():
        handle=user.OpenInputDesktop(0,False,1)
        if not handle and c.get_last_error()==5 and os.environ.get('BLUEBEAN_TEST_ALLOW_UNOBSERVABLE_INPUT_DESKTOP')=='1':
            return None
        assert handle,c.WinError(c.get_last_error())
        try:
            name,size=c.create_unicode_buffer(256),w.DWORD()
            assert user.GetUserObjectInformationW(handle,2,name,c.sizeof(name),c.byref(size))
            return name.value
        finally:
            user.CloseDesktop(handle)

    before=input_desktop()
    desktop_name='BlueBeanReadiness_'+uuid.uuid4().hex[:10]
    desktop=user.CreateDesktopW(desktop_name,None,None,0,0x1ff,None)
    assert desktop,c.WinError(c.get_last_error())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    argv=compatible_browser_argv([str(chrome),f'--user-data-dir={out / "Profile"}',
        f'--remote-debugging-port={port}','--no-first-run','--no-default-browser-check',
        '--no-proxy-server','--disable-background-networking','--disable-component-update',
        '--disable-features=CalculateNativeWinOcclusion','--disable-background-timer-throttling',
        '--window-size=900,700','about:blank'])
    env=dict(os.environ,BLUEBEAN_HOME=str(ROOT),BLUEBEAN_DATA=str(out),LAN_PRODUCT_HOME=str(out),
        BLUEBEAN_PROFILE=str(out/'Profile'),BLUEBEAN_PROFILE_ROOT=str(out),
        BLUEBEAN_PROXY_CONFIG=str(out/'proxies.json'),DSH_HOME=str(out/'DSH'))
    block=c.create_unicode_buffer('\0'.join(f'{k}={v}' for k,v in sorted(env.items()))+'\0\0')
    si,pi=SI(),PI()
    si.cb,si.lpDesktop=c.sizeof(si),'WinSta0\\'+desktop_name
    report={'passed':False,'scope':'production backend + bridge; localhost; private desktop; disposable profile','cases':[]}
    launched=False
    try:
        assert kernel.CreateProcessW(str(chrome),c.create_unicode_buffer(subprocess.list2cmdline(argv)),
            None,None,False,0x400|0x200,block,str(chrome.parent),c.byref(si),c.byref(pi)),c.WinError(c.get_last_error())
        launched=True
        kernel.CloseHandle(pi.hThread)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline=time.monotonic()+25
        page=None
        while time.monotonic()<deadline:
            code=w.DWORD()
            assert kernel.GetExitCodeProcess(pi.hProcess,c.byref(code))
            assert code.value==259,f'private browser exited: {code.value}'
            try:
                with opener.open(f'http://127.0.0.1:{port}/json/list',timeout=.4) as response:
                    page=next(t for t in json.load(response) if t.get('type')=='page' and t.get('url')=='about:blank')
                break
            except (OSError,StopIteration):
                time.sleep(.05)
        assert page,'private browser page not ready'
        with BrowserCdp(page['webSocketDebuggerUrl']) as cdp:
            cdp.send('Page.enable')
            backend=BrowserBackend(port=port)
            backend._cdp=lambda _env:cdp  # Only this disposable page is reachable.
            backend.wait_for('fixture',url='about:blank',state='dom',stable_ms=100,timeout_ms=3000)
            bridge=Bridge.__new__(Bridge)
            bridge.backend,bridge.permissions,bridge.snapshots=backend,{'preset':'full'},{}

            def step(name,args):
                with patch('lan_bridge.audit'):
                    return bridge.run_step('fixture',{name:args})

            def navigate(mode):
                result=step('goto',{'url':base+'/case?mode='+mode})
                assert result.get('ok') is not False,result
                return result

            def target(selector='#submit'):
                result=step('observe',{})
                return next(row['id'] for row in result['elements'] if row['selector']==selector)

            def record(name,**data):
                report['cases'].append({'case':name,'passed':True,**data})
                print(json.dumps(report['cases'][-1]),flush=True)

            for index in range(2):
                started=time.monotonic()
                nav=navigate('slow')
                assert nav['readiness']['dom_ready'] and not nav['readiness']['load_complete'],nav
                assert step('act',{'id':target()}).get('ok') is not False
                done=step('wait_for',{'selector':'#status','text':'BUSINESS_OK','state':'visible','timeout_ms':2000})
                assert done['ok'],done
                state=cdp.evaluate('({clicks, trusted, ready:document.readyState})')
                early=round((time.monotonic()-started)*1000,2)
                assert state=={'clicks':1,'trusted':True,'ready':'interactive'},state
                complete=step('wait_for',{'state':'complete','timeout_ms':6000})
                assert complete['ok'],complete
                full=round((time.monotonic()-started)*1000,2)
                assert full-early>1000,(early,full)
                record(f'slow_resource_round_{index+1}',business_complete_ms=early,full_load_ms=full,clicks=1,trusted=True)

            for mode in ('delayed','overlay','moving','partial'):
                navigate(mode)
                ready=step('wait_for',{'selector':'#submit','state':'actionable','timeout_ms':3000})
                assert ready['ok'],ready
                clicked=step('act',{'id':target()})
                assert clicked.get('ok') is not False,clicked
                state=cdp.evaluate('({clicks,trusted})')
                assert state=={'clicks':1,'trusted':True},state
                record(mode,waited_ms=ready['waited_ms'],point=clicked['at'])

            navigate('plain')
            typed=step('type',{'id':target('#field'),'text':'ready'})
            assert typed.get('ok') is not False,typed
            assert cdp.evaluate("document.querySelector('#field').value==='ready' && inputTrusted") is True
            record('editable_input',trusted=True)

            old=backend.observations['fixture']
            cdp.send('Page.navigate',{'url':base+'/case?mode=plain&new=1'})
            backend.wait_for('fixture',url=base+'/case?mode=plain&new=1',state='dom',timeout_ms=3000)
            backend.observations['fixture']=old
            stale=step('act',{'id':old.items[1]['id']})
            assert stale.get('error_code')=='STALE_OBSERVATION' and stale.get('action_sent') is False,stale
            assert cdp.evaluate('clicks')==0
            record('stale_document',action_sent=False)

            navigate('covered')
            failed=step('act',{'id':target()})
            assert failed.get('error_code')=='PAGE_CONDITION_TIMEOUT' and failed.get('action_sent') is False,failed
            assert cdp.evaluate('clicks')==0
            missing=step('wait_for',{'selector':'#missing','timeout_ms':120})
            assert missing.get('error_code')=='PAGE_CONDITION_TIMEOUT',missing
            record('covered_and_missing',action_sent=False)

            navigate('readonly')
            failed=step('type',{'id':target('#field'),'text':'must-not-type'})
            assert failed.get('error_code')=='PAGE_CONDITION_TIMEOUT',failed
            assert cdp.evaluate("document.querySelector('#field').value")==''
            record('readonly_input',action_sent=False)

            nav=step('goto',{'url':base+'/redirect'})
            assert nav['url']==base+'/case?mode=plain',nav
            record('redirect',url=nav['url'])

            navigate('spa')
            assert step('act',{'id':target()}).get('ok') is not False
            success=step('wait_for',{'url':base+'/spa-done','selector':'#status','text':'BUSINESS_OK','timeout_ms':2000})
            assert success['ok'] and cdp.evaluate('clicks')==1,success
            record('spa_business_result',clicks=1)

            navigate('plain')
            for state in ('dom','complete'):
                missing=step('wait_for',{'selector':'#missing','state':state,'timeout_ms':120})
                assert missing.get('error_code')=='PAGE_CONDITION_TIMEOUT',missing
            assert step('wait_for',{'selector':'#missing','state':'hidden','timeout_ms':120})['ok']
            record('selector_is_required_for_dom_and_complete',action_sent=False)

            first=step('observe',{})
            old_id=next(row['id'] for row in first['elements'] if row['selector']=='#submit')
            second=step('observe',{})
            current_id=next(row['id'] for row in second['elements'] if row['selector']=='#submit')
            assert old_id!=current_id
            rejected=step('act',{'id':old_id})
            assert rejected.get('error_code')=='STALE_OBSERVATION' and not rejected['action_sent'],rejected
            assert cdp.evaluate('clicks')==0
            record('previous_observation_id_does_not_alias',action_sent=False)

            navigate('whitespace')
            assert step('act',{'id':target()}).get('ok') is not False
            assert cdp.evaluate('clicks')==1
            record('whitespace_label_preserves_target_identity')

            direct_backend=BrowserBackend(port=port)
            with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1','NO_PROXY':''}), \
                 patch('urllib.request.proxy_bypass',return_value=False):
                assert any(row['id']==page['id'] for row in direct_backend.targets())
            record('control_channel_ignores_system_proxy')

            if args.baseline_dir:
                def source_literal(file_name,name):
                    tree=ast.parse((args.baseline_dir/file_name).read_text(encoding='utf-8-sig'))
                    return next(ast.literal_eval(node.value) for node in ast.walk(tree)
                        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets))

                before_observe=source_literal('browser_backend.py','OBSERVE_JS')
                before_snapshot=source_literal('browser_backend.py','SNAPSHOT_JS')
                before_probe=source_literal('page_readiness.py','PROBE_JS')
                navigate('plain')
                cdp.evaluate("window.probeTextReads=0;Object.defineProperty(document.body,'innerText',{configurable:true,get(){window.probeTextReads++;return 'BODY_TEXT'}})")
                cdp.evaluate('('+before_probe+')('+json.dumps({'state':'dom'})+')')
                old_reads=cdp.evaluate('probeTextReads')
                cdp.evaluate('window.probeTextReads=0')
                assert step('wait_for',{'state':'dom','timeout_ms':1000})['ok']
                new_reads=cdp.evaluate('probeTextReads')
                assert new_reads==0 and old_reads>0,(old_reads,new_reads)
                cdp.evaluate('delete document.body.innerText')
                record('dom_probe_avoids_unneeded_page_text',before_text_reads=old_reads,after_text_reads=new_reads)

                for state in ('dom','complete'):
                    old_result=cdp.evaluate('('+before_probe+')('+json.dumps({'selector':'#missing','state':state})+')')
                    assert old_result['matched'],old_result
                record('baseline_false_positive_reproduced_and_fixed',states=['dom','complete'])

                navigate('large')
                expected=json.loads(cdp.evaluate(before_snapshot))
                # Warm both paths, then alternate their order to reduce order bias.
                cdp.evaluate(before_observe)
                step('observe',{})
                durations={'before':[],'after':[]}
                counts={'before':[],'after':[]}
                real_evaluate=cdp.evaluate
                calls=[0]

                def counted_evaluate(*a,**kw):
                    calls[0]+=1
                    return real_evaluate(*a,**kw)

                with patch.object(cdp,'evaluate',side_effect=counted_evaluate):
                    for index in range(24):
                        for version in (('before','after') if index%2==0 else ('after','before')):
                            calls[0]=0
                            started=time.perf_counter()
                            if version=='before':
                                cdp.evaluate(before_observe)
                                cdp.evaluate(before_snapshot)
                            else:
                                step('observe',{})
                            durations[version].append((time.perf_counter()-started)*1000)
                            counts[version].append(calls[0])
                assert set(counts['before'])=={2} and set(counts['after'])=={1},counts
                summary=backend.observations['fixture'].summary
                for key,value in expected.items():
                    assert summary[key]==value,(key,summary[key],value)
                record('observation_comparison_1000_controls',samples_per_version=24,
                    before_median_ms=round(statistics.median(durations['before']),3),
                    after_median_ms=round(statistics.median(durations['after']),3),
                    before_p95_ms=round(sorted(durations['before'])[22],3),
                    after_p95_ms=round(sorted(durations['after'])[22],3),
                    before_renderer_calls=2,after_renderer_calls=1,summary_equivalent=True)

            for index in range(20):
                navigate('plain')
                element=target()
                if index%4==0:
                    previous=element
                    element=target()
                    rejected=step('act',{'id':previous})
                    assert rejected.get('error_code')=='STALE_OBSERVATION',rejected
                clicked=step('act',{'id':element})
                assert clicked.get('ok') is not False,clicked
                done=step('wait_for',{'selector':'#status','text':'BUSINESS_OK','state':'visible','timeout_ms':2000})
                assert done['ok'] and cdp.evaluate('clicks')==1,done
            record('repeated_navigation_observation_submit',rounds=20,single_submit_confirmed=20,stale_attempts_prevented=5)
        after=input_desktop()
        report['input_desktop_observation_available']=before is not None and after is not None
        report['input_desktop_unchanged']=after==before if report['input_desktop_observation_available'] else None
        if report['input_desktop_observation_available']:
            assert report['input_desktop_unchanged']
        report['passed']=True
    finally:
        if launched:
            subprocess.run(['taskkill','/PID',str(pi.dwProcessId),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            kernel.WaitForSingleObject(pi.hProcess,5000)
            kernel.CloseHandle(pi.hProcess)
        server.shutdown()
        server.server_close()
        user.CloseDesktop(desktop)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(str(out/'report.json'),flush=True)


if __name__=='__main__':
    main()
