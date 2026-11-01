"""Drive one Chrome window that hosts several environments, over CDP.

Everything here exists because the environment model broke the old assumption
that "one chrome.exe == one profile". Fingerprints, exits and storage now belong
to a StoragePartition inside a single browser, so a probe has to (a) switch
environments through the real command channel, and (b) tell the resulting tabs
apart in the CDP target list.

Attribution rule used by every probe built on this module: the tab of an
environment is the target whose URL carries that environment's marker. The
marker comes from the environment's own start_url, not from a side channel, so a
target can never be attributed to the wrong environment. A probe that wants to
be certain compares an observable (exit IP, fingerprint field) against the value
that environment's configuration predicts -- see _m2_exit_probe.py.

Launch flags matter: `--no-proxy-server` keeps the system proxy (clash) out of
the picture for the default partition, while environment partitions still get
their own proxy from ConfigureNetworkContextParams. Without it, local domains
resolve through clash and every page is a chrome-error.
"""

from __future__ import annotations

import json
import os
import os
import shutil
import socket
import subprocess

from _nowindow import no_window
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Preferred DevTools port; falls back to a free one when something else already holds it.
FIXED_PORT = 9333


def port_open(port: int) -> bool:
    import socket
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0

sys.path.insert(0, str(HERE))
from bluebean_paths import DATA_ROOT  # noqa: E402

try:
    import websockets.sync.client as ws_sync
except ImportError:                                        # noqa: BLE001
    ws_sync = None


def wait_for_port_free(port: int, timeout: float = 8.0) -> bool:
    """Wait until nothing is listening on `port` - a dying browser keeps it bound for a moment."""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if not port_open(port):
            return True
        _time.sleep(0.25)
    return not port_open(port)


def pick_port() -> int:
    """The preferred port when it is genuinely free, otherwise a random one."""
    if wait_for_port_free(FIXED_PORT):
        return FIXED_PORT
    return free_port()


