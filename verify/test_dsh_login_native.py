"""Official DSH BrowserLogin + native host, disposable profile/private desktop."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import env_cli
from _env_driver import Cdp

OUT = ROOT / '.work/auth-popup' / ('native-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
PROFILE = OUT / 'Profile'
(PROFILE / 'Default').mkdir(parents=True)
source = (ROOT / 'lan_seats/lib/client.js').read_text(encoding='utf-8')
handler = source[source.index('    function installOverlayHost(ctx) {'):source.index('    function apply(ctx) {')]
# Read the installed vendor implementation unchanged; never patch DSH's auth code.
vendor = (ROOT / '.work/dsh-asar/dsh-client-ui-settings-account/lib_client.js').read_text(encoding='utf-8')
login_class = vendor[vendor.index('var BrowserLogin = class {'):vendor.index('\n\t\t//#endregion', vendor.index('var BrowserLogin = class {'))]
fixture = ("""<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0}.shell{display:flex;height:100vh}.mP3x-q_sidebarCol{width:280px;flex-shrink:0;background:#eff4ff}
.mP3x-q_centerCol{flex:1;background:#fff;padding:20px}button{margin:12px;padding:12px}
[role=dialog]{position:fixed;inset:40px;background:#dde8ff;padding:30px}
</style></head><body><div class="shell"><aside class="mP3x-q_sidebarCol"><div class="GAkraW_root">
<nav><button aria-label="我的环境">我的环境</button></nav><button id="login">DSH 登录</button>
</div></aside><main class="mP3x-q_centerCol">管理页面</main></div><script>"""
+ handler + '\n' + login_class + """
installOverlayHost({effect: effect => effect()});
window.state={};window.count=0;
window.browser=new BrowserLogin(url=>url);
function dialog(){document.querySelector('[role=dialog]')?.remove();const el=document.createElement('div');el.setAttribute('role','dialog');el.innerHTML='<h2>等待登录</h2><button id="cancel">取消</button>';document.body.append(el);el.querySelector('#cancel').onclick=()=>{state={attempt:{...state.attempt,phase:'cancelled'}};browser.update(state);el.remove()};}
document.querySelector('#login').onclick=async()=>{dialog();const n=++count;await browser.start(async()=>{await new Promise(r=>setTimeout(r,150));state={attempt:{id:'attempt-'+n,phase:'waiting-browser',authorizeUrl:location.origin+'/authorize?attempt='+n}};return state},()=>state)};
window.finish=()=>{state={status:'credential-stored',attempt:{...state.attempt,phase:'succeeded'}};browser.update(state);document.querySelector('[role=dialog]')?.remove();document.body.dataset.authorized='true'};
</script></body></html>""").encode('utf-8')


class Fixture(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # The private browser closes pooled connections at teardown.

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = fixture if self.path == '/overlay' else b'<html><body style="background:#c7f5dc">Authorization or browsing fixture</body></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
(OUT / '.dsh-web.json').write_text(json.dumps({'url': base+'/overlay'}), encoding='utf-8')
user, kernel = c.WinDLL('user32', use_last_error=True), c.WinDLL('kernel32', use_last_error=True)


class SI(c.Structure):
    _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR), ('lpTitle', w.LPWSTR),
                ('dwX', w.DWORD), ('dwY', w.DWORD), ('dwXSize', w.DWORD), ('dwYSize', w.DWORD),
                ('dwXCountChars', w.DWORD), ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD),
                ('dwFlags', w.DWORD), ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
                ('lpReserved2', c.c_void_p), ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]


class PI(c.Structure):
    _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE), ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]


user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p]
user.CreateDesktopW.restype = w.HANDLE
user.CloseDesktop.argtypes = [w.HANDLE]
kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD, c.c_void_p,
                                 w.LPCWSTR, c.POINTER(SI), c.POINTER(PI)]
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
desktop_name = 'BlueBeanPanelTest_' + uuid.uuid4().hex[:10]
desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x1ff, None)
assert desktop, c.WinError(c.get_last_error())
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
chrome = Path(os.environ.get('BLUEBEAN_AUTH_TEST_CHROME', ROOT / '.work/auth-popup/browser/chrome.exe'))
argv = [str(chrome), f'--user-data-dir={PROFILE}', f'--remote-debugging-port={port}', '--no-first-run',
        '--no-default-browser-check', '--window-size=1500,950', '--disable-background-networking',
        '--disable-component-update', '--disable-features=CalculateNativeWinOcclusion',
        '--enable-logging', '--log-file='+str(OUT/'native.log'),
        '--lan-env-check-port=' + str(server.server_port), base + '/page']
environment = dict(os.environ, LAN_PRODUCT_HOME=str(OUT), BLUEBEAN_DATA=str(OUT))
block = c.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(environment.items())) + '\0\0')
si, pi = SI(), PI()
si.cb = c.sizeof(si)
si.lpDesktop = 'WinSta0\\' + desktop_name
report = {'checks': [], 'passed': False,
          'official_service': bool(os.environ.get('BLUEBEAN_AUTH_OFFICIAL_STATE')),
          'empty_environment_catalog': bool(os.environ.get('BLUEBEAN_AUTH_TEST_EMPTY')),
          'scope': 'private desktop, disposable profile, official BrowserLogin and real native host'}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait(fn, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.05)
    raise TimeoutError('condition not reached')


def command(**payload):
    reply = env_cli.send(PROFILE / 'Default', payload, 5)
    assert reply.get('ok'), reply
    return reply


def targets():
    with opener.open(f'http://127.0.0.1:{port}/json/list', timeout=2) as response:
        return json.load(response)


def evaluate(target, expression):
    with Cdp(target['webSocketDebuggerUrl']) as connection:
        message = connection.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'userGesture': True})
        assert 'result' in message, message
        reply = message['result']
        assert not reply.get('exceptionDetails'), reply
        return reply.get('result', {}).get('value')


def geometry():
    return command(command='overlay', query=True)['geometry']


def measure(text, key):
    match = re.search(key + r'=(-?\d+),(-?\d+) (\d+)x(\d+)', text)
    assert match, text
    return tuple(map(int, match.groups()))


def official_login():
    # Only a disposable DSH home may be provided. Never sign into a real account.
    state_file = Path(os.environ['BLUEBEAN_AUTH_OFFICIAL_STATE']).resolve()
    assert state_file.is_relative_to((ROOT / '.work/auth-popup/OfficialData').resolve())
    state = json.loads(state_file.read_text(encoding='utf-8'))
    url = state['url']
    command(command='overlay', on=True, url=url)
    origin = urllib.parse.urlsplit(url)
    panel = wait(lambda: next((t for t in targets() if t.get('url', '').startswith(
        f'{origin.scheme}://{origin.netloc}/')), None))
    wait(lambda: evaluate(panel, "!!document.querySelector('.GAkraW_root')"), 30)
    evaluate(panel, "[...document.querySelectorAll('button')].find(b=>['继续','Continue'].includes(b.textContent))?.click()")
    evaluate(panel, "document.querySelector('[aria-label=账号菜单]').click()")
    wait(lambda: evaluate(panel, "[...document.querySelectorAll('[role=menuitem]')].some(b=>b.textContent==='登录')"))
    evaluate(panel, "[...document.querySelectorAll('[role=menuitem]')].find(b=>b.textContent==='登录').click()")
    popup = wait(lambda: next((t for t in targets() if urllib.parse.urlsplit(t.get('url', '')).hostname
        == 'platform.deepseek.com'), None), 45)
    parsed = urllib.parse.urlsplit(popup['url'])
    assert parsed.path in ('/dsh/authorize', '/sign_in'), parsed.path
    # Store only the host/path; authorization queries never enter reports.
    report['checks'].append('real_official_login_opens_platform_authorization_automatically')
    wait(lambda: 'layer=0' in geometry())
    report['checks'].append('real_official_wait_dialog_releases_browser')
    # An active callback must reject missing state/code, rather than accepting
    # an ordinary Platform website login as a completed Harness grant.
    try:
        opener.open(f'{origin.scheme}://{origin.netloc}/oauth/callback', timeout=5)
        raise AssertionError('callback unexpectedly accepted missing state/code')
    except urllib.error.HTTPError as error:
        assert error.code == 400, error.code
    report['checks'].append('real_official_callback_is_registered_and_rejects_invalid_state')
    command(command='overlay', on=True)
    wait(lambda: 'modal=1' in geometry())
    evaluate(panel, "[...document.querySelectorAll('[role=dialog] button')].find(b=>b.textContent==='取消').click()")
    wait(lambda: 'modal=0' in geometry())
    report['checks'].append('real_official_cancel_dismisses_wait_dialog')
    # After cross-origin navigation Platform may sever its WindowProxy. The
    # vendor cancellation must release DSH; don't equate a retained website
    # window with an active Harness authorization attempt.
    remaining = [t for t in targets() if urllib.parse.urlsplit(t.get('url', '')).hostname == 'platform.deepseek.com']
    report['official_popup_closed_on_cancel'] = not remaining
    command(command='overlay', mode='rail')
    wait(lambda: 'collapsed=1' in geometry())
    report['checks'].append('real_official_cancel_restores_normal_panel_collapse')


try:
    assert kernel.CreateProcessW(str(chrome), c.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None,
        False, 0x400 | 0x200, block, str(chrome.parent), c.byref(si), c.byref(pi)), c.WinError(c.get_last_error())
    kernel.CloseHandle(pi.hThread)
    wait(targets)
    if not os.environ.get('BLUEBEAN_AUTH_TEST_EMPTY'):
        command(command='create', env_id='login-gated', proxy='socks5://127.0.0.1:1', start_url=base+'/page')
        command(command='activate', env_id='login-gated')
    if os.environ.get('BLUEBEAN_AUTH_OFFICIAL_STATE'):
        official_login()
    else:
        command(command='overlay', on=True, url=base+'/overlay')
        panel = wait(lambda: next((t for t in targets() if t.get('url') == base+'/overlay'), None))
        wait(lambda: evaluate(panel, "!!document.querySelector('#login')"))
        wait(lambda: 'bootstrap=0' in geometry())
        assert evaluate(panel, "document.querySelector('#login').click();true")
        popup = wait(lambda: next((t for t in targets() if t.get('url') == base+'/authorize?attempt=1'), None), 8)
        assert evaluate(panel, '!!browser.popup && !browser.popup.closed && browser.navigated')
        report['checks'].append('official_preopen_windowproxy_survives_delayed_authorize_navigation')
        assert 'layer=0' in geometry(), geometry()
        report['checks'].append('login_window_automatically_reveals_authorization_without_overlay')
        assert evaluate(popup, 'document.body.textContent.includes("Authorization")')
        report['checks'].append('authorization_bypasses_environment_startup_gate_in_own_context')
        command(command='overlay', on=True)
        wait(lambda: 'modal=1' in geometry() and 'layer=1' in geometry())
        command(command='overlay', mode='rail')
        wait(lambda: 'layer=0' in geometry())
        assert evaluate(panel, '!!browser.popup && !browser.popup.closed')
        report['checks'].append('waiting_modal_can_hide_and_reopen_without_cancelling_login')
        command(command='overlay', on=True)
        evaluate(panel, "document.querySelector('#cancel').click()")
        wait(lambda: not any(t.get('url') == base+'/authorize?attempt=1' for t in targets()))
        wait(lambda: 'modal=0' in geometry())
        report['checks'].append('official_cancel_closes_owned_authorization_window')
        # Three subsequent attempts exercise cancellation, expiry and success.
        for n, phase in ((2, 'failed'), (3, 'expired'), (4, 'succeeded')):
            command(command='overlay', on=True)
            evaluate(panel, "document.querySelector('#login').click()")
            wait(lambda: any(t.get('url') == base+f'/authorize?attempt={n}' for t in targets()))
            assert 'layer=0' in geometry()
            if phase == 'succeeded':
                evaluate(panel, 'finish()')
                assert evaluate(panel, 'document.body.dataset.authorized') == 'true'
                command(command='overlay', on=True)
                wait(lambda: 'modal=0' in geometry())
                # DSH retains an authorized page on success by design.
                assert any(t.get('url') == base+f'/authorize?attempt={n}' for t in targets())
            else:
                evaluate(panel, 'state.attempt.phase='+json.dumps(phase)+';browser.update(state)')
                wait(lambda: not any(t.get('url') == base+f'/authorize?attempt={n}' for t in targets()))
                evaluate(panel, "document.querySelector('[role=dialog]')?.remove()")
            report['checks'].append('official_login_lifecycle_'+phase)
        # Other dialogs still reveal their complete native viewport.
        evaluate(panel, "const d=document.createElement('div');d.role='dialog';document.body.append(d)")
        command(command='overlay', on=True)
        wait(lambda: 'modal=1' in geometry())
        assert measure(geometry(), 'native_clip')[2] > 280
        report['checks'].append('non_login_setup_dialog_still_has_full_viewport')
        evaluate(panel, "document.querySelector('[role=dialog]').remove()")
        wait(lambda: 'modal=0' in geometry())
        command(command='overlay', mode='rail')
        evaluate(panel, "document.querySelector('nav button').click()")
        wait(lambda: 'collapsed=0' in geometry())
        report['checks'].append('my_environments_navigation_still_reopens_native_panel')
    report['passed'] = True
finally:
    try:
        report['remaining_targets'] = [{'id': t['id'], 'path': urllib.parse.urlsplit(t.get('url', '')).path}
                                      for t in targets()]
    except Exception:
        pass
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(OUT/'report.json'), **report}, ensure_ascii=True), flush=True)
    if pi.hProcess:
        kernel.TerminateProcess(pi.hProcess, 0)
        kernel.WaitForSingleObject(pi.hProcess, 10000)
        kernel.CloseHandle(pi.hProcess)
    server.shutdown()
    server.server_close()
    user.CloseDesktop(desktop)
