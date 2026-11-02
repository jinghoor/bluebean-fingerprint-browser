"""Initialize product helpers after the browser starts, without owning its UI."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import traceback

from _runtime_state import atomic_json, startup_lock
from bluebean_paths import DATA_ROOT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    state = {"pid": os.getpid(), "status": "starting", "started_at": time.time()}
    def report(status, **fields):
        state.update(status=status, seconds=round(time.monotonic() - started, 3), **fields)
        atomic_json(DATA_ROOT / ".startup-services.json", state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
    try:
        with startup_lock(DATA_ROOT / ".product-startup.lock", timeout=2):
            report("starting")
            import _ensure_bridge
            import _ensure_relays
            import _dsh_web
            import env_cli
            with ThreadPoolExecutor(max_workers=2) as pool:
                relay = pool.submit(_ensure_relays.ensure_relays, False, args.profile)
                bridge = _ensure_bridge.ensure_bridge()
                if not bridge.get("ok"):
                    report("bridge-unavailable", error=bridge.get("detail"))
                    return 2
                report("bridge-ready", bridge_port=bridge["port"])
                workbench = _dsh_web.ensure()
                if not workbench.get("ok"):
                    report("workbench-unavailable", error=workbench.get("error"))
                    return 3
                report("workbench-ready")
                profile = env_cli.resolve_profile_dir(args.profile)
                reply = env_cli.send(profile, {"command": "overlay", "on": True,
                                              "url": workbench["url"]}, 35)
                if not reply.get("ok"):
                    report("attachment-unavailable", error=reply.get("error"))
                    return 4
                report("ready", relay_pending=not relay.done())
                marker = DATA_ROOT / ".first-run-runtime-ready-v1.json"
                if not marker.is_file():
                    atomic_json(marker, {"completed_at": time.time(), "runtime_version": 1})
                # Proxy recovery can finish independently of the first-use notice.
                relay.result()
                return 0
    except TimeoutError:
        print("Service initialization already in progress.", flush=True)
        return 0
    except Exception as exc:
        traceback.print_exc()
        report("failed", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