def devtools_answers(port: int, timeout: float = 4.0) -> bool:
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            _json.loads(response.read() or b"{}")
            return True
    except Exception:                              # noqa: BLE001
        return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Cdp:
    """One persistent CDP connection to one target.

    Persistent on purpose: the probes evaluate dozens of expressions per target,
    and the connect/settle cost of a fresh socket per expression dominated the
    runtime when this was one-shot.
    """

    def __init__(self, ws_url: str, open_timeout: float = 8.0):
        if ws_sync is None:
            raise RuntimeError("websockets is not installed (pip install websockets)")
        self._ws = ws_sync.connect(ws_url, open_timeout=open_timeout,
                                   close_timeout=2, max_size=1 << 22)
        self._id = 0

    def __enter__(self) -> "Cdp":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:                                  # noqa: BLE001
            pass

    def send(self, method: str, params: dict | None = None,
             timeout: float = 10.0) -> dict:
        self._id += 1
        want = self._id
        self._ws.send(json.dumps({"id": want, "method": method,
                                  "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(self._ws.recv(timeout=max(0.1, end - time.time())))
            except Exception:                              # noqa: BLE001
                return {}
            if msg.get("id") == want:
                return msg
        return {}

    def evaluate(self, expr: str, timeout: float = 10.0, await_promise: bool = False):
        msg = self.send("Runtime.evaluate",
                        {"expression": expr, "returnByValue": True,
                         "awaitPromise": await_promise,
                         "userGesture": True},
                        timeout=timeout)
        result = msg.get("result", {})
        if result.get("exceptionDetails"):
            return None
        return result.get("result", {}).get("value")

    def navigate(self, url: str, wait: float = 1.5) -> None:
        self.send("Page.enable")
        self.send("Page.navigate", {"url": url})
        time.sleep(wait)


class EnvBrowser:
    """A single Chrome window hosting N environments, plus the CLI to switch."""

    def __init__(self, chrome: str, extra_argv: list[str] | None = None,
                 window: str = "1100,760", user_data_dir: Path | None = None,
                 log_file: Path | None = None, cdp_port: int | None = None):
        self.chrome = chrome
        self.extra_argv = list(extra_argv or [])
        self.window = window
        # Only a profile this object created may be deleted by cleanup(). A caller
        # that passes its own profile owns those bytes: deleting one wiped a live
        # environment set (envs.json + partition storage) while chasing an unrelated
        # probe failure.
        self._owns_profile = user_data_dir is None
        self.user_data_dir = user_data_dir or Path(
            tempfile.mkdtemp(prefix="envprobe_"))
        # One log file per EnvBrowser instance. A shared name looked convenient and
        # cost two debugging sessions: a browser left running by an earlier step
        # keeps appending to it, so a probe reads another browser's ENVPANEL rows
        # and clicks coordinates that belong to a different window.
        self.log_file = log_file or (
            Path(tempfile.gettempdir()) /
            f"envprobe-{os.getpid()}-{id(self) & 0xFFFF:x}.log")
        # Fixed port: a random one made every probe depend on this file staying current, and it went stale whenever the
        # browser was started another way (measured: probes saw "no browser" while one was running on an unknown port).
        self.cdp_port = pick_port() if cdp_port is None else cdp_port
        self.proc: subprocess.Popen | None = None
        self.chrome_pid = 0
        # env id -> CDP target id. Captured when a tab is created for that
        # environment: nothing in the target list says which environment a page
        # belongs to, and putting a marker in the URL only works for URLs this
        # code chooses.
        self._env_targets: dict[str, str] = {}
        # ids of page targets that reported visibilityState == "visible"; used to
        # tell "the tab I just switched to" from "the tab I just parked".
        self._visible_ids: set[str] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 40.0) -> bool:
        for f in (self.log_file, Path(str(self.log_file) + ".old")):
            try:
                f.unlink()
            except OSError:
                pass
        argv = [self.chrome,
                f"--user-data-dir={self.user_data_dir}",
                # A fresh profile otherwise opens chrome://intro/welcome, and the
                # first-run experience does not go through the path that starts
                # the environment manager -- measured: the CLI got no reply at all.
                "--no-first-run",
                "--no-default-browser-check",
                # The system proxy would swallow local hostnames; environment
                # partitions get their own proxy from the browser, not from here.
                "--no-proxy-server",
                f"--remote-debugging-port={self.cdp_port}",
                f"--window-size={self.window}",
                "--enable-logging", f"--log-file={self.log_file}",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
            # Occlusion detection made `visibilityState` report "hidden" for a window that
            # was only covered, which broke the stealth gate nondeterministically.
            "--disable-features=CalculateNativeWinOcclusion",
                "--disable-renderer-backgrounding",
                *self.extra_argv,
                "about:blank"]
        env = dict(os.environ)
        env.setdefault("LAN_PRODUCT_HOME", str(HERE))
        self.proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.chrome_pid = self.proc.pid
        # Record the port: it is randomly assigned, and without this every verification script has to scrape the
        # command line (the previous round lost it after a restart and the page could not be inspected at all).
        try:
            Path(DATA_ROOT / '.env-ui-port.json').write_text(
                json.dumps({'port': self.cdp_port, 'pid': self.chrome_pid}), encoding='utf-8')
        except OSError:
            pass
        return self.wait_cdp(timeout)

    def wait_cdp(self, timeout: float = 40.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.cdp_port}/json/version",
                        timeout=2):
                    return True
            except Exception:                              # noqa: BLE001
                time.sleep(0.2)
        return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> int | None:
        if self.proc is None:
            return None
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except Exception:                                  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:                              # noqa: BLE001
                pass
        return self.proc.returncode

    def cleanup(self) -> None:
        if not self._owns_profile:
            return
        shutil.rmtree(self.user_data_dir, ignore_errors=True)

    # -- command channel ---------------------------------------------------
    def profile_dir(self) -> Path:
        default = self.user_data_dir / "Default"
        return default if default.is_dir() else self.user_data_dir

    def cli(self, *args: str, timeout: float = 20.0) -> dict:
        """Run env_cli.py against this window; returns the parsed result.

        `--json` keeps the answer machine-readable, so a probe asserts on the
        manager's own state rather than on log text.
        """
        # Wait for the profile directory to exist: env_cli resolves <udd>/Default,
        # and before the profile is created it would fall back to <udd> and write
        # the command where nothing is polling.
        deadline = time.time() + 20
        while time.time() < deadline and not (self.user_data_dir / "Default").is_dir():
            time.sleep(0.1)
        r = subprocess.run(
            [sys.executable, str(HERE / "env_cli.py"),
             "--profile", str(self.user_data_dir),
             "--timeout", str(timeout), "--json", *args],
            capture_output=True, text=True, timeout=timeout + 30,
            encoding="utf-8", errors="replace", creationflags=no_window())
        out = (r.stdout or "").strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"ok": False, "raw": out,
                    "stderr": (r.stderr or "").strip(), "rc": r.returncode}

    def state(self) -> dict:
        """The manager's own view of every environment, via the CLI."""
        out = self.cli("list")
        return out if isinstance(out, dict) else {}

    def wait_state(self, predicate, timeout: float = 15.0) -> dict | None:
        end = time.time() + timeout
        last: dict | None = None
        while time.time() < end:
            last = self.state()
            if isinstance(last.get("envs"), list) and predicate(last):
                return last
            time.sleep(0.2)
        return last if last and last.get("envs") else None

    # -- targets -----------------------------------------------------------
    def targets(self) -> list[dict]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.cdp_port}/json/list",
                    timeout=8) as r:
                return [t for t in json.loads(r.read().decode("utf-8", "replace"))
                        if t.get("type") == "page"]
        except Exception:                                  # noqa: BLE001
            return []

    def target_by_url(self, fragment: str, timeout: float = 15.0) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            for t in self.targets():
                if fragment in (t.get("url") or ""):
                    return t
            time.sleep(0.2)
        return None

    def targets_by_url(self, fragment: str) -> list[dict]:
        return [t for t in self.targets() if fragment in (t.get("url") or "")]

    def cdp_for(self, target: dict) -> Cdp:
        return Cdp(target["webSocketDebuggerUrl"])

    def browser_ws(self) -> str:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.cdp_port}/json/version",
                timeout=6) as response:
            return json.loads(response.read().decode())["webSocketDebuggerUrl"]

    def create_target(self, url: str = "about:blank") -> dict:
        """Open a tab outside any environment (the default partition).

        Used as the control in the exit probe: a plain tab must not inherit an
        environment's proxy, and the startup tab cannot serve that role because
        the first environment activation closes it (an environment switch takes
        every tab that belongs to no environment out of the strip).
        """
        with Cdp(self.browser_ws()) as cdp:
            result = cdp.send("Target.createTarget", {"url": url})
        return result.get("result", {})

    # -- convenience -------------------------------------------------------
    def activate(self, env_id: str, *, proxy: str = "", exit_ip: str = "",
                 url: str = "", display_name: str = "") -> dict:
        args = ["activate", env_id]
        if proxy:
            args += ["--proxy", proxy]
        if exit_ip:
            args += ["--exit-ip", exit_ip]
        if display_name:
            args += ["--display-name", display_name]
        if url:
            args += ["--url", url]
        # The baseline has to be taken with the target list already populated,
        # otherwise the first activation sees "two new ids" (the environment tab
        # plus the startup tab that is still being listed) and gives up.
        end = time.time() + 5.0
        before: set[str] = set()
        while time.time() < end:
            before = {t["id"] for t in self.targets()}
            if before:
                break
            time.sleep(0.2)
        payload = self.cli(*args)
        if payload.get("ok"):
            # Wait for the new target to show up. The command channel returns as
            # soon as the tab is inserted, while /json/list is filled in
            # asynchronously -- reading once made this mapping randomly empty,
            # which showed up downstream as "no target for env X".
            # A switch that re-inserts a parked tab produces no new CDP target:
            # the tab already existed while detached. Waiting the full timeout for
            # "a new id" made every such switch cost 8 s and turned the 100-switch
            # gate into a 14-minute run. So: look for a new id briefly, then fall
            # back to the tab that is actually on screen.
            before_visible = set(self._visible_ids)
            end = time.time() + 0.4
            while time.time() < end:
                targets = self.targets()
                appeared = {t["id"] for t in targets} - before
                if len(appeared) == 1:
                    self._env_targets[env_id] = appeared.pop()
                    break
                if len(appeared) > 1:
                    # Prefer a target that already navigated somewhere; the extra
                    # one is the startup tab on its way out.
                    named = [t["id"] for t in targets
                             if t["id"] in appeared
                             and (t.get("url") or "") not in ("", "about:blank")]
                    if len(named) == 1:
                        self._env_targets[env_id] = named[0]
                        break
                time.sleep(0.2)
            if env_id not in self._env_targets:
                # Last resort: the environment just activated is the tab on
                # screen, and only the selected tab reports "visible". Targets
                # that existed before the switch are excluded: the startup tab is
                # often still listed (and visible) for a moment while it is being
                # closed, and navigating *that* tab reported the machine's own
                # address instead of the environment's exit.
                # The tab that is on screen now is the environment's tab; a tab
                # that was already visible before the switch is the one that just
                # got parked, so exclude by *visibility* rather than by existence.
                visible = self.visible_target(timeout=3.0, skip_local=True,
                                             exclude=before_visible or before)
                if visible:
                    self._env_targets[env_id] = visible["id"]
            if env_id in self._env_targets:
                self._visible_ids = {self._env_targets[env_id]}
        return payload

    def visible_target(self, timeout: float = 6.0,
                       exclude: set[str] | None = None,
                       skip_local: bool = False) -> dict | None:
        """The page target that reports document.visibilityState == 'visible'.

        skip_local ignores loopback and chrome:// pages: the assistant layer hosts the workbench inside the active
        environment's partition, so it is the page on screen after a switch without being the environment's tab.
        """
        skip = exclude or set()
        end = time.time() + timeout
        while time.time() < end:
            for target in self.targets():
                if target.get("id") in skip:
                    continue
                if skip_local and str(target.get("url") or "").startswith(
                        ("http://127.0.0.1:", "chrome://")):
                    continue
                if not target.get("webSocketDebuggerUrl"):
                    continue
                try:
                    with Cdp(target["webSocketDebuggerUrl"],
                             open_timeout=3) as cdp:
                        state = cdp.evaluate("document.visibilityState",
                                             timeout=3)
                except Exception:                            # noqa: BLE001
                    continue
                if state == "visible":
                    return target
            time.sleep(0.3)
        return None

    def target_for_env(self, env_id: str) -> dict | None:
        """The page target created for `env_id`, if it still exists."""
        wanted = self._env_targets.get(env_id)
        for target in self.targets():
            if wanted and target.get("id") == wanted:
                return target
        return None

    def newtab(self, env_id: str, url: str = "") -> dict:
        args = ["newtab", env_id]
        if url:
            args += ["--url", url]
        before = {t["id"] for t in self.targets()}
        payload = self.cli(*args)
        if payload.get("ok"):
            appeared = {t["id"] for t in self.targets()} - before
            for i, target_id in enumerate(sorted(appeared)):
                self._env_targets[f"{env_id}#{i}"] = target_id
        return payload

    def log_lines(self, needle: str, limit: int = 40) -> list[str]:
        lines: list[str] = []
        for f in (self.log_file, Path(str(self.log_file) + ".old")):
            if f.exists():
                try:
                    lines += [l for l in
                              f.read_text(encoding="utf-8", errors="replace")
                              .splitlines() if needle in l]
                except OSError:
                    pass
        return lines[-limit:]


