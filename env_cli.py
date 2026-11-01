"""命令行控制多环境侧边栏。

Chrome 里的 EnvManager 每 250ms 读一次 `<profile>/env-command.json`，执行后把
结果写进 `<profile>/env-result.json`。这个工具就是那个通道的客户端。

为什么用文件而不是 socket：切换环境是人的动作，250ms 的轮询延迟不可感知；而为了
一个低频命令去开一个端口、处理连接和鉴权，代码量和攻击面都不划算。等 M3 做侧边栏
UI 时，UI 和 EnvManager 在同一个进程里，可以直接调 C++，这个通道就只留给脚本用。

用法：
    python env_cli.py --profile <user-data-dir> list
    python env_cli.py --profile <dir> activate fb-012
    python env_cli.py --profile <dir> activate fb-012 --proxy socks5://1.2.3.4:1080
    python env_cli.py --profile <dir> activate fb-012 --url https://example.com
    python env_cli.py --profile <dir> newtab fb-012 --url https://example.com
    python env_cli.py --profile <dir> create fb-020 --proxy direct --exit-ip 1.2.3.4
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

COMMAND_FILE = "env-command.json"
RESULT_FILE = "env-result.json"
STATE_FILE = "envs.json"


def resolve_profile_dir(user_data_dir: Path) -> Path:
    """EnvManager 用的是 Profile::GetPath()，即 <user-data-dir>/Default。

    实测：传 --user-data-dir=X 时状态文件出现在 X/Default 下，而 CLI 一开始写在
    X 根目录，于是永远等不到回应。这里做一次探测，两种传法都能工作。
    """
    default = user_data_dir / "Default"
    if default.is_dir():
        return default
    return user_data_dir


def send(profile_dir: Path, command: dict, timeout: float = 15.0) -> dict:
    """Serialize all producers of the single-slot native command channel.

    The OS releases this byte lock if a client crashes. One deadline includes
    queueing and response time; commands are never replayed after dispatch.
    """
    deadline = time.monotonic() + timeout
    lock_path = profile_dir / "env-command.lock"
    with lock_path.open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"\0")
            lock.flush()
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    return {"ok": False, "error": "browser command channel busy", "dispatched": False}
                time.sleep(0.02)
        try:
            if time.monotonic() >= deadline:
                return {"ok": False, "error": "browser command deadline exceeded", "dispatched": False}
            return _send_locked(profile_dir, command, deadline, timeout)
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _send_locked(profile_dir: Path, command: dict, deadline: float, timeout: float) -> dict:
    cmd_path = profile_dir / COMMAND_FILE
    result_path = profile_dir / RESULT_FILE

    # 清掉上一次的结果，否则会立刻读到旧值。
    try:
        result_path.unlink()
    except OSError:
        pass

    # A per-request id keeps this reply from being confused with another producer's (the bridge polls
    # `list` every second on the same channel).
    request_id = secrets.token_hex(8)
    command = dict(command, request_id=request_id)
    tmp = cmd_path.with_name(f"{cmd_path.name}.{request_id}.tmp")
    payload = json.dumps(command, ensure_ascii=False)
    tmp.write_text(payload, encoding="utf-8")
    # The browser reads this file from its poll timer, and Windows refuses to replace a file that is open. Retry
    # briefly, staying within the caller's deadline. Never write partial JSON
    # in place: the browser can consume/delete it while it is being written.
    for attempt in range(12):
        if time.monotonic() >= deadline:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "browser command deadline exceeded before dispatch", "dispatched": False}
        try:
            os.replace(tmp, cmd_path)
            break
        except PermissionError:
            time.sleep(min(0.05 + attempt * 0.02, max(0, deadline - time.monotonic())))
    else:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": "cannot atomically dispatch browser command", "dispatched": False}

    while time.monotonic() < deadline:
        if result_path.exists():
            try:
                raw = result_path.read_text(encoding="utf-8")
                payload = json.loads(raw)
                owner = payload.get("request_id")
                if owner != request_id:
                    time.sleep(0.02)   # another producer's reply; keep waiting for ours
                    continue
            except (json.JSONDecodeError, OSError):
                time.sleep(0.05)
                continue
            try:
                result_path.unlink()
            except OSError:
                pass
            return payload
        time.sleep(0.02)

    return {
        "ok": False,
        "dispatched": True,
        "error": (
            f"no response within {timeout}s -- is Chrome running with this "
            f"profile dir? looked for {result_path}"
        ),
    }


def emit(args, payload: dict, human: str) -> int:
    """Print JSON for scripts, the human line for a person."""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(human)
    return 0 if payload.get("ok", True) else 1


def cmd_list(args) -> int:
    payload = send(resolve_profile_dir(Path(args.profile)),
                   {"command": "list"}, args.timeout)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload.get("envs") is not None else 2
    if not payload.get("envs"):
        if payload.get("ok") is False:
            print(f"FAIL: {payload.get('error')}")
            return 2
        print("(no environments defined yet)")
        return 0
    active = payload.get("active") or ""
    print(f"{'':<2}{'env_id':<14}{'partition':<24}{'proxy':<28}detached")
    print("-" * 76)
    for e in payload["envs"]:
        mark = "*" if e.get("env_id") == active else " "
        print(f"{mark:<2}{e['env_id']:<14}{e['partition']:<24}"
              f"{(e.get('proxy') or '-'):<28}{e.get('detached_tabs', 0)}")
    if active:
        print(f"\nactive: {active}")
    return 0


def cmd_activate(args) -> int:
    command = {"command": "activate", "env_id": args.env_id}
    if args.proxy:
        command["proxy"] = args.proxy
    if args.exit_ip:
        command["expected_exit_ip"] = args.exit_ip
    if args.display_name:
        command["display_name"] = args.display_name
    if getattr(args, "homepage", ""):
        command["homepage"] = args.homepage
    if args.url:
        command["start_url"] = args.url
    payload = send(resolve_profile_dir(Path(args.profile)), command,
                   args.timeout)
    if payload.get("ok"):
        payload.setdefault("active", args.env_id)
        return emit(args, payload, f"activated: {payload.get('active')}")
    return emit(args, payload, f"FAIL: {payload.get('error')}")


def cmd_newtab(args) -> int:
    command = {"command": "newtab", "env_id": args.env_id}
    if args.url:
        command["url"] = args.url
    payload = send(resolve_profile_dir(Path(args.profile)), command,
                   args.timeout)
    if payload.get("ok"):
        return emit(args, payload, f"opened tab in {args.env_id}")
    return emit(args, payload, f"FAIL: {payload.get('error')}")


def cmd_create(args) -> int:
    command = {"command": "create", "env_id": args.env_id}
    if args.proxy:
        command["proxy"] = args.proxy
    if args.exit_ip:
        command["expected_exit_ip"] = args.exit_ip
    if args.display_name:
        command["display_name"] = args.display_name
    if args.seed:
        command["fingerprint_seed"] = args.seed
    if args.url:
        command["start_url"] = args.url
    payload = send(resolve_profile_dir(Path(args.profile)), command,
                   args.timeout)
    if payload.get("ok"):
        return emit(args, payload, f"created: {args.env_id}")
    return emit(args, payload, f"FAIL: {payload.get('error')}")


def cmd_panel(args) -> int:
    """Ask the window to show the environment side panel."""
    payload = send(resolve_profile_dir(Path(args.profile)),
                   {"command": "panel"}, args.timeout)
    if payload.get("ok"):
        return emit(args, payload, "panel shown")
    return emit(args, payload, f"FAIL: {payload.get('error')}")


def cmd_windows(args) -> int:
    """One line per browser window: which environment it holds and whether it is visible."""
    payload = send(resolve_profile_dir(Path(args.profile)),
                   {"command": "windows"}, args.timeout)
    if not payload.get("ok"):
        return emit(args, payload, f"FAIL: {payload.get('error')}")
    windows = payload.get("windows", [])
    lines = [f"active={payload.get('active')} windows={len(windows)}"]
    for index, window in enumerate(windows):
        envs = ",".join(window.get("envs", [])) or "-"
        lines.append(f"win{index} visible={window.get('visible')} env={envs} "
                     f"bounds={window.get('bounds')} tabs={len(window.get('tabs', []))}")
    return emit(args, payload, "\n".join(lines))


def cmd_strip(args) -> int:
    """Tabs of the active environment's window."""
    payload = send(resolve_profile_dir(Path(args.profile)),
                   {"command": "strip"}, args.timeout)
    if not payload.get("ok"):
        return emit(args, payload, f"FAIL: {payload.get('error')}")
    tabs = payload.get("tabs", [])
    lines = [f"active_index={payload.get('active_index')} count={len(tabs)}"]
    for tab in tabs:
        mark = "*" if tab.get("active") else " "
        lines.append(f"{mark}[{tab.get('index')}] {tab.get('env') or '-'} "
                     f"{(tab.get('title') or tab.get('url', ''))[:60]}")
    return emit(args, payload, "\n".join(lines))


