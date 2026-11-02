"""Test real environment media playback on a private, never-displayed desktop."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import env_cli

OUT = ROOT / '.work/media-playback' / ('native-' + uuid.uuid4().hex[:8])
PROFILE = OUT / 'Profile'
(PROFILE / 'Default').mkdir(parents=True)
CHROME = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / '.work/media-playback/browser/chrome.exe').resolve()
assert CHROME.is_file(), CHROME
user, kernel = c.WinDLL('user32', use_last_error=True), c.WinDLL('kernel32', use_last_error=True)

class SI(c.Structure):
    _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR), ('lpTitle', w.LPWSTR),
        ('dwX', w.DWORD), ('dwY', w.DWORD), ('dwXSize', w.DWORD), ('dwYSize', w.DWORD),
        ('dwXCountChars', w.DWORD), ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD),
        ('dwFlags', w.DWORD), ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
        ('lpReserved2', c.POINTER(w.BYTE)), ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]

class PI(c.Structure):
    _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE), ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]

user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p]
user.CreateDesktopW.restype = w.HANDLE
user.CloseDesktop.argtypes = [w.HANDLE]
kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD, c.c_void_p,
    w.LPCWSTR, c.POINTER(SI), c.POINTER(PI)]
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
name = 'BlueBeanMedia_' + uuid.uuid4().hex[:10]
desktop = user.CreateDesktopW(name, None, None, 0, 0x1ff, None)
assert desktop, c.WinError(c.get_last_error())
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
argv = [str(CHROME), f'--user-data-dir={PROFILE}', f'--remote-debugging-port={port}', '--no-first-run',
    '--no-default-browser-check', '--window-size=1400,900', '--disable-background-networking',
    '--disable-component-update', '--disable-features=CalculateNativeWinOcclusion', 'about:blank']
env = dict(os.environ, LAN_PRODUCT_HOME=str(OUT), BLUEBEAN_DATA=str(OUT))
block = c.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(env.items())) + '\0\0')
si, pi = SI(), PI()
si.cb, si.lpDesktop = c.sizeof(si), 'WinSta0\\' + name
launched = False
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    assert kernel.CreateProcessW(str(CHROME), c.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None,
        False, 0x400 | 0x200, block, str(CHROME.parent), c.byref(si), c.byref(pi)), c.WinError(c.get_last_error())
    launched = True
    kernel.CloseHandle(pi.hThread)
    deadline = time.monotonic() + 25
    while True:
        try:
            with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
                json.load(response)
            break
        except OSError:
            assert time.monotonic() < deadline, 'private browser did not start'
            time.sleep(.1)
    for command in ({'command': 'create', 'env_id': 'media-isolation-test', 'proxy': 'direct', 'fingerprint_seed': '673921'},
                    {'command': 'activate', 'env_id': 'media-isolation-test'}):
        reply = env_cli.send(PROFILE / 'Default', command, 8)
        assert reply.get('ok'), reply
    baseline = '--baseline' in sys.argv
    result = subprocess.run(['node', str(ROOT / 'verify/test_media_playback.cjs'), '--chrome', str(CHROME),
        '--cdp', f'http://127.0.0.1:{port}', '--report', 'decode-native-before.json' if baseline else 'decode-native.json',
        *(['--baseline'] if baseline else [])], cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, text=True, encoding='utf-8', errors='replace')
    (OUT / 'test.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    print(result.stdout, flush=True)
    assert result.returncode == 0, f'native media playback failed: {result.returncode}; {result.stderr[-1500:]}'
    if '--sites' in sys.argv:
        (OUT / 'session.json').write_text(json.dumps({'port': port, 'profile': str(PROFILE)}), encoding='utf-8')
        result = subprocess.run(['node', str(ROOT / 'verify/probe_video_sites.cjs'), f'http://127.0.0.1:{port}', str(OUT)],
            cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, 'site verification failed to run'
    report = {'ok': True, 'scope': 'private desktop, disposable direct environment',
        'playback_report': str(ROOT / '.work/media-playback' / ('decode-native-before.json' if baseline else 'decode-native.json'))}
    (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
finally:
    if launched:
        subprocess.run(['taskkill', '/PID', str(pi.dwProcessId), '/T', '/F'], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        kernel.WaitForSingleObject(pi.hProcess, 5000)
        kernel.CloseHandle(pi.hProcess)
    user.CloseDesktop(desktop)
