"""Run the installed product on a private Windows desktop via CDP and its API.

Never switches the user's input desktop or sends global keyboard/mouse input.
All browser profiles, model settings and credentials are disposable test data.
"""
from __future__ import annotations
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

import websockets.sync.client

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path, required=True)
parser.add_argument("--data", type=Path, required=True)
parser.add_argument("--medium", action="store_true")
parser.add_argument("--leave-services", action="store_true",
                    help="Keep only this disposable home's services for an in-place upgrade test")
args = parser.parse_args()
app, data = args.app.resolve(), args.data.resolve()
data.mkdir(parents=True, exist_ok=True)
os.environ.update(BLUEBEAN_DATA=str(data), BLUEBEAN_HOME=str(app),
                  BLUEBEAN_PROFILE=str(data / "BrowserProfile"))
sys.path.insert(0, str(app))
import env_cli
from _runtime_state import process_identity

user = c.WinDLL("user32", use_last_error=True)
kernel = c.WinDLL("kernel32", use_last_error=True)
class STARTUPINFO(c.Structure):
    _fields_ = [("cb",w.DWORD),("lpReserved",w.LPWSTR),("lpDesktop",w.LPWSTR),
        ("lpTitle",w.LPWSTR),("dwX",w.DWORD),("dwY",w.DWORD),
        ("dwXSize",w.DWORD),("dwYSize",w.DWORD),("dwXCountChars",w.DWORD),
        ("dwYCountChars",w.DWORD),("dwFillAttribute",w.DWORD),("dwFlags",w.DWORD),
        ("wShowWindow",w.WORD),("cbReserved2",w.WORD),("lpReserved2",c.c_void_p),
        ("hStdInput",w.HANDLE),("hStdOutput",w.HANDLE),("hStdError",w.HANDLE)]
class PROCESSINFO(c.Structure):
    _fields_ = [("hProcess",w.HANDLE),("hThread",w.HANDLE),
               ("dwProcessId",w.DWORD),("dwThreadId",w.DWORD)]
kernel.CreateProcessW.argtypes = [w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,
    w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(STARTUPINFO),c.POINTER(PROCESSINFO)]
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.GetCurrentProcess.restype = w.HANDLE
kernel.ResumeThread.argtypes = [w.HANDLE]
user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p]
user.CreateDesktopW.restype = w.HANDLE
user.CloseDesktop.argtypes = [w.HANDLE]
user.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
user.OpenInputDesktop.restype = w.HANDLE
user.GetUserObjectInformationW.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
user.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
user.IsWindowVisible.argtypes = [w.HWND]
user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
CALLBACK = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user.EnumDesktopWindows.argtypes = [w.HANDLE, CALLBACK, w.LPARAM]


def input_desktop():
    handle = user.OpenInputDesktop(0, False, 1)
    if not handle:
        return "unavailable:" + str(c.get_last_error())
    try:
        name = c.create_unicode_buffer(256)
        length = w.DWORD()
        assert user.GetUserObjectInformationW(handle, 2, name, c.sizeof(name), c.byref(length))
        return name.value
    finally:
        user.CloseDesktop(handle)


