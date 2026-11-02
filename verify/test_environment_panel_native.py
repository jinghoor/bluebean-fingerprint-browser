"""Verify the production click handler against the real native clip, on a private desktop."""
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import env_cli
from _env_driver import Cdp

OUT = ROOT / '.work/environment-panel-visibility' / ('native-' + time.strftime('%Y%m%d-%H%M%S'))
PROFILE = OUT / 'Profile'
(PROFILE / 'Default').mkdir(parents=True)
source = (ROOT / 'lan_seats/lib/client.js').read_text(encoding='utf-8')
handler = source[source.index('    function installOverlayHost(ctx) {'):source.index('    function apply(ctx) {')]
fixture = ('''<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0}.shell{display:flex;height:100vh}.mP3x-q_sidebarCol{width:280px;flex-shrink:0;background:#eff4ff}
.mP3x-q_centerCol{flex:1;background:#fff;padding:20px}button{display:block;margin:12px;padding:12px}
</style></head><body><div class="shell"><aside class="mP3x-q_sidebarCol"><div class="GAkraW_root">
<nav><button aria-label="我的环境"><span>我的环境</span></button></nav>
<button class="lan-env-sidebar-toggle">切换列表</button><button class="lan-env-sidebar-env">测试环境</button>
<button class="lan-env-sidebar-account">测试账号</button><button id="conversation">测试会话</button>
</div></aside><main class="mP3x-q_centerCol">管理页面</main></div><script>'''
           + handler + '\ninstallOverlayHost({effect: effect => effect()});</script></body></html>').encode('utf-8')


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = fixture if self.path == '/overlay' else b'<html><body style="background:#c7f5dc">Browsing page</body></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
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
chrome = Path(r'E:\chromium\src\out\Release-pgo\chrome.exe')
argv = [str(chrome), f'--user-data-dir={PROFILE}', f'--remote-debugging-port={port}', '--no-first-run',
        '--no-default-browser-check', '--window-size=1500,950', '--disable-background-networking',
        '--disable-component-update', '--disable-features=CalculateNativeWinOcclusion', base + '/page']
environment = dict(os.environ, LAN_PRODUCT_HOME=str(OUT), BLUEBEAN_DATA=str(OUT))
block = c.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(environment.items())) + '\0\0')
si, pi = SI(), PI()
si.cb = c.sizeof(si)
si.lpDesktop = 'WinSta0\\' + desktop_name
report = {'checks': [], 'passed': False, 'scope': 'private desktop, disposable profile, production click handler and real native host'}
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
        reply = connection.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True})['result']
        assert not reply.get('exceptionDetails'), reply
        return reply.get('result', {}).get('value')


def geometry():
    return command(command='overlay', query=True)['geometry']


def measure(text, key):
    match = re.search(key + r'=(-?\d+),(-?\d+) (\d+)x(\d+)', text)
    assert match, text
    return tuple(map(int, match.groups()))


try:
    assert kernel.CreateProcessW(str(chrome), c.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None,
        False, 0x400 | 0x200, block, str(chrome.parent), c.byref(si), c.byref(pi)), c.WinError(c.get_last_error())
    kernel.CloseHandle(pi.hThread)
    wait(targets)
    page = wait(lambda: next((t for t in targets() if t.get('url') == base + '/page'), None))
    command(command='overlay', on=True, url=base + '/overlay')
    panel = wait(lambda: next((t for t in targets() if t.get('url') == base + '/overlay'), None))
    wait(lambda: evaluate(panel, "!!document.querySelector('.GAkraW_root')"))
    initial = wait(lambda: (g if 'collapsed=1' in (g := geometry()) and 'bootstrap=0' in g else None))
    assert measure(initial, 'native_clip')[2] == 280, initial
    report['checks'].append('first_mount_is_sidebar_only')
    original_page = measure(initial, 'page')
    for cycle in range(8):
        command(command='overlay', mode='rail')
        wait(lambda: 'collapsed=1' in geometry())
        # Use the nested icon/text target, as real sidebar clicks do.
        assert evaluate(panel, "(()=>{const b=document.querySelector('nav button span');b.click();return true})()")
        expanded = wait(lambda: (g if 'collapsed=0' in (g := geometry()) else None))
        assert measure(expanded, 'native_clip')[2] > 280, expanded
        assert measure(expanded, 'page') == original_page, expanded
    report['checks'].append('eight_reopens_reveal_native_clip_without_page_reflow')
    command(command='overlay', mode='rail')
    for selector in ('.lan-env-sidebar-toggle', '.lan-env-sidebar-env', '.lan-env-sidebar-account'):
        evaluate(panel, 'document.querySelector(' + json.dumps(selector) + ').click()')
        assert 'collapsed=1' in geometry(), selector
    report['checks'].append('mode_environment_account_switches_leave_clip_collapsed')
    # Compact rail: ensure the same handler measures the actual sidebar width.
    evaluate(panel, "document.querySelector('.mP3x-q_sidebarCol').style.width='56px'")
    wait(lambda: measure(geometry(), 'native_clip')[2] == 56)
    evaluate(panel, "document.querySelector('nav button').click()")
    expanded = wait(lambda: (g if 'collapsed=0' in (g := geometry()) else None))
    assert measure(expanded, 'native_clip')[2] > 280
    assert 'reserved=56' in expanded, expanded
    report['checks'].append('compact_rail_reopens_with_only_56_pixels_reserved')
    report['passed'] = True
finally:
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(OUT / 'report.json'), **report}, ensure_ascii=True), flush=True)
    if pi.hProcess:
        kernel.TerminateProcess(pi.hProcess, 0)
        kernel.WaitForSingleObject(pi.hProcess, 10000)
        kernel.CloseHandle(pi.hProcess)
    server.shutdown()
    server.server_close()
    user.CloseDesktop(desktop)
