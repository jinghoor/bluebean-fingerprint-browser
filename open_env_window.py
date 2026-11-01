"""Open the built browser with the environment panel on screen.

One command for "show me the multi-environment window": start the built
chrome.exe against a dedicated profile, make sure the environments exist, switch to
one and bring the side panel up, then focus the window.

Idempotent: if a browser with this profile is already running it is reused, so a
second invocation just re-opens the panel.

    python open_env_window.py
    python open_env_window.py --profile E:\\chromium-envs\\env-ui --envs fb-008,fb-012
"""

from __future__ import annotations

import argparse
import json
import subprocess

from _nowindow import no_window
import sys
import pathlib
import secrets
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bluebean_paths import CHROME_EXE, PROFILE_DIR, DATA_ROOT  # noqa: E402
from _env_driver import (                                     # noqa: E402
    EnvBrowser, focus_window, free_port, window_rect, windows_of_process)

DEFAULT_CHROME = str(CHROME_EXE)
DEFAULT_PROFILE = str(PROFILE_DIR)

# Names shown in the panel. `--proxy system` means "follow the OS proxy", which
# is the only per-environment exit that works without a helper relay: Chrome
# cannot put credentials in a proxy URL, so an authenticated SOCKS5 upstream needs
# fp_proxy's local relay first (see PROXY.md).
DEFAULT_ENVS = [
    ("fb-008", "fb-008 运营号", "system"),
    ("fb-012", "fb-012 素材号", "system"),
    ("fb-020", "fb-020 测试号", "direct"),
]


def ensure_proxies_bound(profile: pathlib.Path) -> None:
    """Make sure every environment still points at its own relay before a window is shown."""
    import json as _json

    def unbound() -> list[str]:
        # The manager persists its definitions as Default/envs.json (measured: kStateFileName == "envs.json").
        state_file = profile / "Default" / "envs.json"
        for candidate in (state_file, profile / "envs.json"):
            if not candidate.exists():
                continue
            try:
                data = _json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            return [str(env.get("env_id")) for env in (data.get("envs") or [])
                    if str(env.get("proxy") or "").strip().lower() not in {"direct", "system"}
                    and not str(env.get("proxy") or "").startswith("socks5://127.0.0.1:")]
        return []

    bad = unbound()
    if not bad:
        return
    print(f"  environments without a relay proxy: {bad}; restoring relays")
    result = subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "bind_env_proxies.py"),
                    "--profile", str(profile), "--accounts", ",".join(bad),
                    "--restore-only", "--no-restart"],
                   capture_output=True, text=True, timeout=90,
                   creationflags=no_window(), cwd=str(pathlib.Path(__file__).parent))
    print(f"  relay restore rc={result.returncode}; still unbound: {unbound()}")


