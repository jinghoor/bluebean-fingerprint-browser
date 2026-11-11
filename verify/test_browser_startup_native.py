"""Compare browser-process ACG on a private desktop using disposable profiles.

No packaging, input-desktop switching, real accounts, or global mouse/keyboard.
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
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _browser_launch import compatible_browser_argv
from _env_driver import Cdp

CHROME = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "chromium/out/Release-pgo/chrome.exe").resolve()
assert CHROME.is_file(), CHROME
OUT = ROOT / ".work/startup-compatibility" / ("native-" + uuid.uuid4().hex[:10])
OUT.mkdir(parents=True)
user, kernel = c.WinDLL("user32", use_last_error=True), c.WinDLL("kernel32", use_last_error=True)


class SI(c.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
        ("lpReserved2", c.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class PI(c.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p]
user.CreateDesktopW.restype = w.HANDLE
user.CloseDesktop.argtypes = [w.HANDLE]
user.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
user.OpenInputDesktop.restype = w.HANDLE
user.GetUserObjectInformationW.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD, c.c_void_p,
    w.LPCWSTR, c.POINTER(SI), c.POINTER(PI)]
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel.GetProcessMitigationPolicy.argtypes = [w.HANDLE, c.c_int, c.c_void_p, c.c_size_t]
kernel.GetProcessMitigationPolicy.restype = w.BOOL


def input_desktop():
    handle = user.OpenInputDesktop(0, False, 1)
    assert handle, c.WinError(c.get_last_error())
    try:
        name, size = c.create_unicode_buffer(256), w.DWORD()
        assert user.GetUserObjectInformationW(handle, 2, name, c.sizeof(name), c.byref(size))
        return name.value
    finally:
        user.CloseDesktop(handle)


desktop_name = "BlueBeanStartupCompat_" + uuid.uuid4().hex[:10]
desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x1ff, None)
assert desktop, c.WinError(c.get_last_error())
before = input_desktop()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(guarded):
    home = OUT / ("guarded" if guarded else "feature-enabled")
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    argv = [str(CHROME), f"--user-data-dir={home / 'Profile'}", f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check", "--no-proxy-server",
        "--disable-background-networking", "--disable-component-update",
        "--enable-features=BrowserDynamicCodeDisabled", "--disable-features=CalculateNativeWinOcclusion",
        "--enable-logging", f"--log-file={home / 'browser.log'}", "about:blank"]
    if guarded:
        argv = compatible_browser_argv(argv)
    assert "--no-sandbox" not in argv and "--in-process-gpu" not in argv
    env = dict(os.environ, BLUEBEAN_HOME=str(ROOT), BLUEBEAN_DATA=str(home), LAN_PRODUCT_HOME=str(home),
               BLUEBEAN_PROFILE=str(home / "Profile"), BLUEBEAN_PROFILE_ROOT=str(home),
               BLUEBEAN_PROXY_CONFIG=str(home / "proxies.json"), DSH_HOME=str(home / "DSH"))
    block = c.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
    si, pi = SI(), PI()
    si.cb, si.lpDesktop = c.sizeof(si), "WinSta0\\" + desktop_name
    result = {"guarded": guarded, "sandbox_disabled": False}
    launched = False
    started = time.monotonic()
    try:
        assert kernel.CreateProcessW(str(CHROME), c.create_unicode_buffer(subprocess.list2cmdline(argv)),
            None, None, False, 0x400 | 0x200, block, str(CHROME.parent), c.byref(si), c.byref(pi)), c.WinError(c.get_last_error())
        launched = True
        kernel.CloseHandle(pi.hThread)
        deadline = started + 30
        while time.monotonic() < deadline:
            code = w.DWORD()
            assert kernel.GetExitCodeProcess(pi.hProcess, c.byref(code))
            if code.value != 259:
                result["exit_code"] = code.value
                break
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=.3) as response:
                    targets = json.load(response)
                page = next(t for t in targets if t.get("type") == "page")
                with Cdp(page["webSocketDebuggerUrl"]) as cdp:
                    value = cdp.evaluate("document.body.innerHTML='<h1>Startup OK</h1>'; ({value:1+99, text:document.body.innerText})")
                if not value or value.get("value") != 100:
                    raise ValueError("renderer not ready")
                result["renderer"] = value
                policy = w.DWORD()
                assert kernel.GetProcessMitigationPolicy(pi.hProcess, 2, c.byref(policy), c.sizeof(policy)), c.WinError(c.get_last_error())
                result["dynamic_code_policy_flags"] = policy.value
                result["prohibit_dynamic_code"] = bool(policy.value & 1)
                break
            except (OSError, ValueError, StopIteration):
                time.sleep(.1)
        result["seconds"] = round(time.monotonic() - started, 3)
        return result
    finally:
        if launched:
            subprocess.run(["taskkill", "/PID", str(pi.dwProcessId), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            kernel.WaitForSingleObject(pi.hProcess, 5000)
            kernel.CloseHandle(pi.hProcess)


report = {"windows": str(sys.getwindowsversion()), "chrome": str(CHROME), "input_desktop_before": before}
try:
    report["feature_enabled"] = check(False)
    print(json.dumps(report["feature_enabled"]), flush=True)
    report["guarded"] = check(True)
    print(json.dumps(report["guarded"]), flush=True)
    assert report["guarded"].get("renderer", {}).get("value") == 100, "guarded browser did not render"
    assert report["guarded"].get("prohibit_dynamic_code") is False, "ACG remained enabled (check managed policy)"
    report["input_desktop_after"] = input_desktop()
    assert report["input_desktop_after"] == before
    report["passed"] = True
finally:
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(str(OUT / "report.json"), flush=True)
    user.CloseDesktop(desktop)
