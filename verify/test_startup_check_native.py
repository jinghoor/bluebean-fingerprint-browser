"""Exercise the real Chromium gate on a private desktop, using disposable data.

External website responses are deterministic fixtures; browser navigation, native
commands, persisted settings, UI actions and screenshots use the actual product.
"""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import env_cli
from env_startup_check import make_server, SITES, EXIT_SOURCES, EXIT_FALLBACKS
from environment_accounts import AccountStore
from env_management import EnvironmentManagement
from local_account_vault import LocalAccountVault
from _env_driver import Cdp

OUT = ROOT / '.work' / 'environment-startup-check' / ('native-' + time.strftime('%Y%m%d-%H%M%S'))
PROFILE = OUT / 'Profile'
(PROFILE / 'Default').mkdir(parents=True)
report = {'checks': {}, 'network_responses': 'deterministic fixtures; native browser and page actions are real'}
counts = {}


def command(payload):
    return env_cli.send(PROFILE / 'Default', payload, 8)


def probe(proxy, url):
    counts[proxy] = counts.get(proxy, 0) + 1
    time.sleep(.05)
    if proxy.endswith(':11006'):
        return {'ok': False, 'status': 0, 'error': 'fixture unavailable', 'latency_ms': 10}
    return {'ok': True, 'status': 200, 'latency_ms': 21, 'body': json.dumps({
        'ip': '203.0.113.1', 'country': 'US', 'region': 'California', 'city': 'Test City',
        'timezone': 'America/Los_Angeles'})}


