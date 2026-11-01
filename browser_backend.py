"""Browser side of the bridge: CDP client + environment resolution.

Kept separate from the HTTP server so the same code can be used by probes and tests.
Resolution rule: a target belongs to an environment when its URL carries `?e=<env>`
(how our probes navigate) or, for the active environment, when it is the only visible
page target. Everything else is an error rather than a silent wrong-tab action.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from _nowindow import no_window

import sys

sys.path.insert(0, str(Path(__file__).parent))
from _env_driver import Cdp                                          # noqa: E402
from bluebean_paths import DATA_ROOT, PROFILE_DIR                    # noqa: E402


class BrowserCdp(Cdp):
    """Surface transport/protocol failures; never report an empty success."""

    def send(self, method, params=None, timeout=10.0):
        reply = super().send(method, params, timeout)
        if not reply or 'result' not in reply:
            error = (reply.get('error') or {}).get('message', 'connection closed or response timed out')
            raise RuntimeError(f'{method}: {error}')
        return reply

    def evaluate(self, expr, timeout=10.0, await_promise=False):
        reply = self.send('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                          'awaitPromise': await_promise, 'userGesture': True}, timeout)
        result = reply['result']
        if result.get('exceptionDetails'):
            raise RuntimeError('page evaluation failed: ' + result['exceptionDetails'].get('text', 'JavaScript exception'))
        return result.get('result', {}).get('value')

PROFILE = PROFILE_DIR
STATE_FILE = PROFILE / "Default" / "envs.json"
DEVTOOLS_PORT_FILE = PROFILE / "DevToolsActivePort"

# Observation ids are only meaningful inside the observation that produced them, and
# only for one environment: that is what stops "looked at A, clicked on B".
@dataclass
class Observation:
    env: str
    epoch: int
    items: list[dict] = field(default_factory=list)

    def by_id(self, element_id: str) -> dict:
        for item in self.items:
            if item["id"] == element_id:
                return item
        raise KeyError(f"unknown element id {element_id!r} in env {self.env}")


class BrowserBackend:
    def __init__(self, port: int | None = None) -> None:
        # Gates launch their own browser with a temp profile; they pass the port explicitly.
        self._explicit_port = port
        self.observations: dict[str, Observation] = {}
        self._sessions: dict[str, tuple[str, Cdp]] = {}
        self._snapshots: dict[str, dict] = {}
        self._target_cache = {}
        self._live_cache = None
        self._runtime_cache = None
        self._live_lock = threading.RLock()
        self._runtime_lock = threading.RLock()
        self._step_local = threading.local()

    @contextmanager
    def step_context(self):
        # Reuse validation for action + post-action snapshot within ONE step.
        # The next step resolves environment ownership again.
        self._step_local.connections = {}
        try:
            yield
        except Exception:
            # A timed-out socket may still deliver an old reply later. Discard
            # it for the next task, without replaying a possibly completed action.
            for key in list(self._step_local.connections):
                self._drop_session(key)
            raise
        finally:
            self._step_local.connections = None

    # ------------------------------------------------------------------ discovery
    _cached_port: int | None = None

    @staticmethod
    def _port_from_process() -> int | None:
        """Read --remote-debugging-port off the browser process command line."""
        import subprocess
        from _nowindow import no_window
        script = (
            "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -like '*" + str(PROFILE).replace("'", "''") + "*' "
            "-and $_.CommandLine -match '--remote-debugging-port=(\\d+)' } | "
            "Select-Object -First 1 -ExpandProperty CommandLine")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                 capture_output=True, text=True, timeout=25, creationflags=no_window()).stdout
        except Exception:                                        # noqa: BLE001
            return None
        match = re.search(r"--remote-debugging-port=(\d+)", out or "")
        return int(match.group(1)) if match else None

    @classmethod
    def debug_port(cls, refresh: bool = False) -> int:
        if cls._cached_port is not None and not refresh:
            return cls._cached_port
        def responds(candidate: int) -> bool:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{candidate}/json/version", timeout=2) as reply:
                    return reply.status == 200
            except Exception:                                    # noqa: BLE001
                return False

        candidates: list[int] = []
        # Fixed-port launches do not update Chromium's DevToolsActivePort.
        # Prefer the launcher's current record before slow process discovery.
        try:
            saved = json.loads((DATA_ROOT / '.env-ui-port.json').read_text(encoding='utf-8'))
            candidates.append(int(saved['port']))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if DEVTOOLS_PORT_FILE.exists():
            lines = DEVTOOLS_PORT_FILE.read_text(encoding="utf-8").strip().splitlines()
            if lines and lines[0].strip().isdigit():
                candidates.append(int(lines[0].strip()))
        port: int | None = None
        for candidate in candidates:
            if responds(candidate):
                port = candidate
                break
        if port is None:
            from_process = cls._port_from_process()
            if from_process and responds(from_process):
                port = from_process
        if port is None:
            raise RuntimeError("browser is not running with remote debugging "
                               f"(no {DEVTOOLS_PORT_FILE}, no --remote-debugging-port "
                               f"on a chrome.exe using {PROFILE})")
        cls._cached_port = port
        return port

    def port(self, refresh: bool = False) -> int:
        """Explicit port when given, otherwise resolve the profile's live browser."""
        if self._explicit_port:
            return self._explicit_port
        return type(self).debug_port(refresh)

    def targets(self, retry: bool = True) -> list[dict]:
        url = f"http://127.0.0.1:{self.port()}/json/list"
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                return [t for t in json.loads(response.read().decode())
                        if t.get("type") == "page"]
        except Exception:                                          # noqa: BLE001
            if not retry:
                raise
            # The browser was restarted: the cached port points at nothing now.
            type(self)._cached_port = None
            self._explicit_port = None
            self.port(refresh=True)
            return self.targets(retry=False)

    _live_cache: tuple[float, dict] | None = None

    def env_state(self) -> dict:
        """Persisted environment metadata (proxy, locale, ...) from envs.json."""
        if not STATE_FILE.exists():
            return {"active": "", "envs": []}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))

    def live_state(self, max_age: float = 1.0, timeout: float = 10.0) -> dict:
        """Live per-environment tabs (url/title/target_id) from the command channel."""
        import env_cli
        with self._live_lock:
            if self._live_cache and time.monotonic() - self._live_cache[0] < max_age:
                return self._live_cache[1]
            state = env_cli.send(env_cli.resolve_profile_dir(PROFILE), {"command": "list"}, timeout)
            if state.get('ok') is False or not isinstance(state.get('envs'), list):
                self._live_cache = None
                raise RuntimeError('cannot query browser environments: ' + str(state.get('error', 'invalid response')))
            self._live_cache = (time.monotonic(), state)
            return state

    _runtime_cache: tuple[float, dict] | None = None

    def runtime_state(self, max_age: float = 1.0) -> dict:
        """Read running environment ids without serializing every page target."""
        import env_cli
        with self._runtime_lock:
            if self._runtime_cache and time.monotonic() - self._runtime_cache[0] < max_age:
                return self._runtime_cache[1]
            state = env_cli.send(env_cli.resolve_profile_dir(PROFILE), {"command": "runtime"}, 2.5)
            if (state.get('ok') is False or not isinstance(state.get('running_envs'), list)
                    or not isinstance(state.get('active', ''), str)):
                error = str(state.get('error', 'invalid response'))
                # Only fall back for an explicit compatibility response. Timeouts
                # and a busy command channel must not trigger a second native wait.
                if 'unknown command' not in error:
                    raise RuntimeError('cannot query browser runtime state: ' + error)
                full = self.live_state(max_age=1, timeout=2.5)
                state = {'ok': True, 'active': full.get('active', ''),
                         'running_envs': [row.get('env_id') for row in full.get('envs', [])
                                          if row.get('tabs')]}
            self._runtime_cache = (time.monotonic(), state)
            return state

    def envs(self) -> list[dict]:
        return self.env_state().get("envs", [])

    _target_cache: dict[str, tuple[float, str]] = {}

    def _visibility(self, target: dict) -> str:
        try:
            with Cdp(target["webSocketDebuggerUrl"], open_timeout=5) as cdp:
                return (cdp.evaluate("document.visibilityState", timeout=5) or "").strip('"')
        except Exception:                                        # noqa: BLE001
            return "unknown"

    def target_for_env(self, env: str | None) -> dict:
        state = self.live_state()
        env = env or state.get("active") or self.env_state().get("active", "")
        pages = self.targets()
        if not pages:
            raise RuntimeError("no page targets")

        # Exact handle first: the browser publishes the DevTools target id per env.
        for entry in state.get("envs", []):
            if entry.get("env_id") != env:
                continue
            for tab in entry.get("tabs", []):
                target_id = tab.get("target_id") or ""
                if not target_id:
                    continue
                for page in pages:
                    if page["id"] == target_id:
                        self._target_cache[env] = (time.time(), page["id"])
                        return page

        if getattr(self, "strict_resolution", False):
            raise RuntimeError(f"no authoritative live tab for environment {env!r}")

        cached = self._target_cache.get(env)
        if cached and time.time() - cached[0] < 5 and any(
                page["id"] == cached[1] for page in pages):
            return next(page for page in pages if page["id"] == cached[1])

        for page in pages:
            if f"e={env}" in (page.get("url") or ""):
                self._target_cache[env] = (time.time(), page["id"])
                return page

        if env and env == self.env_state().get("active"):
            for page in pages:
                if self._visibility(page) == "visible":
                    self._target_cache[env] = (time.time(), page["id"])
                    return page

        raise RuntimeError(
            f"cannot resolve a tab for env {env!r}: no live tab for it, no page carries "
            f"?e={env}, and it is not the active environment "
            f"(known pages: {[page.get('url', '')[:40] for page in pages]})")

    # ----------------------------------------------------------------------- cdp
    def _cdp(self, env: str | None) -> Cdp:
        """A CDP connection for this environment, cached across steps.

        Opening a WebSocket per step costs ~10-20 ms (measured); a task with 20 steps paid it
        20 times for no reason.
        """
        key = env or self.env_state().get("active", "")
        validated = getattr(getattr(self, '_step_local', None), 'connections', None)
        if validated is not None and key in validated:
            return validated[key]
        cached = self._sessions.get(key)
        if cached is not None:
            target_id, cdp = cached
            try:
                strict = getattr(self, "strict_resolution", False)
                exact = (self.target_for_env(env)["id"] == target_id) if strict else any(
                    page["id"] == target_id for page in self.targets())
                if exact:
                    if validated is not None:
                        validated[key] = cdp
                    return cdp
            except Exception:                                    # noqa: BLE001
                pass
            self._drop_session(key)
        target = self.target_for_env(env)
        cdp = BrowserCdp(target["webSocketDebuggerUrl"], open_timeout=8)
        self._sessions[key] = (target["id"], cdp)
        if validated is not None:
            validated[key] = cdp
        return cdp

    # This probe intentionally reads browser-exposed identity only.  It does not
    # inspect storage, cookies, page text, form fields, or credentials.
    FINGERPRINT_JS = r"""
(async () => {
  const uaData = navigator.userAgentData || null;
  let brands = [];
  try { brands = uaData ? await uaData.getHighEntropyValues(['architecture', 'bitness', 'platform', 'platformVersion', 'fullVersionList']) : []; } catch (_) {}
  const fontsToCheck = ['Arial', 'Calibri', 'Cambria', 'Consolas', 'Courier New', 'Georgia', 'Segoe UI', 'Tahoma', 'Times New Roman', 'Trebuchet MS', 'Verdana'];
  const fonts = fontsToCheck.filter(name => {
    try { return !!document.fonts && document.fonts.check(`12px "${name}"`); } catch (_) { return false; }
  });
  let webgl = {};
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      webgl = {
        vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
        renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
        version: gl.getParameter(gl.VERSION),
      };
    }
  } catch (_) {}
  const intl = (() => { try { return Intl.DateTimeFormat().resolvedOptions(); } catch (_) { return {}; } })();
  return JSON.stringify({
    user_agent: navigator.userAgent || '',
    platform: navigator.platform || '',
    webdriver: !!navigator.webdriver,
    max_touch_points: navigator.maxTouchPoints || 0,
    languages: Array.from(navigator.languages || []),
    language: navigator.language || '',
    intl_locale: intl.locale || '',
    timezone: intl.timeZone || '',
    timezone_offset: new Date().getTimezoneOffset(),
    hardware_concurrency: navigator.hardwareConcurrency || null,
    device_memory_gb: navigator.deviceMemory || null,
    screen: { width: screen.width, height: screen.height, avail_width: screen.availWidth, avail_height: screen.availHeight, color_depth: screen.colorDepth, pixel_depth: screen.pixelDepth },
    window: { inner_width: innerWidth, inner_height: innerHeight, outer_width: outerWidth, outer_height: outerHeight, dpr: devicePixelRatio },
    webgl,
    fonts: { tested: fontsToCheck, detected: fonts, count: fonts.length, status: document.fonts?.status || 'unknown' },
    user_agent_data: brands,
  });
})()
"""

    def fingerprint_snapshot(self, env: str | None) -> dict:
        """Read one environment's browser-visible identity through its own tab."""
        cdp = self._cdp(env)
        version = cdp.send('Browser.getVersion', timeout=8).get('result', {})
        raw = cdp.evaluate(self.FINGERPRINT_JS, timeout=15, await_promise=True)
        values = json.loads(raw) if raw else {}
        if not isinstance(values, dict):
            raise RuntimeError('invalid browser fingerprint response')
        values['_browser'] = {
            'product': version.get('product', ''),
            'user_agent': version.get('userAgent', ''),
            'js_version': version.get('jsVersion', ''),
            'protocol_version': version.get('protocolVersion', ''),
            'revision': version.get('revision', ''),
        }
        return values

    def _drop_session(self, env: str) -> None:
        cached = self._sessions.pop(env, None)
        if cached is not None:
            try:
                cached[1].close()
            except Exception:                                    # noqa: BLE001
                pass

    def close_sessions(self) -> None:
        for env in list(self._sessions):
            self._drop_session(env)

    # ------------------------------------------------------------------ snapshots
    SNAPSHOT_JS = r"""
(() => {
  const nodes = document.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=link]');
  let fingerprint = 0;
  const names = [];
  for (const el of nodes) {
    const text = (el.getAttribute('aria-label') || el.innerText || el.value || '').trim()
        .replace(/\s+/g, ' ').slice(0, 40);
    for (let i = 0; i < text.length; i++) {
      fingerprint = (fingerprint * 31 + text.charCodeAt(i)) >>> 0;
    }
    if (names.length < 40) names.push(text);
  }
  return JSON.stringify({
    url: location.href,
    title: document.title,
    count: nodes.length,
    fingerprint,
    names,
    ready: document.readyState,
  });
})()
"""

    def snapshot(self, env: str | None = None) -> dict:
        """Cheap page fingerprint used to tell the agent what changed after an action."""
        cdp = self._cdp(env)
        raw = cdp.evaluate(self.SNAPSHOT_JS, timeout=15)
        return json.loads(raw) if raw else {}

    @staticmethod
    def diff(before: dict, after: dict) -> dict:
        if not before:
            return {"first": True, "url": after.get("url"), "count": after.get("count")}
        before_names = set(before.get("names") or [])
        after_names = set(after.get("names") or [])
        return {
            "url_changed": before.get("url") != after.get("url"),
            "title_changed": before.get("title") != after.get("title"),
            "count_delta": (after.get("count") or 0) - (before.get("count") or 0),
            "appeared": sorted(after_names - before_names)[:8],
            "disappeared": sorted(before_names - after_names)[:8],
            "url": after.get("url"),
        }

    # --------------------------------------------------------------- observations
    OBSERVE_JS = r"""
(() => {
  const out = [];
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[onclick]';
  let id = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    if (r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) continue;
    const cx = Math.min(innerWidth - 1, Math.max(0, r.left + r.width / 2));
    const cy = Math.min(innerHeight - 1, Math.max(0, r.top + r.height / 2));
    const hit = document.elementFromPoint(cx, cy);
    const name = (el.getAttribute('aria-label') || el.innerText || el.value ||
                  el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    out.push({
      id: 'e' + (id++),
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') ||
            (el.tagName === 'A' ? 'link' : el.tagName === 'BUTTON' ? 'button' : ''),
      name,
      href: (el.getAttribute('href') || '').slice(0, 80),
      bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      clickable: cs.pointerEvents !== 'none',
      occluded: !(hit === el || el.contains(hit) || (hit && hit.contains(el))),
      disabled: !!el.disabled,
      visual_only: false,
    });
    if (out.length >= 120) break;
  }
  return JSON.stringify({url: location.href, title: document.title,
                         viewport: [innerWidth, innerHeight],
                         dpr: devicePixelRatio, elements: out});
})()
"""

    def observe(self, env: str | None, limit: int = 80) -> dict:
        cdp = self._cdp(env)
        raw = cdp.evaluate(self.OBSERVE_JS, timeout=20)
        data = json.loads(raw) if raw else {"elements": []}
        resolved_env = env or self.env_state().get("active", "")
        epoch = int(time.time() * 1000)
        observation = Observation(env=resolved_env, epoch=epoch,
                                  items=data.get("elements", [])[:limit])
        self.observations[resolved_env] = observation
        data["elements"] = observation.items
        data["env"] = resolved_env
        data["epoch"] = epoch
        return data

    # ------------------------------------------------------------------- actions

    BATCH_JS = r"""
(ops) => {
  const out = [];
  for (const op of ops) {
    try {
      if (op.click !== undefined) {
        const el = document.querySelector(op.click);
        if (el) { el.click(); out.push({click: true, tag: el.tagName.toLowerCase()}); }
        else out.push({click: false, error: "not found: " + op.click});
      } else if (op.type !== undefined) {
        const el = document.querySelector(op.type.selector);
        if (el) {
          el.focus();
          el.value = op.type.text;
          el.dispatchEvent(new Event("input", {bubbles: true}));
          out.push({type: true});
        } else out.push({type: false, error: "not found: " + op.type.selector});
      } else if (op.read !== undefined) {
        const el = document.querySelector(op.read);
        out.push({read: el ? (el.innerText || el.value || "").slice(0, op.limit || 200)
                           : null});
      } else if (op.wait !== undefined) {
        out.push({wait: op.wait});
      } else {
        out.push({error: "unknown op"});
      }
    } catch (e) {
      out.push({error: String(e).slice(0, 120)});
    }
  }
  return JSON.stringify(out);
}
"""

    def batch(self, env: str | None, ops: list[dict]) -> dict:
        """Run a list of page ops inside one evaluate (untrusted events: own sites only)."""
        cdp = self._cdp(env)
        started = time.perf_counter()
        result = cdp.evaluate(
            f"({self.BATCH_JS.strip()})({json.dumps(ops)})", timeout=30)
        elapsed = (time.perf_counter() - started) * 1000
        return {"ops": len(ops), "results": json.loads(result) if result else [],
                "ms": round(elapsed, 2)}


    RECTS_JS = """(() => {
      const sels = %s;
      const dpr = window.devicePixelRatio || 1;
      return JSON.stringify(sels.map((sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        return {x: cx, y: cy, dpr,
                covered: !(hit === el || el.contains(hit) || (hit && hit.contains(el)))};
      }));
    })()"""

    IN_PAGE_CLICKS_JS = """(() => {
      const sels = %s;
      const out = [];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) { el.click(); out.push({sel, click: true}); }
        else { out.push({sel, click: false}); }
      }
      return JSON.stringify(out);
    })()"""


    # ------------------------------------------------------------- kernel transport
    KERNEL_PORT_FILE = DATA_ROOT / ".lan-automation-port"
    KERNEL_TOKEN_FILE = DATA_ROOT / ".lan-automation-token"

    def kernel_available(self) -> bool:
        """True when something actually answers on the automation port.

        File existence is not enough: a leftover port file from an earlier run pointed at a closed
        socket and every kernel click failed with ConnectionRefused.
        """
        if not (self.KERNEL_PORT_FILE.exists() and self.KERNEL_TOKEN_FILE.exists()):
            return False
        now = time.monotonic()
        cached = getattr(self, "_kernel_probe", None)
        if cached and now - cached[0] < 5.0:
            return cached[1]
        import socket as socket_module
        try:
            port = int(self.KERNEL_PORT_FILE.read_text(encoding="utf-8").strip())
            with socket_module.create_connection(("127.0.0.1", port), timeout=0.3):
                alive = True
        except Exception:                                        # noqa: BLE001
            alive = False
        self._kernel_probe = (now, alive)
        return alive

    def kernel_task(self, env: str | None, ops: list[dict],
                    timeout: float = 10.0) -> dict:
        """Run ops through the in-browser executor (mojom::AutomationTask) over the socket."""
        import socket as socket_module
        port = int(self.KERNEL_PORT_FILE.read_text(encoding="utf-8").strip())
        token = self.KERNEL_TOKEN_FILE.read_text(encoding="utf-8").strip()
        payload = json.dumps({"token": token, "env": env or "", "ops": ops})
        started = time.perf_counter()
        with socket_module.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            conn.sendall((payload + "\n").encode())
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        reply = json.loads(buffer.decode() or "{}")
        reply["wall_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return reply

    def click_selectors(self, env: str | None, selectors: list[str],
                        trusted: bool = True, kernel: bool = True) -> dict:
        """Click elements by selector; `trusted` picks the stealthy CDP path.

        trusted=True  -> one evaluate for rects, then CDP Input clicks (isTrusted: true,
                         ~1 ms each)
        trusted=False -> one evaluate that clicks in-page (isTrusted: false, ~0.02 ms each)
        """
        cdp = self._cdp(env)
        payload = json.dumps(selectors)
        if trusted and kernel and self.kernel_available():
            # In-browser forwarding of WebMouseEvent pairs: 0.02 ms/click measured, isTrusted
            # preserved, and no CDP session involved.
            try:
                reply = self.kernel_task(env, [{"clickTrusted": sel} for sel in selectors])
            except Exception as exc:                             # noqa: BLE001
                self._kernel_probe = (time.monotonic(), False)
                return {'ok': False, 'mode': 'kernel', 'outcome_unknown': True,
                        'error': 'click dispatch could not be confirmed; do not repeat automatically: ' + str(exc)[:160]}
            if reply and reply.get("ok"):
                return {"mode": "kernel", "clicked": len(reply.get("results") or []),
                        "ms": reply.get("ms"), "wall_ms": reply.get("wall_ms"),
                        "ms_per_click": (reply.get("ms", 0) /
                                         max(1, len(selectors))),
                        "trusted": True, "ok": True}
            return {'ok': False, 'mode': 'kernel',
                    'error': (reply or {}).get('error', 'kernel click failed')}
        if not trusted:
            started = time.perf_counter()
            raw = cdp.evaluate(self.IN_PAGE_CLICKS_JS % payload, timeout=30)
            elapsed = (time.perf_counter() - started) * 1000
            return {"mode": "in-page", "results": json.loads(raw) if raw else [],
                    "ms": round(elapsed, 2), "trusted": False}

        started = time.perf_counter()
        raw = cdp.evaluate(self.RECTS_JS % payload, timeout=20)
        rects = json.loads(raw) if raw else []
        rect_ms = (time.perf_counter() - started) * 1000
        clicked = 0
        covered = 0
        for point in rects:
            if not point:
                continue
            if point.get("covered"):
                covered += 1
            cdp.send("Input.dispatchMouseEvent",
                     {"type": "mousePressed", "x": point["x"], "y": point["y"],
                      "button": "left", "clickCount": 1})
            cdp.send("Input.dispatchMouseEvent",
                     {"type": "mouseReleased", "x": point["x"], "y": point["y"],
                      "button": "left", "clickCount": 1})
            clicked += 1
        elapsed = (time.perf_counter() - started) * 1000
        return {"mode": "trusted", "clicked": clicked, "covered": covered,
                "rects_ms": round(rect_ms, 2), "ms": round(elapsed, 2),
                "ms_per_click": round(elapsed / clicked, 2) if clicked else 0,
                "trusted": True}

    def goto(self, env: str | None, url: str, wait_ms: int | None = None) -> dict:
        cdp = self._cdp(env)
        started = time.monotonic()
        resolved_env = env or self.env_state().get('active', '')
        self.observations.pop(resolved_env, None)
        navigation = cdp.send("Page.navigate", {"url": url}, timeout=15)['result']
        if navigation.get('errorText') or navigation.get('isDownload'):
            return {'ok': False, 'url': url, 'navigation_sent': True,
                    'error': navigation.get('errorText') or 'navigation started a download'}
        # Explicit waits retain their former minimum-delay semantics.
        if wait_ms is not None:
            time.sleep(max(0, wait_ms) / 1000)
            return {'url': url, 'waited_ms': max(0, wait_ms)}
        expected_loader = navigation.get('loaderId')
        last_error = ''
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                frame = cdp.send('Page.getFrameTree', timeout=2)['result']['frameTree']['frame']
                ready = cdp.evaluate('document.readyState', timeout=2)
                if (not expected_loader or frame.get('loaderId') == expected_loader) and ready in ('interactive', 'complete'):
                    return {'url': frame.get('url', url), 'ready': ready,
                            'waited_ms': round((time.monotonic() - started) * 1000, 2)}
            except RuntimeError as exc:
                # During navigation an execution context can disappear. Polling
                # readiness is safe; the navigation itself is never replayed.
                last_error = str(exc)
            time.sleep(.05)
        return {'ok': False, 'url': url, 'navigation_sent': True,
                'error': 'navigation sent but page readiness was not confirmed',
                'detail': last_error, 'waited_ms': round((time.monotonic() - started) * 1000, 2)}

    def act(self, env: str | None, element_id: str, action: str = "click") -> dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        if observation is None:
            raise RuntimeError(f"no observation for env {resolved_env!r}; call observe first")
        item = observation.by_id(element_id)
        x, y, w, h = item["bbox"]
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        cdp = self._cdp(env)
        if action in ("click", "hover"):
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": cx, "y": cy, "button": "none"})
            if action == "click":
                cdp.send("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": cx, "y": cy,
                    "button": "left", "clickCount": 1})
                cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": cx, "y": cy,
                    "button": "left", "clickCount": 1})
        elif action == "focus":
            cdp.evaluate(f"document.elementFromPoint({cx},{cy})?.focus()")
        else:
            raise ValueError(f"unsupported action {action!r}")
        return {"element": element_id, "action": action, "at": [cx, cy],
                "name": item["name"], "trusted": True}

    def type_text(self, env: str | None, element_id: str, text: str) -> dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        if observation is None:
            raise RuntimeError(f"no observation for env {resolved_env!r}; call observe first")
        item = observation.by_id(element_id)
        x, y, w, h = item["bbox"]
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        cdp = self._cdp(env)
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": cx, "y": cy,
            "button": "left", "clickCount": 1})
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": cx, "y": cy,
            "button": "left", "clickCount": 1})
        for char in text:
            cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": char})
            time.sleep(0.03)
        return {"element": element_id, "typed": text}

    def read(self, env: str | None, selector: str = "body",
             limit: int = 2000) -> dict:
        script = (f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
                  " if (!el) return JSON.stringify({found:false});"
                  f" return JSON.stringify({{found:true, text: el.innerText.slice(0,{limit})}}); }})()")
        cdp = self._cdp(env)
        raw = cdp.evaluate(script, timeout=20)
        return json.loads(raw) if raw else {"found": False}

    def screenshot(self, env: str | None, element_id: str | None = None,
                   fast: bool = True) -> dict:
        """Capture a screenshot.

        `fast=True` uses `fromSurface: false`: the renderer's own surface instead of asking the
        compositor for a fresh frame. Measured 231 ms vs 1473 ms for the default, and adding a
        clip costs nothing (217 ms) -- see design doc §36.
        """
        clip = None
        if element_id:
            resolved_env = env or self.env_state().get("active", "")
            observation = self.observations.get(resolved_env)
            if observation is None:
                raise RuntimeError("screenshot by element id needs a prior observe")
            x, y, w, h = observation.by_id(element_id)["bbox"]
            clip = {"x": x, "y": y, "width": max(8, w), "height": max(8, h), "scale": 1}
        cdp = self._cdp(env)
        params: dict[str, Any] = ({"format": "jpeg", "quality": 60, "fromSurface": False}
                                  if fast else {"format": "png"})
        if clip:
            params["clip"] = clip
        started = time.perf_counter()
        reply = cdp.send("Page.captureScreenshot", params, timeout=30)
        elapsed = (time.perf_counter() - started) * 1000
        self.last_screenshot_ms = round(elapsed, 1)
        data = (reply.get("result") or {}).get("data") or ""
        return {"bytes": len(data) * 3 // 4, "clip": clip,
                "ms": self.last_screenshot_ms, "fast": fast,
                "data_prefix": data[:48]}
