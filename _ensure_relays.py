"""Preflight: make sure every environment's relay is actually listening before launching.

Root cause of the operator's "You're offline": the three `fp_proxy.py serve` relays had died, so the
environments pointed at `socks5://127.0.0.1:<port>` where nothing listened. The browser then shows
"Connect to the internet" even though the machine is online.

`ensure_relays()` is called by the launcher and can also run standalone:

  python _ensure_relays.py
"""

from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
from _nowindow import console_python, no_window

from bluebean_paths import APP_ROOT as HERE, DATA_ROOT, PROFILE_DIR

PROFILE = PROFILE_DIR / "Default"
STATE = PROFILE / "envs.json"


def port_of(proxy: str) -> int | None:
    """Local port from `socks5://127.0.0.1:PORT` (only localhost proxies have a relay)."""
    if "127.0.0.1:" not in proxy:
        return None
    tail = proxy.rsplit(":", 1)[-1].strip("/")
    return int(tail) if tail.isdigit() else None


def listening(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def dead_envs(profile: pathlib.Path = PROFILE.parent) -> list[str]:
    state_file = profile / "Default" / "envs.json"
    if not state_file.exists():
        return []
    state = json.loads(state_file.read_text(encoding="utf-8"))
    dead = []
    for entry in state.get("envs", []):
        port = port_of(entry.get("proxy", ""))
        if port is None:
            continue
        if not listening(port):
            dead.append(entry.get("env_id", ""))
    return [env for env in dead if env]


def ensure_relays(verbose: bool = True, profile: pathlib.Path = PROFILE.parent) -> dict:
    """Start relays for environments whose local proxy port stopped listening."""
    dead = dead_envs(profile)
    if not dead:
        if verbose:
            print("  relays: all listening")
        return {"ok": True, "restarted": []}
    if verbose:
        print(f"  relays: dead for {dead} -> starting them")
    result = subprocess.run(
        [console_python(), str(HERE / "bind_env_proxies.py"), "--accounts", ",".join(dead),
         "--profile", str(profile), "--restore-only", "--no-restart"],
        cwd=str(HERE), capture_output=True, text=True, timeout=90,
        creationflags=no_window())
    tail = (result.stdout or "").strip().splitlines()
    if verbose:
        for line in tail[-6:]:
            print("   ", line)
        if result.returncode != 0:
            print("   ", (result.stderr or "")[-300:])
    return {"ok": result.returncode == 0, "restarted": dead,
            "output": result.stdout or "", "rc": result.returncode}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=== proxy preflight ===")
    result = ensure_relays()
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