def wait_for(callback, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = callback()
            if last:
                return last
        except (OSError, ValueError, KeyError, TimeoutError):
            pass
        time.sleep(0.1)
    raise TimeoutError(str(last)[:250])


def read(name):
    return json.loads((data / name).read_text(encoding="utf-8"))


def rpc(url, method, params=None):
    with websockets.sync.client.connect(url, open_timeout=5, max_size=16 * 1024 * 1024) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reply = json.loads(ws.recv(timeout=10))
            if reply.get("id") == 1:
                if "error" in reply:
                    raise RuntimeError(str(reply["error"]))
                return reply.get("result", {})
    raise TimeoutError(method)


def evaluate(target, expression):
    r = rpc(target["webSocketDebuggerUrl"], "Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in r:
        raise RuntimeError(str(r["exceptionDetails"]))
    return r.get("result", {}).get("value")


def command(payload):
    return env_cli.send(env_cli.resolve_profile_dir(data / "BrowserProfile"), payload, 8)


def api(path, payload=None):
    bridge = read(".lan-bridge.json")
    request = urllib.request.Request(f"http://127.0.0.1:{bridge['port']}"+path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization":"Bearer "+bridge["token"],"Content-Type":"application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
        return json.load(response)


def launch_product():
    info = STARTUPINFO()
    info.cb = c.sizeof(info)
    info.lpDesktop = "WinSta0\\" + desktop_name
    process = PROCESSINFO()
    block = c.create_unicode_buffer("\0".join(k+"="+v for k,v in sorted(env.items()))+"\0\0")
    if args.medium:
        api32 = c.WinDLL("advapi32", use_last_error=True)
        class SIDATTR(c.Structure):
            _fields_ = [("sid",c.c_void_p),("attributes",w.DWORD)]
        api32.OpenProcessToken.argtypes = [w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
        api32.ConvertStringSidToSidW.argtypes = [w.LPCWSTR,c.POINTER(c.c_void_p)]
        api32.CreateRestrictedToken.argtypes = [w.HANDLE,w.DWORD,w.DWORD,c.POINTER(SIDATTR),w.DWORD,c.c_void_p,w.DWORD,c.c_void_p,c.POINTER(w.HANDLE)]
        api32.SetTokenInformation.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
        api32.GetTokenInformation.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)]
        api32.ConvertSidToStringSidW.argtypes = [c.c_void_p,c.POINTER(w.LPWSTR)]
        api32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p]
        api32.GetSecurityDescriptorDacl.argtypes = [c.c_void_p,c.POINTER(w.BOOL),c.POINTER(c.c_void_p),c.POINTER(w.BOOL)]
        api32.SetKernelObjectSecurity.argtypes = [w.HANDLE,w.DWORD,c.c_void_p]
        api32.CreateProcessWithTokenW.argtypes = [w.HANDLE,w.DWORD,w.LPCWSTR,w.LPWSTR,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(STARTUPINFO),c.POINTER(PROCESSINFO)]
        source,filtered=w.HANDLE(),w.HANDLE()
        admin,medium=c.c_void_p(),c.c_void_p()
        descriptor,user_sid_text=c.c_void_p(),w.LPWSTR()
        def checked(result):
            if not result: raise c.WinError(c.get_last_error())
        try:
            checked(api32.OpenProcessToken(kernel.GetCurrentProcess(),0xF01FF,c.byref(source)))
            checked(api32.ConvertStringSidToSidW("S-1-5-32-544",c.byref(admin)))
            disabled=SIDATTR(admin,0)
            checked(api32.CreateRestrictedToken(source,1,1,c.byref(disabled),0,None,0,None,c.byref(filtered)))
            # The built-in Administrator's owner/default ACL may contain only
            # Administrators and SYSTEM. A filtered token must own/access its
            # own pipes, child processes and tokens like an ordinary user.
            required=w.DWORD()
            api32.GetTokenInformation(filtered,1,None,0,c.byref(required))
            token_user=c.create_string_buffer(required.value)
            checked(api32.GetTokenInformation(filtered,1,token_user,len(token_user),c.byref(required)))
            user_sid=c.c_void_p.from_buffer(token_user)
            checked(api32.SetTokenInformation(filtered,4,c.byref(user_sid),c.sizeof(user_sid)))
            checked(api32.ConvertSidToStringSidW(user_sid,c.byref(user_sid_text)))
            checked(api32.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:(A;;GA;;;"+user_sid_text.value+")(A;;GA;;;SY)",1,c.byref(descriptor),None))
            present,defaulted=w.BOOL(),w.BOOL()
            acl=c.c_void_p()
            checked(api32.GetSecurityDescriptorDacl(descriptor,c.byref(present),c.byref(acl),c.byref(defaulted)))
            checked(api32.SetTokenInformation(filtered,6,c.byref(acl),c.sizeof(acl)))
            checked(api32.SetKernelObjectSecurity(filtered,4,descriptor))
            checked(api32.ConvertStringSidToSidW("S-1-16-8192",c.byref(medium)))
            label=SIDATTR(medium,0x20)
            checked(api32.SetTokenInformation(filtered,25,c.byref(label),c.sizeof(label)+12))
            checked(api32.CreateProcessWithTokenW(filtered,0,str(app/"BlueBean.exe"),None,
                subprocess.CREATE_NO_WINDOW|0x404,block,str(app),c.byref(info),c.byref(process)))
            from windows_security import process_integrity
            assert process_integrity(process.dwProcessId)==0x2000
            kernel.ResumeThread(process.hThread)
        finally:
            for handle in (filtered,source):
                if handle: kernel.CloseHandle(handle)
            kernel.LocalFree.argtypes=[c.c_void_p]
            for sid in (admin,medium,descriptor,c.cast(user_sid_text,c.c_void_p)):
                if sid: kernel.LocalFree(sid)
    else:
        assert kernel.CreateProcessW(str(app/"BlueBean.exe"),None,None,None,False,
            subprocess.CREATE_NO_WINDOW|0x400,block,str(app),c.byref(info),c.byref(process)), c.WinError(c.get_last_error())
    kernel.CloseHandle(process.hThread)
    kernel.CloseHandle(process.hProcess)


def targets():
    port = read(".env-ui-port.json")["port"]
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
        return json.load(response)


def windows(pid, surface=None):
    rows = []
    @CALLBACK
    def visit(hwnd, _):
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if not pid or owner.value == pid:
            rect = w.RECT()
            user.GetWindowRect(hwnd, c.byref(rect))
            title = c.create_unicode_buffer(512)
            user.GetWindowTextW(hwnd, title, len(title))
            window_class = c.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, window_class, len(window_class))
            rows.append({"hwnd": hwnd, "pid": owner.value, "title": title.value,
                         "class": window_class.value,
                         "rect": [rect.left, rect.top, rect.right, rect.bottom],
                         "visible": bool(user.IsWindowVisible(hwnd))})
        return True
    c.set_last_error(0)
    ok = user.EnumDesktopWindows(surface or desktop, visit, 0)
    if not ok and c.get_last_error():
        raise c.WinError(c.get_last_error())
    return rows


before = input_desktop()
desktop_name = "BlueBeanAcceptance_" + uuid.uuid4().hex[:10]
desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x1FF, None)
assert desktop, c.WinError(c.get_last_error())
env = dict(os.environ)
for name in ("DEEPSEEK_API_KEY", "PYTHONHOME", "PYTHONPATH", "DSH_HOME", "LAN_BRIDGE_TOKEN", "LAN_BRIDGE_URL"):
    env.pop(name, None)
