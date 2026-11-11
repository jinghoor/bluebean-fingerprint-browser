"""Start one authenticated bridge per product data directory, on its own port."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request

from _nowindow import console_python, detached_no_window, hidden_startup_info
from _runtime_state import atomic_json, process_identity, startup_lock
from bluebean_paths import APP_ROOT as HERE, DATA_ROOT, PROFILE_DIR
from bridge_runtime import source_revision

CONFIG = DATA_ROOT / ".lan-bridge.json"
LOG = DATA_ROOT / "logs" / "lan-bridge.log"
READY_TIMEOUT = 10.0


def load_config() -> tuple[int, str]:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(data.get("port", 0)), str(data.get("token", ""))
    except (OSError, ValueError, TypeError):
        return 0, ""


def probe(port: int, token: str, timeout: float = 0.7, expected_revision: str | None = None) -> dict:
    if not port or not token:
        return {"ok": False, "detail": "not initialized"}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as reply:
            payload = json.loads(reply.read())
        owned = (payload.get("ok") and payload.get("data_root") == str(DATA_ROOT.resolve())
                 and payload.get("app_root") == str(HERE.resolve())
                 and payload.get("profile_root") == str(PROFILE_DIR.resolve()))
        return {"ok": bool(owned), "pid": payload.get("pid"),
                "current": expected_revision is None or payload.get('runtime_revision') == expected_revision,
                "runtime_revision": payload.get('runtime_revision'),
                "busy": any(payload.get(key,True) for key in
                    ('environment_agent_busy','environment_import_busy','browser_busy','chat_busy')),
                "detail": ('ready' if expected_revision is None or payload.get('runtime_revision')==expected_revision
                           else 'bridge source changed; runtime reload required') if owned else "different service instance"}
    except urllib.error.HTTPError as error:
        return {"ok": False, "detail": f"HTTP {error.code}"}
    except (OSError, ValueError) as error:
        return {"ok": False, "detail": type(error).__name__}


def bridge_pids() -> list[int]:
    try:
        state = json.loads(CONFIG.read_text(encoding="utf-8"))
        pid = int(state.get("pid") or 0)
        return [pid] if pid and process_identity(pid).get("alive") else []
    except (OSError, ValueError):
        return []


def start_bridge(port: int = 0) -> subprocess.Popen:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as log:
        return subprocess.Popen(
            [console_python(), "-B", str(HERE / "lan_bridge.py"), "--port", str(port)],
            cwd=str(HERE), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=detached_no_window(), startupinfo=hidden_startup_info())


def stop_idle_owned_bridge(health: dict, port: int, token: str, revision: str) -> bool:
    """Stop only the authenticated, idle Python service belonging to this home."""
    if not health.get('ok') or health.get('busy'):
        return False
    pid=int(health.get('pid') or 0)
    try:
        state=json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return False
    identity=process_identity(pid)
    if state.get('pid') != pid or not identity.get('alive') or not identity.get('started'):
        return False
    if os.path.normcase(str(identity.get('image',''))) != os.path.normcase(str(console_python())):
        return False
    confirmed=probe(port,token,expected_revision=revision)
    if not confirmed.get('ok') or confirmed.get('pid') != pid or confirmed.get('busy'):
        return False
    if confirmed.get('current'):
        return False
    # Hold a process handle and verify its creation time to prevent PID reuse.
    if os.name != 'nt':
        return False
    import ctypes as c
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[c.c_uint32,c.c_int,c.c_uint32]
    kernel.OpenProcess.restype=c.c_void_p
    kernel.CloseHandle.argtypes=[c.c_void_p]
    kernel.GetProcessTimes.argtypes=[c.c_void_p]+[c.POINTER(c.c_uint64)]*4
    kernel.TerminateProcess.argtypes=[c.c_void_p,c.c_uint32]
    kernel.WaitForSingleObject.argtypes=[c.c_void_p,c.c_uint32]
    handle=kernel.OpenProcess(0x101001,False,pid)
    if not handle:
        return False
    try:
        times=[c.c_uint64() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*(c.byref(v) for v in times)) or times[0].value != identity['started']:
            return False
        if not kernel.TerminateProcess(handle,0):
            return False
        return kernel.WaitForSingleObject(handle,5000)==0
    finally:
        kernel.CloseHandle(handle)


def ensure_bridge(verbose: bool = True) -> dict:
    with startup_lock(DATA_ROOT / ".bridge-startup.lock"):
        revision=source_revision(HERE)
        port, token = load_config()
        health = probe(port, token,expected_revision=revision)
        replaced=False
        if health["ok"]:
            if health.get('current'):
                return {"ok": True, "started": False, "port": port, "pid": health.get("pid"),"runtime_current":True}
            if not stop_idle_owned_bridge(health,port,token,revision):
                return {"ok":True,"started":False,"port":port,"pid":health.get('pid'),
                        "runtime_current":False,"reload_pending":True,
                        "detail":"bridge source changed; reload deferred while busy or ownership is unconfirmed"}
            replaced=True
        if not token:
            token = secrets.token_urlsafe(32)
        # Mint the token BEFORE spawning. Pollers never retain an empty old token.
        # Port zero lets the OS allocate an endpoint without displacing a dev build.
        atomic_json(CONFIG, {"port": 0, "token": token})
        # Keep the endpoint stable during an idle source upgrade so open clients
        # do not retain a dead cached URL. New installations still use port zero.
        child = start_bridge(port if replaced else 0)
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            port, current_token = load_config()
            health = probe(port, current_token,expected_revision=revision)
            if health["ok"] and health.get("pid") == child.pid and health.get('current'):
                if verbose:
                    print(f"  bridge ready: pid={child.pid}, port={port}", flush=True)
                return {"ok": True, "started": True, "port": port, "pid": child.pid,
                        "runtime_current":True,"reloaded":replaced}
            if child.poll() is not None:
                break
            time.sleep(0.05)
        if child.poll() is None:
            child.terminate()  # Only this unsuccessful launch, never a foreign process.
        result = {"ok": False, "started": True, "port": port,
                  "pid": child.pid, "log": str(LOG), "detail": health["detail"]}
        if verbose:
            print(f"  bridge unavailable: {result['detail']}; see {LOG}", flush=True)
        return result


if __name__ == "__main__":
    result = ensure_bridge()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 2)
