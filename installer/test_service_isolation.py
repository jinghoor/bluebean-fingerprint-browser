"""Exercise real bridge processes against separate disposable product homes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(sys.argv[1]).resolve()
BASE.mkdir(parents=True, exist_ok=True)
created = []
results = {}


def launch(home):
    env = dict(os.environ, BLUEBEAN_HOME=str(ROOT), BLUEBEAN_DATA=str(home),
               BLUEBEAN_PROFILE=str(home / "BrowserProfile"))
    result = subprocess.run([sys.executable, "-B", "-c",
        "import json,_ensure_bridge;print(json.dumps(_ensure_bridge.ensure_bridge(False)))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=35,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr[-1200:]
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["ok"], data
    return data


def check(port, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


try:
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(launch, [BASE / "A"] * 3))
    created.extend({r["pid"] for r in replies})
    assert len({r["pid"] for r in replies}) == 1
    assert sum(r["started"] for r in replies) == 1
    results["concurrent_single_instance"] = True
    a = json.loads((BASE / "A/.lan-bridge.json").read_text())
    b_reply = launch(BASE / "B")
    created.append(b_reply["pid"])
    b = json.loads((BASE / "B/.lan-bridge.json").read_text())
    assert a["port"] != b["port"] and a["token"] != b["token"]
    assert check(a["port"], a["token"]) == check(b["port"], b["token"]) == 200
    assert check(a["port"], b["token"]) == check(b["port"], a["token"]) == 401
    results["ports_and_auth_isolated"] = True
    # Simulate an upgrade retaining a stale endpoint belonging to another home.
    c_home = BASE / "C"
    c_home.mkdir()
    (c_home / ".lan-bridge.json").write_text(json.dumps({"port": a["port"], "token": "wrong-test-token"}))
    c_reply = launch(c_home)
    created.append(c_reply["pid"])
    assert c_reply["port"] not in (a["port"], b["port"])
    assert check(a["port"], a["token"]) == 200
    results["stale_401_recovers_without_touching_other_home"] = True
    results["seconds"] = round(time.monotonic() - started, 3)
    results["passed"] = True
    (BASE / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results), flush=True)
finally:
    # Only PIDs returned by these test-specific data homes are touched.
    for pid in created:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