report = {"input_desktop_before": before, "medium_token":args.medium,"checks": {}}
template_before = (app/"lan_seats/lib/client.js").read_bytes()
browser_pid = 0
try:
    started = time.monotonic()
    launch_product()
    browser = wait_for(lambda: read(".startup-browser.json"), 35)
    browser_pid = browser["pid"]
    tested_browser_pids = [browser_pid]
    time.sleep(2)
    report["private_windows"] = windows(0)
    report["checks"]["no_console_windows"] = not any(x["visible"] and x["class"]=="ConsoleWindowClass" for x in report["private_windows"])
    user.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
    user.OpenDesktopW.restype = w.HANDLE
    default_surface = user.OpenDesktopW("Default", 0, False, 0x41)
    if default_surface:
        report["browser_windows_on_default"] = windows(browser_pid, default_surface)
        user.CloseDesktop(default_surface)
    assert report.get("browser_windows_on_default") == [], "Test leaked onto the input desktop"
    visible = wait_for(lambda: next((x for x in windows(browser_pid) if x["visible"] and x["rect"][2]-x["rect"][0]>500), None), 10)
    report["browser_ready_seconds"] = browser["seconds"]
    report["native_window_seconds"] = round(time.monotonic() - started, 3)
    report["checks"]["native_window_on_private_desktop"] = True
    ready = wait_for(lambda: (s if (s := read(".startup-services.json"))["status"] == "ready" else None), 110)
    report["assistant_ready_seconds"] = ready["seconds"]
    clean_state = command({"command": "list"})
    report["checks"]["fresh_user_has_no_development_environments"] = clean_state.get("ok") is not False and clean_state.get("envs") == []
    assert report["checks"]["fresh_user_has_no_development_environments"], 'Fresh install unexpectedly contains environments'
    report["checks"]["first_run_marker_created"] = (data / ".first-run-runtime-ready-v1.json").is_file()
    dsh = read(".dsh-web.json")
    port_prefix = "http://127.0.0.1:" + str(dsh["port"])
    target = wait_for(lambda: next((t for t in targets() if t.get("url", "").startswith(port_prefix)), None), 20)
    # Match opening the AI panel before interacting with first-use dialogs.
    # A hidden WebContents can defer animation-frame based geometry updates.
    command({"command": "overlay", "on": True})
    wait_for(lambda: evaluate(target, "!!document.querySelector('[role=dialog],dialog[open],[aria-modal=true]')"), 30)
    import base64
    (data/"first-run.png").write_bytes(base64.b64decode(rpc(target["webSocketDebuggerUrl"],"Page.captureScreenshot",{"format":"png"})["data"]))
    snapshot = evaluate(target, "JSON.stringify({text:document.body.innerText.slice(0,2400),buttons:[...document.querySelectorAll('button')].map(e=>({text:e.innerText,aria:e.getAttribute('aria-label')})).slice(-35),dialogs:[...document.querySelectorAll('[role=dialog],dialog[open],[aria-modal=true]')].map(e=>({text:e.innerText.slice(0,600),width:e.getBoundingClientRect().width})),shell:!!document.querySelector('.GAkraW_root'),sidebar:!!document.querySelector('.mP3x-q_sidebarCol'),inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,placeholder:e.placeholder}))})")
    report["first_run_dom"] = json.loads(snapshot)
    # Inspect without collapsing: a rail request intentionally hides an open
    # modal, so subsequent input would otherwise target a hidden WebContents.
    geometry = command({"command": "overlay", "query": True})
    report["first_run_geometry"] = geometry
    report["checks"]["first_run_attach"] = geometry.get("ok", False)
    report["checks"]["onboarding_full_width"] = "modal=1" in geometry["geometry"] and "native_clip=0,0 1024x" in geometry["geometry"]
    # Exercise the real first-use sequence, without any actual credentials.
    evaluate(target,"[...document.querySelectorAll('[role=dialog] button')].find(e=>e.textContent.trim()==='继续')?.click()")
    time.sleep(0.6)
    report["account_dom"] = evaluate(target,"({text:document.body.innerText.slice(-3500),buttons:[...document.querySelectorAll('button')].map(e=>({text:e.textContent,aria:e.getAttribute('aria-label')})).slice(-25),inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,placeholder:e.placeholder}))})")
    report["account_geometry"] = command({"command":"overlay","query":True})
    (data/"account.png").write_bytes(base64.b64decode(rpc(target["webSocketDebuggerUrl"],"Page.captureScreenshot",{"format":"png"})["data"]))
    evaluate(target,"[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='添加 API Key')?.click()")
    time.sleep(0.5)
    report["api_key_dom"] = evaluate(target,"({text:document.body.innerText.slice(-3000),inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,placeholder:e.placeholder,rect:(()=>{let r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()})),buttons:[...document.querySelectorAll('[role=dialog] button')].map(e=>({text:e.textContent,aria:e.getAttribute('aria-label')}))})")
    report["api_key_geometry"] = command({"command":"overlay","query":True})
    (data/"api-key.png").write_bytes(base64.b64decode(rpc(target["webSocketDebuggerUrl"],"Page.captureScreenshot",{"format":"png"})["data"]))
    report["checks"]["api_key_input_reachable"] = evaluate(target,"(()=>{const e=document.querySelector('input[type=password]');if(!e)return false;const r=e.getBoundingClientRect();e.focus();return r.x>=0&&r.right<=innerWidth&&r.bottom<=innerHeight&&document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===e})()")
    rpc(target["webSocketDebuggerUrl"],"Input.insertText",{"text":"acceptance-only-not-a-real-key"})
    report["checks"]["api_key_input_editable"] = evaluate(target,"document.querySelector('input[type=password]')?.value==='acceptance-only-not-a-real-key'")
    # Clear through real editing events. A DOM value assignment leaves React's
    # credential draft dirty and correctly triggers an unsaved-changes dialog.
    for event in (
        {"type":"rawKeyDown","key":"a","code":"KeyA","windowsVirtualKeyCode":65,"modifiers":2},
        {"type":"keyUp","key":"a","code":"KeyA","windowsVirtualKeyCode":65,"modifiers":2},
        {"type":"rawKeyDown","key":"Backspace","code":"Backspace","windowsVirtualKeyCode":8},
        {"type":"keyUp","key":"Backspace","code":"Backspace","windowsVirtualKeyCode":8},
    ):
        rpc(target["webSocketDebuggerUrl"],"Input.dispatchKeyEvent",event)
    report["checks"]["temporary_key_cleared_without_saving"] = evaluate(target,"document.querySelector('input[type=password]')?.value===''")
    evaluate(target,"[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='稍后配置')?.click()")
    time.sleep(0.3)
    report["after_skip_dialogs"] = evaluate(target,"[...document.querySelectorAll('[role=dialog],dialog[open],[aria-modal=true]')].map(e=>({text:e.textContent.slice(0,1200),width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,buttons:[...e.querySelectorAll('button')].map(b=>({text:b.textContent,disabled:b.disabled}))}))")
    report["after_skip_geometry"] = command({"command":"overlay","query":True})
    wait_for(lambda: "modal=0" in command({"command":"overlay","query":True}).get("geometry",""), 8)
    report["checks"]["normal_sidebar_restored_after_onboarding"] = "native_clip=0,0 280x" in command({"command":"overlay","mode":"rail"})["geometry"]
    report["overlay_cycles"] = []
    for cycle in range(8):
        hidden = command({"command":"overlay","on":False})
        wait_for(lambda: not read("BrowserProfile/Default/lan-layer-state.json").get("visible"), 3)
        restored = command({"command":"overlay","on":True})
        wait_for(lambda: read("BrowserProfile/Default/lan-layer-state.json").get("visible"), 3)
        report["overlay_cycles"].append({"hidden":hidden.get("visible") is False,"restored":restored.get("visible") is True})
    report["checks"]["overlay_hide_restore_8_cycles"] = all(x["hidden"] and x["restored"] for x in report["overlay_cycles"])
    from _env_driver import post_click
    report["native_toolbar_cycles"] = []
    for cycle in range(8):
        post_click(visible["hwnd"],32,77)
        wait_for(lambda: not read("BrowserProfile/Default/lan-layer-state.json").get("visible"), 3)
        post_click(visible["hwnd"],32,77)
        wait_for(lambda: read("BrowserProfile/Default/lan-layer-state.json").get("visible"), 3)
        report["native_toolbar_cycles"].append(True)
    report["checks"]["native_toolbar_8_cycles"] = len(report["native_toolbar_cycles"])==8
    report["environment_list"] = api("/v1/lan/manage/list",{})
    report["checks"]["environment_api"] = bool(report["environment_list"].get("ok"))
    report["checks"]["environment_api_starts_empty"] = report["environment_list"].get("envs") == []
    assert report["checks"]["environment_api_starts_empty"], 'Management API unexpectedly seeded environments'
    created = api("/v1/lan/manage/save",{"env_id":"acceptance-001","display_name":"Acceptance environment","proxy":"direct","create_only":True})
    report["environment_save"] = created
    report["checks"]["environment_save_api"] = bool(created.get("ok"))
    selected = command({"command":"activate","env_id":"acceptance-001"})
    report["checks"]["environment_activate"] = bool(selected.get("ok"))
    from browser_backend import BrowserBackend
    backend = BrowserBackend()
    report["kernel_state_paths"] = [str(backend.KERNEL_PORT_FILE),str(backend.KERNEL_TOKEN_FILE)]
    try:
        time.sleep(0.5)
        report["kernel_observe"] = backend.kernel_task("acceptance-001",[{"observe":True}],timeout=5)
        report["checks"]["automation_transport"] = bool(report["kernel_observe"].get("ok"))
    except Exception as exc:
        report["kernel_error"] = repr(exc)
        report["kernel_error_browser_identity"] = process_identity(browser_pid)
        report["kernel_error_targets"] = [{k:t.get(k) for k in ("url","type","id")} for t in targets()]
        report["checks"]["automation_transport"] = False
    # Preserve the actual first completion marker and service identities across
    # a shortcut reuse and a browser restart.
    marker = (data/".first-run-runtime-ready-v1.json").read_bytes()
    service_before = {name:read(name) for name in (".dsh-web.json",".lan-bridge.json")}
    automation_before = (data/".lan-automation-port").read_text()
    launched_at = time.time()
    launch_product()
    reused = wait_for(lambda: (s if (s:=read(".startup-browser.json")).get("recorded_at",0)>launched_at else None), 15)
    report["checks"]["shortcut_reuses_browser_and_port"] = reused.get("reused") and reused["pid"]==browser_pid and (data/".lan-automation-port").read_text()==automation_before
    browser_target = next(t for t in targets() if t.get("type")=="page")
    try:
        rpc(browser_target["webSocketDebuggerUrl"],"Browser.close")
    except Exception:
        pass
    wait_for(lambda: not process_identity(browser_pid).get("alive"), 10)
    launched_at = time.time()
    launch_product()
    warm = wait_for(lambda: (s if (s:=read(".startup-browser.json")).get("recorded_at",0)>launched_at else None), 20)
    browser_pid=warm["pid"]
    tested_browser_pids.append(browser_pid)
    report["warm_browser_seconds"] = warm["seconds"]
    # A shortcut-reuse helper may already own initialization when the next
    # browser starts. Its completion (rather than its spawn) must be newer.
    wait_for(lambda: (s if (s:=read(".startup-services.json")).get("started_at",0)+s.get("seconds",0)>=launched_at and s["status"]=="ready" else None), 30)
    report["checks"]["warm_service_reuse"] = all(read(name)["pid"]==state["pid"] for name,state in service_before.items())
    report["checks"]["first_run_completed_once"] = (data/".first-run-runtime-ready-v1.json").read_bytes()==marker
    notice_log = (data/"logs/shortcut-launch.log").read_text(encoding="utf-8")
    report["checks"]["first_run_notice_only_once"] = notice_log.count("startup notice: first-run-runtime-preparation")==1 and notice_log.count("startup notice: normal-startup")==2
    report["checks"]["saved_environment_survives_restart"] = any(e.get("env_id")=="acceptance-001" for e in api("/v1/lan/manage/list",{}).get("envs",[]))
    report["checks"]["restart_adds_no_demo_environments"] = {
        e.get("env_id") for e in api("/v1/lan/manage/list",{}).get("envs",[])
    } == {"acceptance-001"}
    # Graceful shutdown must also be safe with an attached assistant.
    try:
        rpc(next(t for t in targets() if t.get("type")=="page")["webSocketDebuggerUrl"],"Browser.close")
    except Exception:
        pass
    wait_for(lambda: not process_identity(browser_pid).get("alive"), 10)
    import struct
    crashes = []
    for dump in (data/"BrowserProfile/Crashpad/reports").glob("*.dmp"):
        blob = dump.read_bytes()
        count, directory = struct.unpack_from("<II",blob,8)
        streams = {struct.unpack_from("<I",blob,directory+i*12)[0]:struct.unpack_from("<I",blob,directory+i*12+8)[0] for i in range(count)}
        pid = struct.unpack_from("<I",blob,streams[15]+8)[0] if 15 in streams else None
        if pid in tested_browser_pids:
            crashes.append(dump.name)
    report["browser_crash_dumps"] = crashes
    report["checks"]["no_browser_crash_on_activation_or_exit"] = not crashes
    report["checks"]["plugin_template_immutable"] = (app/"lan_seats/lib/client.js").read_bytes()==template_before
    report["input_desktop_after"] = input_desktop()
    report["checks"]["user_desktop_unchanged"] = report["input_desktop_after"] == before
    report["passed"] = all(report["checks"].values())
    print(json.dumps({k:report[k] for k in ("browser_ready_seconds","assistant_ready_seconds","checks","passed")}, ensure_ascii=True), flush=True)
    assert report["passed"], "Acceptance checks failed; see product-test.json"
finally:
    (data / "product-test.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    # Only the disposable browser and services recorded in this test home.
    pids = [browser_pid]
    for name in (() if args.leave_services else (".dsh-web.json", ".lan-bridge.json", ".startup-services.json", "BrowserProfile/.env-check-service.json")):
        try:
            pids.append(int(read(name).get("pid") or 0))
        except (OSError, ValueError):
            pass
    for pid in set(pids):
        identity = process_identity(pid)
        if pid and identity.get("alive") and Path(identity.get("image","")).is_relative_to(app):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    user.CloseDesktop(desktop)