hits = []
class Destination(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        hits.append(self.path)
        body = b'<html><title>Gate destination</title><body style="background:#e6f3ff">Environment homepage reached</body></html>'
        self.send_response(200);self.send_header('Content-Length', str(len(body)));self.end_headers();self.wfile.write(body)


destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
threading.Thread(target=destination.serve_forever, daemon=True).start()
home = f'http://127.0.0.1:{destination.server_port}/home'
vault = LocalAccountVault(PROFILE / 'Default' / 'env-account-vault.dpapi.json')
store = AccountStore(vault)
native_accounts = [store.save('match', {'platform_id':'custom', 'platform':'Native account '+str(i),
    'login':'native-'+str(i)+'@example.test', 'url':home.rsplit('/',1)[0]+'/account-'+str(i),
    'password':'native-fixture-password-'+str(i), 'otp_secret':'JBSWY3DPEHPK3PXP'}) for i in (1,2)]
backend = SimpleNamespace(env_state=lambda:json.loads((PROFILE/'Default/envs.json').read_text(encoding='utf-8')))
def ensure_live(env):
    reply=command({'command':'activate','env_id':env})
    if not reply.get('ok'): raise ValueError('native account activation failed')
bridge=SimpleNamespace(backend=backend,account_vault=vault,ensure_live=ensure_live,execution_lock=threading.RLock())
manager=EnvironmentManagement(bridge,command=command,background_live=False)
service = make_server(PROFILE, command=command, probe=probe,account_store=store,account_command=manager.account_services)
threading.Thread(target=service.serve_forever, daemon=True).start()
envs = []
for i, name in enumerate(['match', 'mismatch', 'dynamic', 'direct', 'unrecorded', 'failure', 'system'], 1):
    envs.append({'env_id': name, 'display_name': 'Startup ' + name,
        'proxy': name if name in ('direct', 'system') else f'http://127.0.0.1:{11000+i}',
        'expected_exit_ip': '' if name == 'unrecorded' else '203.0.113.2' if name in ('mismatch', 'dynamic') else '203.0.113.1',
        'ip_mode': 'dynamic' if name == 'dynamic' else 'static', 'start_url': home, 'homepage': home})
(PROFILE / 'Default' / 'envs.json').write_text(json.dumps({'active': 'match', 'envs': envs}), encoding='utf-8')

user = c.WinDLL('user32', use_last_error=True)
kernel = c.WinDLL('kernel32', use_last_error=True)
class SI(c.Structure):
    _fields_ = [('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),
        ('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),('dwYSize',w.DWORD),('dwXCountChars',w.DWORD),
        ('dwYCountChars',w.DWORD),('dwFillAttribute',w.DWORD),('dwFlags',w.DWORD),('wShowWindow',w.WORD),
        ('cbReserved2',w.WORD),('lpReserved2',c.c_void_p),('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]
class PI(c.Structure):
    _fields_ = [('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]
user.CreateDesktopW.argtypes=[w.LPCWSTR,w.LPCWSTR,c.c_void_p,w.DWORD,w.DWORD,c.c_void_p];user.CreateDesktopW.restype=w.HANDLE
user.CloseDesktop.argtypes=[w.HANDLE]
kernel.CreateProcessW.argtypes=[w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(SI),c.POINTER(PI)]
kernel.CloseHandle.argtypes=[w.HANDLE]
kernel.TerminateProcess.argtypes=[w.HANDLE,w.UINT]
kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
desktop_name = 'BlueBeanStartupCheck_' + uuid.uuid4().hex[:10]
desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x1ff, None)
assert desktop, c.WinError(c.get_last_error())
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));cdp_port=sock.getsockname()[1]
chrome = Path(os.environ.get('BLUEBEAN_TEST_CHROME', r'E:\chromium\src\out\Release-pgo\chrome.exe'))
argv = [str(chrome), f'--user-data-dir={PROFILE}', f'--remote-debugging-port={cdp_port}',
        f'--lan-env-check-port={service.server_port}', '--no-first-run', '--no-default-browser-check',
        '--window-size=1400,960', '--disable-background-networking', '--disable-component-update',
        '--disable-features=CalculateNativeWinOcclusion', '--enable-logging', f'--log-file={OUT / "chrome.log"}', 'about:blank']
environment = dict(os.environ, LAN_PRODUCT_HOME=str(OUT), BLUEBEAN_DATA=str(OUT))
block = c.create_unicode_buffer('\0'.join(f'{k}={v}' for k,v in sorted(environment.items()))+'\0\0')
si=SI();si.cb=c.sizeof(si);si.lpDesktop='WinSta0\\'+desktop_name
pi=PI()
assert kernel.CreateProcessW(str(chrome),c.create_unicode_buffer(subprocess.list2cmdline(argv)),None,None,False,
    0x400|0x200,block,str(chrome.parent),c.byref(si),c.byref(pi)), c.WinError(c.get_last_error())
kernel.CloseHandle(pi.hThread)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait(fn, timeout=20):
    deadline=time.monotonic()+timeout;last=None
    while time.monotonic()<deadline:
        try:
            last=fn()
            if last:return last
        except (OSError,ValueError,KeyError,TimeoutError):pass
        time.sleep(.1)
    raise TimeoutError(str(last))


def targets():
    with opener.open(f'http://127.0.0.1:{cdp_port}/json/list', timeout=2) as response:
        return [t for t in json.load(response) if t['type']=='page']


def target(env):
    return next((t for t in targets() if parse_qs(urlsplit(t['url']).query).get('env')==[env]), None)


def evaluate(t, expression):
    with Cdp(t['webSocketDebuggerUrl']) as connection:
        raw=connection.send('Runtime.evaluate', {'expression':expression,'returnByValue':True,'awaitPromise':True})
        r=raw.get('result',{})
        if r.get('exceptionDetails'):raise RuntimeError(str(r))
        return r.get('result',{}).get('value')


def page_state(t):
    return evaluate(t,"({status:document.getElementById('status')?.textContent,buttons:[...document.querySelectorAll('#actions button')].map(x=>({text:x.textContent,disabled:x.disabled})),text:document.body?.innerText||'',ready:document.readyState})")


def activate(env):
    reply=command({'command':'activate','env_id':env});assert reply.get('ok'),reply
    t=wait(lambda:target(env))
    wait(lambda:(s if (s:=page_state(t)).get('status') and '正在通过' not in s['status'] else None))
    return t


def click(t, text):
    assert evaluate(t, f"(()=>{{const b=[...document.querySelectorAll('#actions button')].find(x=>x.textContent.includes({json.dumps(text)}));if(!b||b.disabled)return false;b.click();return true}})()"), text


def screenshot(t,name,width=1280):
    import base64
    with Cdp(t['webSocketDebuggerUrl']) as connection:
        connection.send('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':False})
        time.sleep(.2)
        data=connection.send('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
        (OUT/name).write_bytes(base64.b64decode(data['result']['data']))


try:
    wait(targets)
    initial=wait(lambda:target('match'))
    wait(lambda:'检查通过' in (page_state(initial) or {}).get('status',''))
    report['checks']['first_environment_opens_check_page_and_passes']=True
    # Read from the real account vault, through the real portal and native gate.
    wait(lambda:evaluate(initial,"document.querySelectorAll('.account').length") == 2)
    wait(lambda:evaluate(initial,"[...document.querySelectorAll('.otp-code')].every(e=>/^\\d{6}$/.test(e.textContent))"))
    report['checks']['saved_accounts_and_auto_totp_visible']=True
    assert evaluate(initial,"[...document.querySelectorAll('.credential-value')].every(e=>e.textContent==='••••••••')")
    evaluate(initial,"document.querySelector('.credential-reveal').click()")
    wait(lambda:evaluate(initial,"document.querySelector('.credential-value').textContent")=='native-fixture-password-1')
    evaluate(initial,"document.querySelector('.credential-reveal').click()")
    assert evaluate(initial,"document.querySelector('.credential-value').textContent")=='••••••••'
    report['checks']['native_password_reveal_and_hide']=True
    screenshot(initial,'accounts-desktop.png')
    before_account_probes=counts['http://127.0.0.1:11001']
    for index,account in enumerate(native_accounts,1):
        if index>1: initial=activate('match')
        selector=json.dumps('[data-account="'+account['account_id']+'"] [data-open-account]')
        assert evaluate(initial,f"(()=>{{const b=document.querySelector({selector});if(!b||b.disabled)return false;b.click();return true}})()")
        wait(lambda:any(t['url']==account['url'] for t in targets()))
        assert next(e for e in command({'command':'list'})['envs'] if e['env_id']==account['session_env_id']).get('tabs')
    assert counts['http://127.0.0.1:11001']==before_account_probes
    assert native_accounts[0]['session_env_id']!=native_accounts[1]['session_env_id']
    report['checks']['two_accounts_open_real_dedicated_sessions_without_duplicate_probes']=True
    initial=activate('match')
    t=activate('mismatch')
    wait(lambda:'风险' in (page_state(t) or {}).get('status',''))
    report['checks']['mismatch_ui_three_choices']=len(page_state(t)['buttons'])==3
    screenshot(t,'mismatch-desktop.png')
    screenshot(t,'mismatch-narrow.png',480)
    assistant_url = home.rsplit('/', 1)[0] + '/assistant'
    assert command({'command':'overlay','url':assistant_url}).get('ok')
    wait(lambda:'/assistant' in hits)
    report['checks']['assistant_loads_while_environment_locked']=True
    assert command({'command':'overlay','on':False}).get('ok')
    before=len(hits)
    with Cdp(t['webSocketDebuggerUrl']) as connection:
        connection.send('Target.createTarget', {'url': assistant_url})
        connection.send('Target.createTarget', {'url': home})
    time.sleep(.6)
    assert len(hits)==before,hits
    report['checks']['browser_created_tab_cannot_send_request']=True
    report['checks']['assistant_url_in_regular_tab_has_no_exemption']=True
    reply=command({'command':'newtab','env_id':'mismatch','url':home})
    assert not reply.get('ok'),reply
    report['checks']['native_new_tab_blocked']=True
    with Cdp(t['webSocketDebuggerUrl']) as connection:
        connection.send('Page.navigate',{'url':home})
    time.sleep(.6)
    assert len(hits)==before,hits
    assert target('mismatch'),targets()
    report['checks']['main_frame_navigation_blocked_before_request']=True
    wait(lambda: any('接受本次风险' in b['text'] and not b['disabled'] for b in (page_state(t) or {}).get('buttons', [])))
    click(t,'接受本次风险')
    wait(lambda:'已接受本次' in (page_state(t) or {}).get('status',''))
    assert target('mismatch'), 'risk confirmation should keep the account portal visible'
    saved=json.loads((PROFILE/'Default/envs.json').read_text(encoding='utf-8'))
    assert next(e for e in saved['envs'] if e['env_id']=='mismatch')['expected_exit_ip']=='203.0.113.2'
    report['checks']['continue_once_preserves_expected_ip']=True
    assert command({'command':'newtab','env_id':'mismatch','url':home}).get('ok')
    report['checks']['native_new_tab_allowed_after_confirmation']=True
    count=counts['http://127.0.0.1:11002']
    command({'command':'activate','env_id':'mismatch'});time.sleep(.3)
    assert counts['http://127.0.0.1:11002']==count
    report['checks']['switching_does_not_recheck']=True

    old_key=parse_qs(urlsplit(t['url']).query)['key'][0]
    command({'command':'startup-check','env_id':'mismatch','key':old_key,'action':'close'})
    wait(lambda:not next(e for e in command({'command':'list'})['envs'] if e['env_id']=='mismatch').get('tabs'))
    wait(lambda:target('mismatch') is None)
    t=activate('mismatch')
    wait(lambda:any('改为动态' in b['text'] and not b['disabled'] for b in (page_state(t) or {}).get('buttons', [])))
    assert parse_qs(urlsplit(t['url']).query)['key'][0] != old_key
    report['checks']['closed_environment_gets_fresh_capability']=True
    click(t,'改为动态')
    wait(lambda:next(e for e in command({'command':'list'})['envs'] if e['env_id']=='mismatch').get('ip_mode')=='dynamic')
    persisted=json.loads((PROFILE/'Default/envs.json').read_text(encoding='utf-8'))
    changed=next(e for e in persisted['envs'] if e['env_id']=='mismatch')
    assert changed['ip_mode']=='dynamic' and changed['expected_exit_ip']=='203.0.113.2'
    report['checks']['dynamic_selection_persists_and_keeps_old_ip']=True

    t=activate('dynamic');wait(lambda:'动态' in (page_state(t) or {}).get('status',''))
    assert command({'command':'newtab','env_id':'dynamic','url':home}).get('ok')
    report['checks']['dynamic_changed_exit_allowed']=True
    t=activate('direct');wait(lambda:'直连' in (page_state(t) or {}).get('status',''))
    wait(lambda:evaluate(t,"document.getElementById('ipnum').textContent")=='203.0.113.1')
    assert counts['direct']==len(SITES)+len(EXIT_SOURCES)
    assert evaluate(t,"[...document.querySelectorAll('.svc')].some(el=>el.textContent.includes('百度'))&&[...document.querySelectorAll('.svc')].some(el=>el.textContent.includes('哔哩哔哩'))")
    assert command({'command':'newtab','env_id':'direct','url':home}).get('ok')
    report['checks']['direct_displays_exit_and_new_sites_without_blocking_browsing']=True
    t=activate('unrecorded');wait(lambda:'尚未保存' in (page_state(t) or {}).get('status',''))
    click(t,'保存当前 IP')
    wait(lambda:next(e for e in command({'command':'list'})['envs'] if e['env_id']=='unrecorded')['expected_exit_ip']=='203.0.113.1')
    report['checks']['baseline_requires_explicit_save_and_persists']=True
    t=activate('match');wait(lambda:'检查通过' in (page_state(t) or {}).get('status',''))
    assert command({'command':'newtab','env_id':'match','url':home}).get('ok')
    report['checks']['matching_exit_auto_unlocks']=True
    t=activate('system');wait(lambda:'检查通过' in (page_state(t) or {}).get('status',''))
    assert command({'command':'newtab','env_id':'system','url':home}).get('ok')
    report['checks']['system_proxy_checks_exit_and_allows_matching_ip']=counts['system']==len(SITES)+len(EXIT_SOURCES)
    t=activate('failure');wait(lambda:'无法' in (page_state(t) or {}).get('status',''))
    assert not command({'command':'newtab','env_id':'failure','url':home}).get('ok')
    click(t,'关闭环境')
    wait(lambda:not next(e for e in command({'command':'list'})['envs'] if e['env_id']=='failure').get('tabs'))
    report['checks']['failure_stays_locked_and_close_stops_environment']=True
    wait(lambda:target('failure') is None)
    t=activate('failure')
    wait(lambda:counts['http://127.0.0.1:11006']>=2*(len(SITES)+len(EXIT_SOURCES)+len(EXIT_FALLBACKS)))
    report['checks']['closed_environment_rechecks']=counts['http://127.0.0.1:11006']>=2*(len(SITES)+len(EXIT_SOURCES)+len(EXIT_FALLBACKS))
    click(t,'关闭环境')
    report['checks']['no_other_environment_scanned']=set(counts)=={e['proxy'] for e in envs}
    report['passed']=all(report['checks'].values())
    assert report['passed'],report
finally:
    if not report.get('passed'):
        try:
            report['failure_pages'] = [
                {'env':parse_qs(urlsplit(t['url']).query).get('env',[]),
                 'url':t['url'].split('&key=')[0], 'state':page_state(t)}
                for t in targets()]
        except Exception as exc:
            report['failure_snapshot_error'] = type(exc).__name__
    report['proxy_request_counts']=counts
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(OUT/'report.json'),**report},ensure_ascii=True,indent=2),flush=True)
    kernel.TerminateProcess(pi.hProcess,0);kernel.WaitForSingleObject(pi.hProcess,10000);kernel.CloseHandle(pi.hProcess)
    service.shutdown();service.server_close();destination.shutdown();destination.server_close()
    user.CloseDesktop(desktop)