def cmd_dump_state(args) -> int:
    """直接读磁盘上的状态文件，不需要 Chrome 在跑。"""
    path = resolve_profile_dir(Path(args.profile)) / STATE_FILE
    if not path.exists():
        print(f"{path} does not exist")
        return 0
    print(path.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Control the multi-environment side panel")
    ap.add_argument("--profile", required=True,
                    help="Chrome user-data-dir (the environment state lives here)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output for every subcommand")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show environments and which one is active")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("activate", help="switch to an environment")
    p.add_argument("env_id")
    p.add_argument("--proxy", default="")
    p.add_argument("--exit-ip", default="")
    p.add_argument("--display-name", default="")
    p.add_argument("--url", default="", help="start URL for a new environment tab")
    p.set_defaults(func=cmd_activate)

    p = sub.add_parser("create", help="define an environment without switching")
    p.add_argument("--homepage", default="",
                   help="page a new tab opens (default https://www.google.com)")
    p.add_argument("env_id")
    p.add_argument("--proxy", default="")
    p.add_argument("--exit-ip", default="")
    p.add_argument("--display-name", default="")
    p.add_argument("--seed", default="")
    p.add_argument("--url", default="")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("newtab", help="open one more tab in an environment (no URL -> home page)")
    p.add_argument("env_id")
    p.add_argument("--url", default="")
    p.set_defaults(func=cmd_newtab)

    p = sub.add_parser("panel", help="show the environment side panel")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("state", help="print envs.json from disk")
    p.set_defaults(func=cmd_dump_state)

    p = sub.add_parser("windows", help="show每 window: tabs, environment, visibility")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("strip", help="show the active window's tab strip")
    p.set_defaults(func=cmd_strip)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
