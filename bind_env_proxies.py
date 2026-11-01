"""Give each environment its own real exit: local relay + per-partition proxy.

Chrome cannot put credentials in a proxy URL (`--proxy-server=socks5://u:p@h:p`
fails, measured in PROXY.md), so an authenticated upstream needs a local relay in
front of it. This script does the whole chain for a set of environments:

  1. start (or reuse) one detached relay process per environment, each pointing at
     that environment's upstream port;
  2. verify the relay really reaches the expected exit address, and read the exit's
     timezone so the environment's fingerprint seed can be chosen to match it;
  3. write the environment's proxy / seed / expected exit into the profile's
     envs.json;
  4. restart the browser (a StoragePartition's NetworkContext is created once, so a
     proxy change only takes effect for a partition that is built after it), then
     open each environment in turn and read its exit address back from the page.

The relays are separate processes on purpose: the browser must not be the owner of
its own route, or quitting the browser (or this script) would kill the exit.

    python bind_env_proxies.py                            # fb-008, fb-012, fb-020
    python bind_env_proxies.py --accounts fb-025,fb-052
    python bind_env_proxies.py --no-restart                # only write config
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from _nowindow import console_python, detached_no_window, hidden_startup_info, no_window

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import env_seed                                              # noqa: E402
from _env_driver import EnvBrowser, focus_window, window_rect, windows_of_process  # noqa: E402
from fp_launcher import lookup_proxy_geo                     # noqa: E402
from fp_proxy import ProxyConfig                             # noqa: E402

from bluebean_paths import CHROME_EXE, PROFILE_DIR, PROXY_CONFIG as DEFAULT_PROXY_CONFIG, DATA_ROOT

CHROME = str(CHROME_EXE)
PROFILE = PROFILE_DIR
PROXY_CONFIG = DEFAULT_PROXY_CONFIG
GEO_CACHE = DATA_ROOT / ".geo-cache"
IP_URL = "https://ipinfo.io/ip"


def env_url(env_id: str) -> str:
    """Exit-address page for one environment, tagged so its tab is findable."""
    return f"{IP_URL}?e={env_id}"


# ---------------------------------------------------------------------------
# relays
# ---------------------------------------------------------------------------

def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=no_window())
    return str(pid) in (out.stdout or "")


def read_relay(port_file: Path) -> dict | None:
    if not port_file.exists():
        return None
    try:
        payload = json.loads(port_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not pid_alive(payload.get("pid", 0)):
        return None
    if not port_open(payload.get("port", 0)):
        return None
    return payload


def ensure_relay(account: str, relay_dir: Path) -> dict:
    port_file = relay_dir / f"{account}.json"
    existing = read_relay(port_file)
    if existing:
        print(f"  relay {account}: reusing {existing['proxy_url']} "
              f"(pid {existing['pid']})")
        return existing

    relay_dir.mkdir(parents=True, exist_ok=True)
    log_path = relay_dir / f"{account}.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    argv = [console_python(), str(HERE / "fp_proxy.py"),
            "--config", str(PROXY_CONFIG), "serve", "--account", account,
            "--port-file", str(port_file), "--ttl", "86400"]
    subprocess.Popen(argv, stdout=log_file, stderr=subprocess.STDOUT,
                     creationflags=detached_no_window(), startupinfo=hidden_startup_info(),
                     stdin=subprocess.DEVNULL)
    end = time.time() + 20
    while time.time() < end:
        payload = read_relay(port_file)
        if payload:
            print(f"  relay {account}: started {payload['proxy_url']} "
                  f"(pid {payload['pid']}, upstream {payload['upstream']})")
            return payload
        time.sleep(0.3)
    raise SystemExit(f"relay for {account} did not come up; see {log_path}")


def curl_exit_ip(proxy_url: str, timeout: int = 25) -> str:
    curl = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
               "System32" / "curl.exe")
    out = subprocess.run([curl, "-s", "--max-time", str(timeout),
                          "--proxy", proxy_url, IP_URL],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=no_window())
    body = (out.stdout or "").strip()
    return body if body.count(".") == 3 else ""


# ---------------------------------------------------------------------------
# env state
# ---------------------------------------------------------------------------

def envs_path() -> Path:
    return PROFILE / "Default" / "envs.json"


def load_envs() -> dict:
    path = envs_path()
    if not path.exists():
        return {"active": "", "envs": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_envs(state: dict) -> None:
    path = envs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def patch_env(state: dict, env_id: str, **fields) -> None:
    for env in state.get("envs", []):
        if env.get("env_id") == env_id:
            env.update(fields)
            return
    entry = {"env_id": env_id, "display_name": env_id}
    entry.update(fields)
    state.setdefault("envs", []).append(entry)


def pids_for_profile(profile: Path) -> list[int]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*--user-data-dir="
        + str(profile).replace("'", "''") + "*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=no_window())
    return [int(line) for line in (out.stdout or "").split() if line.isdigit()]


def running_browser_for(profile: Path) -> int | None:
    pids = pids_for_profile(profile)
    return pids[0] if pids else None


def stop_browser_for_profile(profile: Path) -> int:
    """Kill only the browser started with this user-data-dir; returns how many."""
    pids = pids_for_profile(profile)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=no_window())
    return len(pids)


def read_page_ip(browser: EnvBrowser, env_id: str,
                 timeout: float = 75.0) -> tuple[str, str]:
    """Activate-check one environment: returns (exit address, raw body text).

    The tab is navigated explicitly and the read retried: the first load in a
    fresh partition goes through a SOCKS relay with a metered upstream, and a 30s
    window was measured to be too short for whichever environment happened to be
    loaded during a burst (the failure moved between environments from run to run,
    which is the signature of a timeout rather than of broken wiring).
    """
    target = browser.target_by_url(f"e={env_id}", timeout=10)
    if target is None:
        target = browser.target_for_env(env_id)
    if target is None:
        # The mapping can still be missing right after the first activation (the
        # tab list is asynchronous). Ask for the visible tab -- the environment was
        # just activated, so it is the one on screen -- with a short wait.
        for _ in range(20):
            target = browser.target_for_env(env_id) or browser.visible_target(
                timeout=1.5)
            if target:
                break
            time.sleep(0.5)
    if target is None:
        return "", "no target"
    with browser.cdp_for(target) as cdp:
        cdp.send("Page.enable")
        cdp.send("Page.navigate", {"url": env_url(env_id)})
        end = time.time() + timeout
        body = ""
        while time.time() < end:
            text = cdp.evaluate(
                "document.body ? document.body.innerText.trim() : ''")
            if text:
                body = text
                if text.count(".") == 3 and "\n" not in text:
                    return text, text
            time.sleep(0.5)
        return "", body


def main() -> int:
    global PROFILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="fb-008,fb-012,fb-020")
    ap.add_argument("--profile", default=str(PROFILE))
    ap.add_argument("--chrome", default=CHROME)
    ap.add_argument("--no-restart", action="store_true",
                    help="write the config but leave the running browser alone")
    ap.add_argument("--restore-only", action="store_true",
                    help="restore relays without exit probes or changing environment identity/home pages")
    args = ap.parse_args()

    profile = Path(args.profile)
    PROFILE = profile
    relay_dir = profile / "relays"
    cfg = ProxyConfig.load(str(PROXY_CONFIG))
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]

    print("=== 1. relays ===")
    relays: dict[str, dict] = {}
    for account in accounts:
        spec = cfg.for_account(account)
        if spec.mode not in ("socks5", "http", "https"):
            print(f"  {account}: mode={spec.mode}, nothing to relay")
            continue
        relays[account] = ensure_relay(account, relay_dir)

    if args.restore_only:
        state = load_envs()
        for account, relay in relays.items():
            patch_env(state, account, proxy=relay["proxy_url"])
        save_envs(state)
        return 0

    print("\n=== 2. verify each relay reaches its own exit ===")
    facts: dict[str, dict] = {}
    for account, relay in relays.items():
        ip = curl_exit_ip(relay["proxy_url"])
        geo = lookup_proxy_geo(relay["proxy_url"], GEO_CACHE, force=True,
                               cache_key=relay["upstream"]) or {}
        tz = geo.get("timezone", "")
        seed = ""
        if tz:
            picked = env_seed.pick_seed(account, tz)
            seed = picked[0] if picked else ""
        facts[account] = {"proxy_url": relay["proxy_url"], "ip": ip,
                          "tz": tz, "seed": seed,
                          "upstream": relay["upstream"]}
        print(f"  {account}: {relay['proxy_url']} -> upstream {relay['upstream']}"
              f"  exit {ip or 'UNREACHABLE'}  tz {tz or '?'}")
        if seed:
            print(f"      seed {seed} (derives to {tz})")

    if len({f["ip"] for f in facts.values() if f["ip"]}) != len(
            [f for f in facts.values() if f["ip"]]):
        print("  NOTE: two relays report the same exit address; the upstream "
              "rotation may have landed on the same node twice.")

    print("\n=== 3. write envs.json ===")
    if not args.no_restart:
        killed = stop_browser_for_profile(profile)
        print(f"  stopped {killed} browser process(es) for this profile")
        # Wait for the profile lock to go away. Launching while the previous
        # instance is still shutting down makes Chrome hand the new process off to
        # the old one, which then exits immediately -- and every CDP read in step 4
        # silently talks to nothing.
        for _ in range(40):
            if running_browser_for(profile) is None:
                break
            time.sleep(0.5)
        print("  profile free:",
              running_browser_for(profile) is None)
    state = load_envs()
    for account, fact in facts.items():
        patch_env(state, account,
                  display_name=account,
                  proxy=fact["proxy_url"],
                  fingerprint_seed=fact["seed"],
                  expected_exit_ip=fact["ip"],
                  start_url=env_url(account))
        print(f"  {account}: proxy={fact['proxy_url']} seed={fact['seed']}")
    save_envs(state)
    print(f"  written: {envs_path()}")

    if args.no_restart:
        print("\nRESULT: config written; restart the browser to apply it "
              "(a partition's network context is created once).")
        return 0

    print("\n=== 4. restart and verify inside each environment ===")
    browser = EnvBrowser(args.chrome, user_data_dir=profile,
                         window="1400,900",
                         # A window that stays open must not write to the
                         # shared probe log; keep its log with the profile.
                         log_file=profile / "browser.log")
    if not browser.start(timeout=60):
        print("FAIL: browser did not start (is another instance holding the "
              "profile?)")
        return 2
    if running_browser_for(profile) not in (None, browser.chrome_pid):
        print("FAIL: a different browser instance owns this profile:",
              running_browser_for(profile))
        return 2
    print(f"  browser pid {browser.chrome_pid}")

    checks: list[tuple[str, bool, str]] = []
    for account, fact in facts.items():
        print(f"\n  activate {account}")
        result = browser.activate(account)
        print(f"    {result}")
        if fact["ip"]:
            seen, body = read_page_ip(browser, account)
            print(f"    page exit: {seen!r} (expected {fact['ip']})"
                  + ("" if seen else f"  body={body[:80]!r}"))
            checks.append((f"{account} exits through its own relay",
                           seen == fact["ip"], f"page={seen} "
                           f"expected={fact['ip']}"))
        else:
            checks.append((f"{account} relay reachable", False,
                           "relay did not produce an exit address"))

    browser.activate(accounts[0])
    time.sleep(1.0)
    print(f"\n  panel: {browser.cli('panel')}")
    time.sleep(1.5)
    for hwnd, title in windows_of_process(browser.chrome_pid):
        rect = window_rect(hwnd)
        if rect[2] > 400 and rect[3] > 300:
            focus_window(hwnd)
            print(f"  focused window: {title!r}")
            break

    # Put the plain exit page back into the environment definitions: the ?e= marker
    # exists for attribution during this run, not for the operator's address bar.
    # The next run adds it again before verifying.
    for account in facts:
        browser.cli("create", account, "--url", IP_URL)

    print("\n" + "=" * 74)
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"   [{detail}]" if not ok else ""))
    failed = [n for n, ok, _ in checks if not ok]
    print()
    if failed:
        print(f"RESULT: FAIL -- {failed}")
        return 1
    print("RESULT: PASS -- every environment leaves through its own relay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