def file_url(path: Path | str, query: str = "") -> str:
    p = Path(path).resolve().as_posix()
    return f"file:///{p}" + (f"?{query}" if query else "")


# ---------------------------------------------------------------------------
# Windows process accounting
#
# psutil is not installed on this workstation, and the two things the probes need
# (per-process CPU for renderer attribution, private bytes for the leak check) are
# two syscalls. Keeping it in ctypes also means the numbers are the same ones
# Task Manager shows.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    def process_tree(root_pid: int) -> dict[int, str]:
        """pid -> exe name for `root_pid` and every descendant."""
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return {}
        children: dict[int, list[int]] = {}
        names: dict[int, str] = {}
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid, ppid = entry.th32ProcessID, entry.th32ParentProcessID
            names[pid] = entry.szExeFile
            children.setdefault(ppid, []).append(pid)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        _k32.CloseHandle(snap)

        out: dict[int, str] = {}
        stack = [root_pid]
        while stack:
            pid = stack.pop()
            if pid in out:
                continue
            out[pid] = names.get(pid, "?")
            stack.extend(children.get(pid, []))
        return out

    def process_cpu_seconds(pid: int) -> float | None:
        handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation, exit_, kernel, user = (wt.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(handle, ctypes.byref(creation),
                                        ctypes.byref(exit_),
                                        ctypes.byref(kernel),
                                        ctypes.byref(user)):
                return None
            def to_seconds(ft: wt.FILETIME) -> float:
                return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
            return to_seconds(kernel) + to_seconds(user)
        finally:
            _k32.CloseHandle(handle)

    def private_bytes(pid: int) -> int | None:
        handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                                  False, pid)
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not _psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.PrivateUsage)
        finally:
            _k32.CloseHandle(handle)