def running_browser_for(profile: Path) -> int | None:
    """PID of a chrome process started with this user-data-dir, if any."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        "Where-Object { $line = $_.CommandLine; $line -like '*--user-data-dir="
        + str(profile).replace("'", "''")
        + "*' -and $line -notlike '*--type=*' } "
        "| Select-Object -First 1 -ExpandProperty ProcessId"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         creationflags=no_window(),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    text = (out.stdout or "").strip()
    return int(text) if text.isdigit() else None


def recorded_cdp_port(pid: int) -> int | None:
    """Reuse the port recorded by this launch instead of waiting for port selection."""
    try:
        record = json.loads((DATA_ROOT / ".env-ui-port.json")
                            .read_text(encoding="utf-8"))
        if int(record.get("pid", -1)) == pid:
            port = int(record.get("port", 0))
            return port if 1 <= port <= 65535 else None
    except (OSError, ValueError, TypeError):
        pass
    return None



AUTOMATION_PORT_FILE = DATA_ROOT / ".lan-automation-port"
AUTOMATION_TOKEN_FILE = DATA_ROOT / ".lan-automation-token"
AUTOMATION_PORT = 8798


def automation_switches() -> list[str]:
    """Switches that turn on the in-browser automation socket (design doc §32/§40).

    Idempotent: the token is minted once and reused, so a restart keeps the same credential and
    the bridge does not have to be reconfigured.
    """
    token = ""
    if AUTOMATION_TOKEN_FILE.exists():
        token = AUTOMATION_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        token = secrets.token_hex(16)
        AUTOMATION_TOKEN_FILE.write_text(token, encoding="utf-8")
    AUTOMATION_PORT_FILE.write_text(str(AUTOMATION_PORT), encoding="utf-8")
    return [f"--lan-automation-port={AUTOMATION_PORT}",
            f"--lan-automation-token={token}"]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", default=DEFAULT_CHROME)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--windows", default="1400,900")
    ap.add_argument("--no-automation", action="store_true",
                    help="do not open the in-browser automation socket")
    ap.add_argument("--skip-envs", action="store_true",
                    help="do not create the default environments")
    args = ap.parse_args()

    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    # An environment whose relay died shows "You're offline" even though the machine is online,
    # because its partition proxies to 127.0.0.1:<dead port>. Check before launching.
    try:
        import _ensure_relays
        print("proxy preflight:")
        _ensure_relays.ensure_relays(profile=profile)
    except Exception as exc:                                       # noqa: BLE001
        print(f"  proxy preflight skipped: {type(exc).__name__}: {exc}")

    # The assistant float window talks to lan_bridge.py over files in this profile. Nothing else
    # starts that process, so without this check a reboot leaves the assistant waiting forever.
    try:
        import _ensure_bridge
        print("assistant preflight:")
        bridge_state = _ensure_bridge.ensure_bridge()
        if not bridge_state.get("ok"):
            print("  assistant: bridge is down; the assistant will report an error instead of hanging")
    except Exception as exc:                                       # noqa: BLE001
        print(f"  assistant preflight skipped: {type(exc).__name__}: {exc}")

    # Start the private workbench before Chrome so the first product window can
    # attach the real, current UI instead of briefly showing an empty placeholder.
    try:
        import _dsh_web
        dsh_state = _dsh_web.ensure()
        print(f"  workbench: {'ready' if dsh_state.get('ok') else 'unavailable'}")
    except Exception as exc:                                       # noqa: BLE001
        print(f"  workbench preflight skipped: {type(exc).__name__}: {exc}")

    existing = running_browser_for(profile)
    if existing:
        # The existing-browser path only needs the command client. Avoid pick_port(),
        # which waits for a port that the already-running browser necessarily owns.
        browser = EnvBrowser(args.chrome, user_data_dir=profile,
                             window=args.windows, cdp_port=recorded_cdp_port(existing) or 9333)
    else:
        browser = EnvBrowser(args.chrome, user_data_dir=profile,
                             window=args.windows,
                             extra_argv=([] if args.no_automation else automation_switches()) +
                                        ([] if args.no_automation else ["--lan-product-startup"]))
    ensure_proxies_bound(profile)
    if existing:
        print(f"browser already running for this profile: pid {existing}")
        print("  reusing it; only the panel request below is sent.")
        payload = browser.cli("panel")
        print("  panel:", payload)
        for hwnd, _title in windows_of_process(existing):
            focus_window(hwnd)
        return 0

    print(f"starting {args.chrome}")
    print(f"  profile: {profile}")
    if not browser.start(timeout=60):
        print("FAIL: the browser did not expose a debugging port in time")
        return 2
    print(f"  pid: {browser.chrome_pid}")

    if not args.skip_envs:
        state = browser.state()
        known = {e.get("env_id") for e in state.get("envs", [])}
        for env_id, display, proxy in DEFAULT_ENVS:
            if env_id in known:
                print(f"  env {env_id}: already defined")
                continue
            result = browser.cli("create", env_id, "--display-name", display,
                                 "--proxy", proxy)
            print(f"  env {env_id}: {result}")

    state = browser.state()
    first = state.get("active") or (DEFAULT_ENVS[0][0] if not args.skip_envs else None)
    if first and state.get("active") != first:
        print(f"  activate {first}: {browser.activate(first)}")
    print("  panel:", browser.cli("panel"))

    # Bring it to the front so the operator sees it without hunting for it.
    for hwnd, title in windows_of_process(browser.chrome_pid):
        rect = window_rect(hwnd)
        if rect[2] > 400 and rect[3] > 300:
            focus_window(hwnd)
            print(f"  focused window: {title!r} rect={rect}")
            break

    state = browser.state()
    print("\nenvironments:")
    for env in state.get("envs", []):
        mark = "*" if env.get("env_id") == state.get("active") else " "
        print(f"  {mark} {env.get('env_id'):<10} proxy={env.get('proxy') or '-':<8} "
              f"partition={env.get('partition')}")
    print(f"\nwindow left running (pid {browser.chrome_pid}). "
          "Click a row in the side panel to switch, or use env_cli.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
