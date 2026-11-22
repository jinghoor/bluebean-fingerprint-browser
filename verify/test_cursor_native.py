"""Exercise the native Agent cursor through the real browser automation socket.

The browser is created on a disposable private Windows desktop.  The assertion is
deliberately compositor-side: a cursor request must be accepted by the native
overlay and produce an ENVCURSOR trace without changing the page or requiring a
renderer script.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import env_cli

OUT = ROOT / ".work" / "cursor-native" / ("native-" + time.strftime("%Y%m%d-%H%M%S"))
PROFILE = OUT / "Profile"
(PROFILE / "Default").mkdir(parents=True)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<html><body><h1>DSH fixture</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
workbench_url = f"http://127.0.0.1:{server.server_port}/"
(OUT / ".dsh-web.json").write_text(json.dumps({"url": workbench_url}), encoding="utf-8")
(PROFILE / "Default" / "envs.json").write_text(
    json.dumps({"active": "cursor-env", "envs": [{
        "env_id": "cursor-env", "display_name": "Cursor fixture",
        "proxy": "direct", "start_url": workbench_url,
        "homepage": workbench_url,
    }]}), encoding="utf-8")


class SI(c.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
        ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
        ("lpReserved2", c.c_void_p), ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE),
    ]


class PI(c.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


user = c.WinDLL("user32", use_last_error=True)
kernel = c.WinDLL("kernel32", use_last_error=True)
user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p]
user.CreateDesktopW.restype = w.HANDLE
user.CloseDesktop.argtypes = [w.HANDLE]
kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL,
                                  w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(SI), c.POINTER(PI)]
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


chrome = Path(os.environ.get("BLUEBEAN_TEST_CHROME", r"E:\chromium\src\out\Release-pgo\chrome.exe"))
cdp_port = free_port()
automation_port = free_port()
automation_token = "cursor-test-" + uuid.uuid4().hex
desktop_name = "BlueBeanCursorTest_" + uuid.uuid4().hex[:10]
desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x1FF, None)
assert desktop, c.WinError(c.get_last_error())
argv = [str(chrome), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={cdp_port}",
        f"--lan-automation-port={automation_port}", f"--lan-automation-token={automation_token}",
        "--no-first-run", "--no-default-browser-check", "--window-size=1100,760",
        "--disable-background-networking", "--disable-component-update",
        "--disable-features=CalculateNativeWinOcclusion", "--enable-logging",
        f"--log-file={OUT / 'chrome.log'}", "about:blank"]
environment = dict(os.environ, LAN_PRODUCT_HOME=str(OUT), BLUEBEAN_DATA=str(OUT))
block = c.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0\0")
si, pi = SI(), PI()
si.cb = c.sizeof(si)
si.lpDesktop = "WinSta0\\" + desktop_name


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.08)
    raise TimeoutError(last)


def command(payload):
    reply = env_cli.send(PROFILE / "Default", payload, 8)
    assert reply.get("ok"), reply
    return reply


def send_cursor(action, x, y):
    payload = json.dumps({"token": automation_token, "env": "cursor-env", "ops": [],
                          "cursor": {"x": x, "y": y, "action": action}}).encode() + b"\n"
    with socket.create_connection(("127.0.0.1", automation_port), timeout=4) as conn:
        conn.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


report = {"passed": False, "checks": [], "scope": "private desktop, disposable profile"}
try:
    assert kernel.CreateProcessW(
        str(chrome), c.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None,
        False, 0x400 | 0x200, block, str(chrome.parent), c.byref(si), c.byref(pi)
    ), c.WinError(c.get_last_error())
    kernel.CloseHandle(pi.hThread)
    wait(lambda: (PROFILE / "Default" / "envs.json").exists())
    command({"command": "newtab", "env_id": "cursor-env", "url": workbench_url})
    command({"command": "panel"})
    wait(lambda: "layer=1" in command({"command": "overlay", "query": True}).get("geometry", ""))
    report["checks"].append("native_overlay_mounted")
    first_motion = send_cursor("move", 120, 120)
    long_motion = send_cursor("move", 820, 520)
    assert first_motion.get("cursor_ms", 0) >= 100, first_motion
    assert long_motion.get("cursor_ms", 0) >= 170, long_motion
    report["checks"].append("distance_based_accelerated_arc_duration")
    for action, x, y in (("move", 180, 220), ("click", 260, 340), ("type", 330, 410), ("scroll", 420, 500)):
        reply = send_cursor(action, x, y)
        assert reply.get("ok"), reply
        assert reply.get("cursor_ms", 0) >= 80, reply
    report["checks"].append("move_click_type_scroll_accepted_by_native_socket")
    log = (OUT / "chrome.log").read_text(encoding="utf-8", errors="replace")
    for action in ("move", "click", "type", "scroll"):
        assert f"ENVCURSOR: env=cursor-env action={action}" in log, action
    report["checks"].append("native_compositor_traces_all_actions")
    report["passed"] = True
finally:
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(OUT / "report.json"), **report}, ensure_ascii=True), flush=True)
    if pi.hProcess:
        kernel.TerminateProcess(pi.hProcess, 0)
        kernel.WaitForSingleObject(pi.hProcess, 10000)
        kernel.CloseHandle(pi.hProcess)
    server.shutdown()
    server.server_close()
    user.CloseDesktop(desktop)
