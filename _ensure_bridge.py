"""Preflight for the assistant backend: make sure `lan_bridge.py` is up before the browser is.

Why this exists: the float window writes `chat-request.json` and waits for `chat-response.json`, and
nothing else in the tree starts the bridge. Measured before this file existed: the bridge was an
orphaned process with no launcher, so after a reboot the assistant sat on "思考中…" forever with no
error anywhere.

`ensure_bridge()` is idempotent: it probes `/v1/health` first and only spawns when the probe fails.
Can also run standalone:

  python _ensure_bridge.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess

from _nowindow import console_python, detached_no_window, hidden_startup_info, no_window
import sys
import time
import urllib.error
import urllib.request

from bluebean_paths import APP_ROOT as HERE, DATA_ROOT

CONFIG = DATA_ROOT / ".lan-bridge.json"
LOG = DATA_ROOT / "logs" / "lan-bridge.log"
READY_TIMEOUT = 20.0


def load_config() -> tuple[int, str]:
    """Port and token, creating the config exactly the way lan_bridge.load_token() does."""
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            return int(data.get("port", 8799)), str(data.get("token", ""))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return 8799, ""


def probe(port: int, token: str, timeout: float = 2.0) -> dict:
    """GET /v1/health. Returns {'ok': bool, 'detail': str}."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            payload = json.loads(reply.read().decode("utf-8", "replace") or "{}")
        if payload.get("ok"):
            return {"ok": True, "detail": f"backend={payload.get('backend')} "
                                          f"protocol={payload.get('protocol')}"}
        return {"ok": False, "detail": f"health said: {payload}"}
    except urllib.error.HTTPError as error:
        return {"ok": False, "detail": f"HTTP {error.code}"}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        return {"ok": False, "detail": f"{type(error).__name__}: {error}"}


def bridge_pids() -> list[int]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe' OR name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*lan_bridge.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         creationflags=no_window(),
                            capture_output=True, text=True)
    return [int(line) for line in (result.stdout or "").split() if line.isdigit()]


def start_bridge(port: int) -> int | None:
    """Spawn the bridge detached so it survives this launcher. Returns its pid."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = LOG.open("ab")
    # A hidden console, not DETACHED_PROCESS: with no console at all, every console child the bridge
    # starts gets its own window (measured: one flash per browser action). The hidden console is
    # inherited by those children, so they never display anything.
    process = subprocess.Popen(
        [console_python(), str(HERE / "lan_bridge.py"), "--port", str(port)],
        cwd=str(HERE), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        creationflags=detached_no_window(), startupinfo=hidden_startup_info())
    log.close()
    return process.pid


def ensure_bridge(verbose: bool = True) -> dict:
    """Start the assistant backend when it is not answering. Idempotent."""
    port, token = load_config()
    health = probe(port, token)
    if health["ok"]:
        if verbose:
            print(f"  bridge: answering on {port} ({health['detail']})")
        return {"ok": True, "started": False, "port": port, "pids": bridge_pids()}

    stale = bridge_pids()
    if verbose:
        print(f"  bridge: not answering on {port} ({health['detail']})")
        if stale:
            print(f"  bridge: process(es) {stale} exist but do not serve the API")
    pid = start_bridge(port)
    if verbose:
        print(f"  bridge: started pid {pid}, waiting for /v1/health")

    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        health = probe(port, token)
        if health["ok"]:
            if verbose:
                print(f"  bridge: ready ({health['detail']})")
            return {"ok": True, "started": True, "port": port, "pid": pid,
                    "pids": bridge_pids()}
        time.sleep(0.25)

    if verbose:
        print(f"  bridge: FAILED to answer within {READY_TIMEOUT:.0f}s; see {LOG}")
        tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-6:] \
            if LOG.exists() else []
        for line in tail:
            print("   ", line)
    return {"ok": False, "started": True, "port": port, "pid": pid,
            "log": str(LOG), "detail": health.get("detail", "")}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=== assistant bridge preflight ===")
    result = ensure_bridge()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
