"""Launch and supervise the DSH web workbench for the product's own home.

Why this exists: the workbench is `dsh web` (profile `web`) served from a product-private DSH home
(`.dsh-home`), never the operator's `~/.dsh`. Two measured details drive the implementation:

  * the URL is printed with a one-time `?token=`; the server answers 200 and sets a `dsh-auth-*` cookie,
    then rejects token-less requests with 401. A probe that ignores cookies reports 401 even when the
    server is healthy, so `probe()` keeps a cookie jar.
  * the model key comes from `.deepseek-key` and is injected into the child environment. Verified with
    `dsh headless "Reply with exactly OK"` -> `OK` in 2.6 s against this same home.

    python _dsh_web.py --check      # is it up? prints url/port/pid
    python _dsh_web.py --start      # start if needed, then print url
    python _dsh_web.py --stop
    python _dsh_web.py --restart
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _nowindow import detached_no_window, hidden_startup_info, no_window  # noqa: E402
from bluebean_paths import APP_ROOT, DATA_ROOT, DSH_BIN, DSH_EXE  # noqa: E402
from bluebean_version import VERSION  # noqa: E402
from _runtime_state import atomic_json, atomic_text, process_identity, startup_lock

EXE = str(DSH_EXE)
BIN = str(DSH_BIN)
HOME = DATA_ROOT / "DSH"
STATE = DATA_ROOT / ".dsh-web.json"
LOG = DATA_ROOT / "logs" / "dsh-web.log"
URL_PATTERN = re.compile(r"dsh web:\s+(http://[0-9.]+:\d+/\?token=[A-Za-z0-9_\-]+)")


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    atomic_json(STATE, state)


def process_alive(pid: int) -> bool:
    """Cheap liveness check that does not touch the network."""
    if not pid:
        return False
    return bool(process_identity(pid).get("alive"))


def probe(url: str, timeout: float = 1.5) -> tuple[bool, str]:
    """GET the workbench the way a browser does: token first, then the auth cookie it sets."""
    if not url:
        return False, "no url"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(url, timeout=timeout) as response:
            body = response.read(2000).decode("utf-8", "replace")
            if response.status != 200:
                return False, f"HTTP {response.status}"
            if "<html" not in body.lower():
                return False, "not an app shell"
            return True, f"HTTP 200, cookies={len(list(jar))}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:                                    # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def api_key() -> str:
    key_file = HOME / ".deepseek-key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def plugin_signature() -> str:
    digest = hashlib.sha256()
    for relative in ("lan_plugin/index.js", "lan_seats/lib/client.js"):
        digest.update((APP_ROOT / relative).read_bytes())
    return digest.hexdigest()


def bridge_signature() -> str:
    path = DATA_ROOT / ".lan-bridge.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def owns_state(state: dict) -> bool:
    identity = process_identity(int(state.get("pid") or 0))
    return bool(identity.get("alive") and
                state.get("home") == str(HOME) and state.get("app_root") == str(APP_ROOT.resolve()) and
                state.get("process_started") == identity.get("started") and
                pathlib.Path(identity.get("image", "")).resolve() == DSH_EXE.resolve())


def sync_seat_plugin() -> None:
    """Copy the seat plugin's client half into the profile install with live bridge credentials.

    pnpm may hardlink every package file, including client.js, to the bundled
    template. Replace the profile's leaf atomically so per-home credentials
    never mutate the installation or another home's plugin.
    """
    source = APP_ROOT / "lan_seats" / "lib" / "client.js"
    installed = HOME / "profiles" / "web" / "node_modules" / "lan-seats" / "lib" / "client.js"
    config_file = DATA_ROOT / ".lan-bridge.json"
    if not (source.exists() and installed.parent.exists() and config_file.exists()):
        return
    config = json.loads(config_file.read_text(encoding="utf-8"))
    text = source.read_text(encoding="utf-8")
    text = re.sub(r"const BLUEBEAN_VERSION = '[^']*';",
                  "const BLUEBEAN_VERSION = '" + VERSION + "';", text)
    text = re.sub(r"const BASE = '[^']*';",
                  "const BASE = 'http://127.0.0.1:" + str(config["port"]) + "';", text)
    text = re.sub(r"const TOKEN = '[^']*';",
                  "const TOKEN = '" + str(config["token"]) + "';", text)
    atomic_text(installed, text)


def start(timeout: float = 120.0) -> dict:
    prepare_profile()
    sync_seat_plugin()
    HOME.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = LOG.open("ab")
    # Poll only this launch's log output. A PIPE/readline can block past the
    # deadline and breaks when the short-lived desktop launcher exits.
    offset = log.tell()

    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["DSH_HOME"] = str(HOME)
    bridge_config = DATA_ROOT / ".lan-bridge.json"
    if bridge_config.exists():
        try:
            bridge = json.loads(bridge_config.read_text(encoding="utf-8"))
            env["LAN_BRIDGE_TOKEN"] = str(bridge.get("token") or "")
            env["LAN_BRIDGE_URL"] = f"http://127.0.0.1:{int(bridge.get('port') or 8799)}"
        except (OSError, ValueError, TypeError):
            pass
    key = api_key()
    if key:
        env["DEEPSEEK_API_KEY"] = key

    process = subprocess.Popen(
        [EXE, BIN, "web", "--port", "0", "--no-open"],
        env=env, cwd=str(HERE), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        creationflags=detached_no_window(), startupinfo=hidden_startup_info())
    log.close()

    url = ""
    deadline = time.monotonic() + timeout
    pending = ""
    with LOG.open("rb") as output:
        output.seek(offset)
        while time.monotonic() < deadline and not url:
            pending += output.read().decode("utf-8", "replace")
            match = URL_PATTERN.search(pending)
            if match:
                url = match.group(1)
                break
            pending = pending[-8192:]
            if process.poll() is not None:
                break
            time.sleep(0.05)

    if not url:
        process.terminate()
        return {"ok": False, "error": "no URL printed by dsh web", "log": str(LOG)}

    ok, detail = probe(url)
    if not ok:
        process.terminate()
        return {"ok": False, "error": f"probe failed: {detail}", "url": url}

    state = {"url": url, "pid": process.pid, "port": int(url.split(":")[2].split("/")[0]),
             "home": str(HOME), "started_at": int(time.time()), "probe": detail,
             "app_root": str(APP_ROOT.resolve()),
             "process_started": process_identity(process.pid).get("started"),
             "plugin_signature": plugin_signature(), "bridge_signature": bridge_signature()}
    save_state(state)
    return {"ok": True, **state}


def prepare_profile() -> None:
    """Initialize a clean DSH web profile and install only bundled product plugins."""
    target = HOME / "profiles" / "web"
    marker = target / ".bluebean-plugins-ready-v2"
    signature = plugin_signature()
    installed = all((target / "node_modules" / name / "package.json").is_file()
                    for name in ("lan-browser", "lan-seats"))
    if installed and marker.is_file() and marker.read_text(encoding="utf-8").strip() == signature:
        return
    if installed:
        # Existing profile: update our client in place, preserve user's plugins/config.
        sync_seat_plugin()
        marker.write_text(signature, encoding="utf-8")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["DSH_HOME"] = str(HOME)
    # DSH's plugin manager invokes pnpm by name. Both the shim and Node must
    # come from the installation rather than a developer machine's PATH.
    private_tools = APP_ROOT / "tools"
    private_node = APP_ROOT / "dsh" / "resources" / "runtime" / "primary-runtime" / "dependencies" / "node" / "bin"
    if (private_tools / "pnpm.cmd").is_file():
        env["PATH"] = os.pathsep.join((str(private_tools), str(private_node), env.get("PATH", "")))
    env["npm_config_manage_package_manager_versions"] = "false"
    for plugin in ("lan_plugin", "lan_seats"):
        plugin_dir = APP_ROOT / plugin
        result = subprocess.run(
            [EXE, BIN, "plugin", "--profile", "web", "add", f"file:{plugin_dir.as_posix()}"],
            env=env, cwd=str(APP_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
            creationflags=no_window(), startupinfo=hidden_startup_info())
        if result.returncode:
            raise RuntimeError(f"Could not install bundled DSH plugin {plugin}: "
                               f"{(result.stderr or result.stdout)[-1200:]}")
    patch = target / "cordis.patch.yml"
    patch.write_text(
        "- insert:\n"
        "    - id: lan-browser\n"
        "      name: lan-browser\n"
        "    - id: lan-seats\n"
        "      name: lan-seats\n",
        encoding="utf-8")
    marker.write_text(signature, encoding="utf-8")


def stop() -> dict:
    state = load_state()
    pid = int(state.get("pid") or 0)
    if pid and owns_state(state):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, creationflags=no_window())
        time.sleep(0.8)
        STATE.unlink(missing_ok=True)
        return {"ok": True, "stopped": pid}
    return {"ok": True, "stopped": 0, "foreign_state_ignored": bool(pid)}


def ensure() -> dict:
    """Reuse a healthy instance, otherwise start one."""
    with startup_lock(DATA_ROOT / ".dsh-startup.lock", timeout=180):
        state = load_state()
        url = str(state.get("url") or "")
        owned = owns_state(state)
        compatible = (state.get("plugin_signature") == plugin_signature() and
                      state.get("bridge_signature") == bridge_signature())
        if owned and compatible and url:
            ok, detail = probe(url)
            if ok:
                return {"ok": True, "reused": True, **state, "probe": detail}
        if owned:
            stop()
        return start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.stop:
        print(json.dumps(stop(), ensure_ascii=False))
        return 0
    if args.restart:
        stop()
        print(json.dumps(start(), ensure_ascii=False))
        return 0
    if args.check:
        state = load_state()
        ok, detail = probe(str(state.get("url") or ""))
        print(json.dumps({**state, "healthy": ok, "probe": detail}, ensure_ascii=False))
        return 0 if ok else 2
    print(json.dumps(ensure(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
