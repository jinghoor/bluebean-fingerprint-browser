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

CONFIG = DATA_ROOT / ".lan-bridge.json"
LOG = DATA_ROOT / "logs" / "lan-bridge.log"
READY_TIMEOUT = 10.0


def load_config() -> tuple[int, str]:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(data.get("port", 0)), str(data.get("token", ""))
    except (OSError, ValueError, TypeError):
        return 0, ""


def probe(port: int, token: str, timeout: float = 0.7) -> dict:
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
                "detail": "ready" if owned else "different service instance"}
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


def ensure_bridge(verbose: bool = True) -> dict:
    with startup_lock(DATA_ROOT / ".bridge-startup.lock"):
        port, token = load_config()
        health = probe(port, token)
        if health["ok"]:
            return {"ok": True, "started": False, "port": port, "pid": health.get("pid")}
        if not token:
            token = secrets.token_urlsafe(32)
        # Mint the token BEFORE spawning. Pollers never retain an empty old token.
        # Port zero lets the OS allocate an endpoint without displacing a dev build.
        atomic_json(CONFIG, {"port": 0, "token": token})
        child = start_bridge(0)
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            port, current_token = load_config()
            health = probe(port, current_token)
            if health["ok"] and health.get("pid") == child.pid:
                if verbose:
                    print(f"  bridge ready: pid={child.pid}, port={port}", flush=True)
                return {"ok": True, "started": True, "port": port, "pid": child.pid}
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