else:  # pragma: no cover - this workstation is Windows; keep imports usable
    def process_tree(root_pid: int) -> dict[int, str]:
        return {}

    def process_cpu_seconds(pid: int) -> float | None:
        return None

    def private_bytes(pid: int) -> int | None:
        return None


def tree_private_mb(root_pid: int) -> float | None:
    total = 0
    seen = 0
    for pid in process_tree(root_pid):
        value = private_bytes(pid)
        if value:
            total += value
            seen += 1
    return round(total / (1024 * 1024), 1) if seen else None


def cpu_owner_of_workload(root_pid: int, sample_seconds: float = 0.5) -> dict:
    """Sample CPU twice around a workload and return per-pid deltas.

    Used to attribute a page to a renderer process: nothing in CDP maps a target
    to a pid, but a busy loop on the page is visible in exactly one process.
    """
    pids = list(process_tree(root_pid))
    before = {pid: process_cpu_seconds(pid) for pid in pids}
    time.sleep(sample_seconds)
    after = {pid: process_cpu_seconds(pid) for pid in pids}
    deltas = {}
    for pid in pids:
        b, a = before.get(pid), after.get(pid)
        if a is not None and b is not None:
            deltas[pid] = round(a - b, 3)
    return deltas


# ---------------------------------------------------------------------------
# Real mouse input
#
# Two separate jobs need the physical cursor: proving that CDP-injected events
# do NOT move it (the "automation must not steal the mouse" contract), and
# clicking the side panel, which is browser UI -- CDP events go to the renderer
# and can never reach a Views button.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    def get_cursor_pos() -> tuple[int, int]:
        point = wt.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return (point.x, point.y)

    def set_cursor_pos(x: int, y: int) -> None:
        _user32.SetCursorPos(int(x), int(y))

    def click_at(x: int, y: int, settle: float = 0.35) -> None:
        """Move the real pointer and click with real button events."""
        set_cursor_pos(x, y)
        time.sleep(settle)
        _user32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
        time.sleep(0.05)
        _user32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP

    def windows_of_process(pid: int) -> list[tuple[int, str]]:
        """Visible top-level windows owned by `pid` (hwnd, title)."""
        found: list[tuple[int, str]] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def callback(hwnd, _lparam):
            owner = wt.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not _user32.IsWindowVisible(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((int(hwnd), buf.value))
            return True

        _user32.EnumWindows(WNDENUMPROC(callback), 0)
        return found

    def foreground_window() -> int:
        return int(_user32.GetForegroundWindow())

    def focus_window(hwnd: int) -> bool:
        """Make `hwnd` the foreground window, working around the foreground lock.

        SetForegroundWindow alone fails when the calling process does not own the
        foreground, and the failure is silent: clicks then land in whichever window
        is on top. Measured cost of ignoring that: a probe clicked a *different*
        browser window that happened to overlap and reported the click as broken.
        """
        _user32.ShowWindow(hwnd, 9)                   # SW_RESTORE
        if foreground_window() == hwnd:
            return True
        current_thread = _k32.GetCurrentThreadId()
        foreground = foreground_window()
        target_thread = _user32.GetWindowThreadProcessId(foreground, None)
        attached = bool(_user32.AttachThreadInput(current_thread, target_thread,
                                                 True))
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(current_thread, target_thread, False)
        return foreground_window() == hwnd

    def window_client_origin(hwnd: int) -> tuple[int, int]:
        point = wt.POINT(0, 0)
        _user32.ClientToScreen(hwnd, ctypes.byref(point))
        return (point.x, point.y)

    def window_rect(hwnd: int) -> tuple[int, int, int, int]:
        rect = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return (rect.left, rect.top, rect.right - rect.left,
                rect.bottom - rect.top)

    def dpi_scale(hwnd: int) -> float:
        try:
            return _user32.GetDpiForWindow(hwnd) / 96.0
        except Exception:                              # noqa: BLE001
            return 1.0

    def input_desktop_available() -> bool:
        """False when this process has no interactive input desktop.

        Measured on this workstation: with the RDP session disconnected,
        GetCursorPos fails (reports 0,0), GetForegroundWindow returns 0 and
        OpenInputDesktop fails -- set/read of the physical cursor and synthetic
        mouse_event input then do nothing at all, which looks exactly like a
        broken UI.
        """
        point = wt.POINT(-1, -1)
        if not _user32.GetCursorPos(ctypes.byref(point)):
            return False
        return (point.x, point.y) != (0, 0)

    def post_click(hwnd: int, client_x: int, client_y: int,
                   settle: float = 0.15) -> None:
        """Deliver a click as window messages instead of physical input.

        Works without an input desktop (no cursor needed) and still goes through
        the window's own hit-test and the view's button callback, which is the
        wiring a click test is about.
        """
        WM_MOUSEMOVE = 0x0200
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        MK_LBUTTON = 0x0001
        lparam = ((client_y & 0xFFFF) << 16) | (client_x & 0xFFFF)
        _user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        time.sleep(settle * 0.3)
        _user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        time.sleep(settle * 0.3)
        _user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)

else:  # pragma: no cover
    def get_cursor_pos() -> tuple[int, int]:
        return (0, 0)

    def set_cursor_pos(x: int, y: int) -> None:
        return None

    def click_at(x: int, y: int, settle: float = 0.35) -> None:
        return None

    def input_desktop_available() -> bool:
        return False

    def post_click(hwnd: int, client_x: int, client_y: int,
                   settle: float = 0.15) -> None:
        return None

    def windows_of_process(pid: int) -> list[tuple[int, str]]:
        return []

    def focus_window(hwnd: int) -> bool:
        return False

    def window_client_origin(hwnd: int) -> tuple[int, int]:
        return (0, 0)

    def window_rect(hwnd: int) -> tuple[int, int, int, int]:
        return (0, 0, 0, 0)

    def dpi_scale(hwnd: int) -> float:
        return 1.0

