"""Browser side of the bridge: CDP client + environment resolution.

Kept separate from the HTTP server so the same code can be used by probes and tests.
Resolution rule: a target belongs to an environment when its URL carries `?e=<env>`
(how our probes navigate) or, for the active environment, when it is the only visible
page target. Everything else is an error rather than a silent wrong-tab action.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.request
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from _nowindow import no_window
from page_readiness import readiness, wait_for as wait_for_page

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
# Local control traffic must never inherit an Internet/system proxy.
LOOPBACK_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BrowserControlUnavailable(RuntimeError):
    error_code = 'BROWSER_CONTROL_UNAVAILABLE'
    action_sent = False

# Observation ids are only meaningful inside the observation that produced them, and
# only for one environment: that is what stops "looked at A, clicked on B".
@dataclass
class Observation:
    env: str
    epoch: int
    items: list[dict] = field(default_factory=list)
    document_token: str = ""
    summary: dict | None = None

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
                with LOOPBACK_HTTP.open(
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
            raise BrowserControlUnavailable(
                'Browser control endpoint could not be located for this profile. '
                'The browser may still be running; do not infer that it is closed. '
                'Pause browser actions and restore the product connection, rather than '
                'repeatedly reopening environments or falling back to shell commands.')
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
            with LOOPBACK_HTTP.open(url, timeout=8) as response:
                return [t for t in json.loads(response.read().decode())
                        if t.get("type") == "page"]
        except Exception:                                          # noqa: BLE001
            if not retry or self._explicit_port is not None:
                raise
            # The browser was restarted: the cached port points at nothing now.
            type(self)._cached_port = None
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
        target = None
        if cached is not None:
            target_id, cdp = cached
            try:
                strict = getattr(self, "strict_resolution", False)
                if strict:
                    target = self.target_for_env(env)
                    exact = target['id'] == target_id
                else:
                    exact = any(page['id'] == target_id for page in self.targets())
                if exact:
                    if validated is not None:
                        validated[key] = cdp
                    return cdp
            except Exception:                                    # noqa: BLE001
                # An ownership/discovery failure must not be repeated inside
                # the same step or silently select another browser.
                self._drop_session(key)
                raise
            self._drop_session(key)
        target = target or self.target_for_env(env)
        cdp = BrowserCdp(target["webSocketDebuggerUrl"], open_timeout=8)
        cdp.send('Page.enable', timeout=5)
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
  const states = [];
  for (const el of nodes) {
    const text = (el.getAttribute('aria-label') || el.innerText || el.value || '').trim()
        .replace(/\s+/g, ' ').slice(0, 40);
    for (let i = 0; i < text.length; i++) {
      fingerprint = (fingerprint * 31 + text.charCodeAt(i)) >>> 0;
    }
    if (names.length < 40) names.push(text);
    if (states.length < 120) {
      states.push({
        key: el.id || el.getAttribute('name') || el.getAttribute('aria-label') || text,
        tag: el.tagName.toLowerCase(),
        value: 'value' in el ? String(el.value ?? '') : '',
        checked: 'checked' in el ? !!el.checked : el.getAttribute('aria-checked'),
        selected: el.tagName === 'SELECT' ? Array.from(el.selectedOptions).map(option => option.value) : el.getAttribute('aria-selected'),
        expanded: el.getAttribute('aria-expanded'),
      });
    }
  }
  return JSON.stringify({
    url: location.href,
    title: document.title,
    count: nodes.length,
    fingerprint,
    names,
    states,
    ready: document.readyState,
  });
})()
"""

    def snapshot(self, env: str | None = None) -> dict:
        """Cheap page fingerprint used to tell the agent what changed after an action."""
        cdp = self._cdp(env)
        # Optional post-action evidence must not hold a completed action for 15s.
        raw = cdp.evaluate(self.SNAPSHOT_JS, timeout=2)
        data = json.loads(raw) if raw else {}
        if data.get('ready'):
            data['readiness'] = readiness(data['ready'])
        return data

    @staticmethod
    def diff(before: dict, after: dict) -> dict:
        if not before:
            return {"first": True, "url": after.get("url"), "count": after.get("count"),
                    "readiness":after.get('readiness')}
        before_names = set(before.get("names") or [])
        after_names = set(after.get("names") or [])
        before_states = json.dumps(before.get("states") or [], ensure_ascii=False, sort_keys=True)
        after_states = json.dumps(after.get("states") or [], ensure_ascii=False, sort_keys=True)
        return {
            "url_changed": before.get("url") != after.get("url"),
            "title_changed": before.get("title") != after.get("title"),
            "count_delta": (after.get("count") or 0) - (before.get("count") or 0),
            "appeared": sorted(after_names - before_names)[:8],
            "disappeared": sorted(before_names - after_names)[:8],
            "state_changed": before_states != after_states,
            "url": after.get("url"),
            "readiness": after.get("readiness"),
        }

    # --------------------------------------------------------------- observations
    OBSERVE_JS = r"""
(() => {
  const out = [], names = [], idCounts = new Map(), siblingGroups = new WeakMap();
  const summarySelector = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=combobox],[role=listbox],[role=option],[role=menu],[role=menuitem],[role=tree],[role=treeitem],[role=gridcell],[role=spinbutton],[aria-haspopup]';
  const sel = summarySelector + ',[onclick]';
  let count = 0, fingerprint = 0, id = 0;
  function locator(el, root) {
    if (el.id) {
      try { if (root.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id); } catch (_) {}
    }
    const path = [];
    for (let node = el; node && node.nodeType === 1 && node !== root; node = node.parentElement) {
      const parent = node.parentElement;
      if (parent && !siblingGroups.has(parent)) {
        const groups = new Map();
        for (const child of parent.children) {
          if (!groups.has(child.tagName)) groups.set(child.tagName, []);
          groups.get(child.tagName).push(child);
        }
        siblingGroups.set(parent, groups);
      }
      const siblings = parent ? siblingGroups.get(parent).get(node.tagName) : [node];
      path.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')');
    }
    return path.join(' > ');
  }
  function visit(root, framePath, shadowPath, originX, originY) {
    if (!root || out.length >= 120) return;
    let nodes = [];
    try { nodes = Array.from(root.querySelectorAll(sel)); } catch (_) { return; }
    for (const el of nodes) {
      const rawLabel = el.getAttribute('aria-label') || el.getAttribute('name') ||
        el.getAttribute('placeholder') || el.innerText ||
        (el.matches('input,textarea') ? '' : el.value) || '';
      const label = rawLabel.trim().replace(/\s+/g, ' ');
      if (el.matches(summarySelector)) {
        const text = label.slice(0, 40);
        count++;
        for (let i = 0; i < text.length; i++) fingerprint = (fingerprint * 31 + text.charCodeAt(i)) >>> 0;
        if (names.length < 40) names.push(text);
      }
      if (out.length >= 120) break;
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      const cs = getComputedStyle(el);
      const x = r.left + originX, y = r.top + originY;
      const clipped = {left: Math.max(0, x), top: Math.max(0, y), right: Math.min(innerWidth, x + r.width), bottom: Math.min(innerHeight, y + r.height)};
      const visible = cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) > 0 &&
        clipped.right > clipped.left && clipped.bottom > clipped.top &&
        (!el.checkVisibility || el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}));
      if (!visible) continue;
      const cx = (clipped.left + clipped.right) / 2, cy = (clipped.top + clipped.bottom) / 2;
      const hit = root === document && document.elementFromPoint(cx, cy);
      const name = (rawLabel || el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 60);
      const selectState = el.tagName === 'SELECT' ? {
        multiple: el.multiple,
        selected_values: Array.from(el.selectedOptions).map(option => option.value),
        options: Array.from(el.options).slice(0, 120).map(option => ({value: option.value, label: (option.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120), selected: option.selected, disabled: option.disabled}))
      } : {};
      const inputState = el.matches('input[type=checkbox],input[type=radio]') ? {checked: el.checked, value: el.value || ''} : {};
      const menuState = {input_type: el.getAttribute('type'), aria_haspopup: el.getAttribute('aria-haspopup'), aria_expanded: el.getAttribute('aria-expanded'), aria_selected: el.getAttribute('aria-selected'), aria_current: el.getAttribute('aria-current'), aria_level: el.getAttribute('aria-level'), menu_role: el.closest('[role=menu],[role=listbox],[role=tree]')?.getAttribute('role') || null};
      out.push({id:'e' + (id++), tag:el.tagName.toLowerCase(), role:el.getAttribute('role') || (el.tagName === 'A' ? 'link' : el.tagName === 'BUTTON' ? 'button' : ''), name, href:(el.getAttribute('href') || '').slice(0,80), bbox:[Math.round(x),Math.round(y),Math.round(r.width),Math.round(r.height)], point:[cx,cy], in_viewport: clipped.right > clipped.left && clipped.bottom > clipped.top, clickable:cs.pointerEvents !== 'none', occluded: root === document ? !(hit === el || el.contains(hit) || (hit && hit.contains(el))) : false, disabled:el.matches(':disabled') || !!el.closest('[inert],[aria-disabled="true"]'), visual_only:false, selector:locator(el,root), frame_path:framePath, shadow_path:shadowPath, value:'value' in el ? String(el.value ?? '') : '', ...selectState, ...inputState, ...menuState});
    }
    if (out.length >= 120) return;
    let frames = [];
    try { frames = Array.from(root.querySelectorAll('iframe,frame')); } catch (_) {}
    for (const frame of frames) {
      if (out.length >= 120) break;
      try {
        const child = frame.contentDocument;
        if (!child) continue;
        const r = frame.getBoundingClientRect();
        visit(child, framePath.concat(locator(frame,root)), shadowPath, originX + r.left, originY + r.top);
      } catch (_) {}
    }
    let hosts = [];
    try { hosts = Array.from(root.querySelectorAll('*')).filter(el => el.shadowRoot).slice(0, 40); } catch (_) {}
    for (const host of hosts) {
      if (out.length >= 120) break;
      try { visit(host.shadowRoot, framePath, shadowPath.concat(locator(host,root)), originX, originY); } catch (_) {}
    }
  }
  visit(document, [], [], 0, 0);
  return JSON.stringify({url:location.href,title:document.title,ready:document.readyState,document_token:String(performance.timeOrigin),viewport:[innerWidth,innerHeight],dpr:devicePixelRatio,elements:out,_snapshot:{url:location.href,title:document.title,count,fingerprint,names,ready:document.readyState}});
})()
"""

    def observe(self, env: str | None, limit: int = 80) -> dict:
        resolved_env = env or self.env_state().get("active", "")
        self.observations.pop(resolved_env,None)
        cdp = self._cdp(env)
        # One renderer round-trip produces both the observation and its baseline.
        # The snapshot stays internal, rather than duplicating text in tool output.
        raw = cdp.evaluate(self.OBSERVE_JS, timeout=20)
        data = json.loads(raw) if raw else None
        if not isinstance(data,dict) or not isinstance(data.get('elements'),list):
            raise RuntimeError('page observation was not returned; observe again before acting')
        epoch = int(time.time() * 1000)
        observation_id = secrets.token_hex(4)
        items = data.get('elements', [])[:max(1,min(120,limit))]
        for item in items:
            item['id'] = observation_id + ':' + item['id']
        summary = data.pop('_snapshot',None)
        if isinstance(summary,dict) and summary.get('ready'):
            summary['readiness'] = readiness(summary['ready'])
        observation = Observation(env=resolved_env, epoch=epoch,
                                  items=items, document_token=data.get('document_token',''),
                                  summary=summary)
        self.observations[resolved_env] = observation
        data["elements"] = observation.items
        data["env"] = resolved_env
        data["epoch"] = epoch
        data['readiness'] = readiness(data.get('ready','loading'))
        return data

    def wait_for(self, env: str | None, **condition) -> dict:
        return wait_for_page(self._cdp(env), condition)

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
                    timeout: float = 10.0,
                    cursor: dict | None = None) -> dict:
        """Run ops through the in-browser executor (mojom::AutomationTask) over the socket."""
        import socket as socket_module
        port = int(self.KERNEL_PORT_FILE.read_text(encoding="utf-8").strip())
        token = self.KERNEL_TOKEN_FILE.read_text(encoding="utf-8").strip()
        payload = json.dumps({"token": token, "env": env or "", "ops": ops,
                              **({"cursor": cursor} if cursor else {})})
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

    def cursor_event(self, env: str | None, x: float, y: float,
                     action: str = "move") -> bool:
        """Send a best-effort native cursor event without touching the page."""
        if not (self.KERNEL_PORT_FILE.exists() and self.KERNEL_TOKEN_FILE.exists()):
            return False
        # A stale port file is common after an abnormal browser exit. Probe once
        # per cache window instead of waiting 1.5s for every Agent action.
        if not self.kernel_available():
            return False
        try:
            reply = self.kernel_task(env, [], timeout=1.5,
                                     cursor={"x": float(x), "y": float(y),
                                             "action": action})
            # The native layer starts the arc before replying. Wait only for
            # the remaining animation time so the following click, key input,
            # or wheel event lands after the pointer reaches its target.
            cursor_ms = float(reply.get("cursor_ms", 0) or 0)
            elapsed_ms = float(reply.get("wall_ms", 0) or 0)
            remaining_ms = max(0.0, cursor_ms - elapsed_ms)
            if remaining_ms:
                time.sleep(remaining_ms / 1000.0)
            return bool(reply.get("ok", True))
        except Exception:
            self._kernel_probe = (time.monotonic(), False)
            return False

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
            self.cursor_event(env, point["x"], point["y"], "click")
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
        # Navigation is a browser-control action even when the Agent chooses a
        # direct URL instead of clicking a page link. Move the native pointer
        # to a stable viewport anchor before dispatching the real navigation.
        try:
            raw_anchor = cdp.evaluate(
                "JSON.stringify({x: innerWidth / 2, y: innerHeight / 2})")
            anchor = json.loads(raw_anchor or "{}")
            self.cursor_event(env, float(anchor.get("x", 0)),
                              float(anchor.get("y", 0)), "navigate")
        except Exception:
            pass
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
                            'readiness':readiness(ready),
                            'next_action':'observe; wait_for the required target only if it is not ready',
                            'waited_ms': round((time.monotonic() - started) * 1000, 2)}
            except RuntimeError as exc:
                # During navigation an execution context can disappear. Polling
                # readiness is safe; the navigation itself is never replayed.
                last_error = str(exc)
            time.sleep(.05)
        return {'ok': False, 'url': url, 'navigation_sent': True,
                'error': 'navigation sent but page readiness was not confirmed',
                'detail': last_error, 'waited_ms': round((time.monotonic() - started) * 1000, 2)}

    def new_tab(self, env: str | None, url: str | None = None) -> dict:
        import env_cli
        target_env = env or self.env_state().get('active', '')
        command = {'command': 'newtab', 'env_id': target_env}
        if url:
            command['url'] = url
        result = env_cli.send(env_cli.resolve_profile_dir(PROFILE), command, 20)
        return {**result, 'env': target_env, 'url': url or ''}

    def switch_tab(self, env: str | None, index: int) -> dict:
        import env_cli
        target_env = env or self.env_state().get('active', '')
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return {'ok':False,'error_code':'INVALID_TAB_INDEX','error':'tab index must be a non-negative integer.'}
        result = env_cli.send(env_cli.resolve_profile_dir(PROFILE),
                              {'command':'selecttab','env_id':target_env,'index':index}, 20)
        return {**result, 'env': target_env, 'index': index}

    def close_tab(self, env: str | None) -> dict:
        target = self.target_for_env(env)
        cdp = self._cdp(env)
        result = cdp.send('Page.close', timeout=5)
        self._drop_session(env or self.env_state().get('active', ''))
        return {'ok': True, 'env': env or self.env_state().get('active', ''),
                'target_id': target.get('id',''), 'closed': result.get('result') is not None}

    def pending_dialog(self, env: str | None) -> dict | None:
        cdp = self._cdp(env)
        events = cdp.pop_events()
        for event in events:
            if event.get('method') == 'Page.javascriptDialogOpening':
                params = event.get('params') or {}
                return {'dialog_open': True,
                        'dialog_type': params.get('type',''),
                        'message': params.get('message',''),
                        'default_prompt': params.get('defaultPrompt','')}
        return None

    def handle_dialog(self, env: str | None, accept: bool = True,
                      prompt_text: str | None = None) -> dict:
        cdp = self._cdp(env)
        params = {'accept': bool(accept)}
        if prompt_text is not None:
            params['promptText'] = str(prompt_text)
        result = cdp.send('Page.handleJavaScriptDialog', params, timeout=5)
        if result.get('error'):
            return {'ok':False,'error_code':'DIALOG_NOT_OPEN',
                    'error':result['error'].get('message','dialog is not open')}
        return {'ok':True,'accepted':bool(accept),'prompt_text':prompt_text,
                'trusted':True}

    def act(self, env: str | None, element_id: str, action: str = "click") -> dict:
        if action not in ('click', 'hover', 'focus'):
            raise ValueError(f"unsupported action {action!r}")
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                    'error':'Element id is not from the latest observation; observe again.',
                    'next_action':'observe'}
        if item.get('selector') and observation.document_token:
            check = self.wait_for(env, selector=item['selector'], document_token=observation.document_token,
                identity={key:item.get(key,'') for key in ('tag','name','href')},
                state='actionable', timeout_ms=2000,
                frame_path=item.get('frame_path', []), shadow_path=item.get('shadow_path', []))
            if not check.get('ok'):
                return check
            item = {**item, 'bbox':check['state']['box'], 'point':check['state'].get('point')}
        x, y, w, h = item["bbox"]
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        if item.get('point'):
            cx, cy = item['point']
        cdp = self._cdp(env)
        if action in ("click", "hover"):
            self.cursor_event(env, cx, cy, "click" if action == "click" else "move")
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
            self.cursor_event(env, cx, cy, "focus")
            cdp.evaluate(f"document.elementFromPoint({cx},{cy})?.focus()")
        else:
            raise ValueError(f"unsupported action {action!r}")
        return {"element": element_id, "action": action, "at": [cx, cy],
                "name": item["name"], "trusted": True}

    @staticmethod
    def _key_definition(token: str) -> tuple[str, str, int, int]:
        names = {
            'CTRL': ('Control', 'ControlLeft', 17, 2), 'CONTROL': ('Control', 'ControlLeft', 17, 2),
            'SHIFT': ('Shift', 'ShiftLeft', 16, 8), 'ALT': ('Alt', 'AltLeft', 18, 1),
            'META': ('Meta', 'MetaLeft', 91, 4), 'ENTER': ('Enter', 'Enter', 13, 0),
            'TAB': ('Tab', 'Tab', 9, 0), 'BACKSPACE': ('Backspace', 'Backspace', 8, 0),
            'DELETE': ('Delete', 'Delete', 46, 0), 'ESC': ('Escape', 'Escape', 27, 0),
            'ESCAPE': ('Escape', 'Escape', 27, 0), 'SPACE': (' ', 'Space', 32, 0),
            'ARROWUP': ('ArrowUp', 'ArrowUp', 38, 0), 'ARROWDOWN': ('ArrowDown', 'ArrowDown', 40, 0),
            'ARROWLEFT': ('ArrowLeft', 'ArrowLeft', 37, 0), 'ARROWRIGHT': ('ArrowRight', 'ArrowRight', 39, 0),
            'HOME': ('Home', 'Home', 36, 0), 'END': ('End', 'End', 35, 0),
            'PAGEUP': ('PageUp', 'PageUp', 33, 0), 'PAGEDOWN': ('PageDown', 'PageDown', 34, 0),
        }
        upper = token.upper()
        if upper in names:
            return names[upper]
        if len(token) == 1:
            code = 'Key' + token.upper() if token.isalpha() else 'Digit' + token
            return token, code, ord(token.upper()), 0
        raise ValueError(f'unsupported key token: {token}')

    def _dispatch_keys(self, cdp: Cdp, keys: list[str]) -> None:
        for raw in keys:
            modifiers = 0
            held: list[tuple[str, str, int, int]] = []
            tokens = [part for part in raw.split('+') if part] if '+' in raw else [raw]
            for token in tokens:
                key, code, virtual_key, bit = self._key_definition(token)
                if bit:
                    if not any(item[2] == virtual_key for item in held):
                        cdp.send('Input.dispatchKeyEvent', {'type':'keyDown','key':key,'code':code,
                            'windowsVirtualKeyCode':virtual_key,'nativeVirtualKeyCode':virtual_key,
                            'modifiers':modifiers | bit})
                        held.append((key, code, virtual_key, bit)); modifiers |= bit
                    continue
                cdp.send('Input.dispatchKeyEvent', {'type':'keyDown','key':key,'code':code,
                    'windowsVirtualKeyCode':virtual_key,'nativeVirtualKeyCode':virtual_key,
                    'modifiers':modifiers, **({'text':token} if len(token) == 1 else {})})
                cdp.send('Input.dispatchKeyEvent', {'type':'keyUp','key':key,'code':code,
                    'windowsVirtualKeyCode':virtual_key,'nativeVirtualKeyCode':virtual_key,
                    'modifiers':modifiers})
            for key, code, virtual_key, bit in reversed(held):
                modifiers &= ~bit
                cdp.send('Input.dispatchKeyEvent', {'type':'keyUp','key':key,'code':code,
                    'windowsVirtualKeyCode':virtual_key,'nativeVirtualKeyCode':virtual_key,
                    'modifiers':modifiers})

    def _prepare_editable(self, env: str | None, element_id: str,
                          editable: bool = True) -> tuple[dict, Cdp] | dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                    'error':'Element id is not from the latest observe; observe again before editing.',
                    'next_action':'observe'}
        if item.get('selector') and observation.document_token:
            check = self.wait_for(env, selector=item['selector'], document_token=observation.document_token,
                identity={key:item.get(key,'') for key in ('tag','name','href')},
                state='actionable', editable=editable, timeout_ms=2000,
                frame_path=item.get('frame_path', []), shadow_path=item.get('shadow_path', []))
            if not check.get('ok'):
                return check
            item = {**item, 'bbox':check['state']['box'], 'point':check['state'].get('point')}
        return item, self._cdp(env)

    @staticmethod
    def _resolve_element_expression(selector: str, item: dict, body: str) -> str:
        frames = json.dumps(item.get('frame_path', []), ensure_ascii=False)
        shadows = json.dumps(item.get('shadow_path', []), ensure_ascii=False)
        return ("(() => { let root=document; for (const s of " + frames + ") {"
                "const frame=root && root.querySelector(s); root=frame && frame.contentDocument;"
                "if(!root) return null; } for (const s of " + shadows + ") {"
                "const host=root && root.querySelector(s); root=host && host.shadowRoot;"
                "if(!root) return null; } const el=root.querySelector(" + json.dumps(selector) + ");"
                "if(!el) return null; " + body + " })()")

    def type_text(self, env: str | None, element_id: str, text: str,
                  replace: bool = False) -> dict:
        prepared = self._prepare_editable(env, element_id)
        if isinstance(prepared, dict):
            return prepared
        item, cdp = prepared
        x, y, w, h = item["bbox"]
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        if item.get('point'):
            cx, cy = item['point']
        self.cursor_event(env, cx, cy, "type")
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": cx, "y": cy,
            "button": "left", "clickCount": 1})
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": cx, "y": cy,
            "button": "left", "clickCount": 1})
        if replace:
            self._dispatch_keys(cdp, ['CTRL+A', 'BACKSPACE'])
        for char in text:
            cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": char})
            time.sleep(0.03)
        return {"element": element_id, "typed": text, "replaced": replace,
                "trusted": True}

    def clear_text(self, env: str | None, element_id: str) -> dict:
        prepared = self._prepare_editable(env, element_id)
        if isinstance(prepared, dict):
            return prepared
        item, cdp = prepared
        x, y, w, h = item['bbox']
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        if item.get('point'):
            cx, cy = item['point']
        self.cursor_event(env, cx, cy, 'clear')
        cdp.send('Input.dispatchMouseEvent', {'type':'mousePressed','x':cx,'y':cy,'button':'left','clickCount':1})
        cdp.send('Input.dispatchMouseEvent', {'type':'mouseReleased','x':cx,'y':cy,'button':'left','clickCount':1})
        self._dispatch_keys(cdp, ['CTRL+A', 'BACKSPACE'])
        return {'element': element_id, 'cleared': True, 'trusted': True}

    def send_keys(self, env: str | None, element_id: str | None,
                  keys: list[str]) -> dict:
        if not isinstance(keys, list) or not keys:
            return {'ok': False, 'action_sent': False, 'error_code': 'INVALID_KEYS',
                    'error': 'key.keys must be a non-empty array.'}
        cdp = self._cdp(env)
        if element_id:
            prepared = self._prepare_editable(env, element_id, editable=False)
            if isinstance(prepared, dict):
                return prepared
            item, cdp = prepared
            x, y, w, h = item['bbox']
            cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
            if item.get('point'):
                cx, cy = item['point']
            self.cursor_event(env, cx, cy, 'key')
            cdp.send('Input.dispatchMouseEvent', {'type':'mousePressed','x':cx,'y':cy,'button':'left','clickCount':1})
            cdp.send('Input.dispatchMouseEvent', {'type':'mouseReleased','x':cx,'y':cy,'button':'left','clickCount':1})
        self._dispatch_keys(cdp, keys)
        return {'ok': True, 'element': element_id, 'keys': keys, 'trusted': True}

    def set_checked(self, env: str | None, element_id: str, checked: bool) -> dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                    'error':'Element id is not from the latest observe; observe again before changing the check state.',
                    'next_action':'observe'}
        if item.get('tag') != 'input' or item.get('input_type') not in ('checkbox', 'radio'):
            return {'ok':False,'action_sent':False,'error_code':'CHECK_CONTROL_REQUIRED',
                    'error':'check works with native checkbox or radio controls.'}
        current = bool(item.get('checked'))
        if current == bool(checked):
            return {'ok':True,'element':element_id,'checked':current,'changed':False,'trusted':True}
        result = self.act(env, element_id, 'click')
        if result.get('ok') is False:
            return result
        cdp = self._cdp(env)
        state = cdp.evaluate(self._resolve_element_expression(
            item['selector'], item, "return JSON.stringify({checked:!!el.checked});"), timeout=5)
        value = json.loads(state or '{}').get('checked')
        if value != bool(checked):
            return {'ok':False,'action_sent':True,'error_code':'CHECK_STATE_NOT_APPLIED',
                    'error':'The control did not reach the requested state.',
                    'checked':value,'expected':bool(checked),'outcome_unknown':True,
                    'retry_hint':'Observe the control before retrying.'}
        return {'ok':True,'element':element_id,'checked':value,'changed':True,'trusted':True}

    def set_range(self, env: str | None, element_id: str, value: float) -> dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                    'error':'Element id is not from the latest observe; observe again before setting the range.',
                    'next_action':'observe'}
        if item.get('tag') != 'input' or item.get('input_type') != 'range':
            return {'ok':False,'action_sent':False,'error_code':'RANGE_CONTROL_REQUIRED',
                    'error':'range works with native input type range controls.'}
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {'ok':False,'action_sent':False,'error_code':'INVALID_RANGE_VALUE',
                    'error':'range.value must be a number.'}
        selector = item.get('selector')
        cdp = self._cdp(env)
        state = cdp.evaluate(self._resolve_element_expression(
            selector, item, "if(el.type!=='range') return {}; const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; setter.call(el,String(" +
            json.dumps(value) + ")); el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return JSON.stringify({value:el.value,min:el.min,max:el.max,step:el.step});"), timeout=5)
        result = json.loads(state or '{}') if state else {}
        if result.get('value') is None or result.get('value') == '':
            return {'ok':False,'action_sent':True,'error_code':'RANGE_NOT_APPLIED',
                    'error':'The browser rejected the requested range value.',
                    'outcome_unknown':True}
        return {'ok':True,'element':element_id,'value':result['value'],
                'min':result.get('min'),'max':result.get('max'),'step':result.get('step'),
                'trusted':False,'mode':'native-form-events'}

    def scroll_into_view(self, env: str | None, element_id: str | None = None,
                         selector: str | None = None) -> dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        item = None
        if element_id:
            try:
                if observation is None:
                    raise KeyError(element_id)
                item = observation.by_id(element_id)
            except KeyError:
                return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                        'error':'Element id is not from the latest observe; observe again before scrolling.',
                        'next_action':'observe'}
            selector = item.get('selector')
        if not selector:
            return {'ok':False,'action_sent':False,'error_code':'SCROLL_TARGET_REQUIRED',
                    'error':'Provide an observed id or a CSS selector to scroll into view.'}
        cdp = self._cdp(env)
        raw = cdp.evaluate("JSON.stringify((() => { const el=document.querySelector(" +
            json.dumps(selector) + "); if(!el) return null; el.scrollIntoView({block:'center',inline:'center',behavior:'instant'}); const r=el.getBoundingClientRect(); return {box:[r.x,r.y,r.width,r.height],point:[r.left+r.width/2,r.top+r.height/2]}; })())", timeout=5)
        result = json.loads(raw or 'null') if raw else None
        if not result:
            return {'ok':False,'action_sent':False,'error_code':'TARGET_NOT_FOUND',
                    'error':'The observed element is no longer in the document.',
                    'next_action':'observe'}
        return {'ok':True,'element':element_id,'selector':selector,
                'box':result['box'],'point':result['point']}

    def upload_file(self, env: str | None, element_id: str, path: str) -> dict:
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION',
                    'error':'Element id is not from the latest observe; observe again before uploading.',
                    'next_action':'observe'}
        if item.get('tag') != 'input' or item.get('input_type') != 'file':
            return {'ok':False,'action_sent':False,'error_code':'FILE_INPUT_REQUIRED',
                    'error':'upload_file requires an input type file control.'}
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            return {'ok':False,'action_sent':False,'error_code':'FILE_NOT_FOUND',
                    'error':'The selected upload file does not exist.'}
        cdp = self._cdp(env)
        root = cdp.send('DOM.getDocument', {'depth': 1})['result']['root']['nodeId']
        node = cdp.send('DOM.querySelector', {'nodeId': root, 'selector': item['selector']})['result'].get('nodeId')
        if not node:
            return {'ok':False,'action_sent':False,'error_code':'FILE_INPUT_NOT_FOUND',
                    'error':'The upload control is no longer in the document.',
                    'next_action':'observe'}
        cdp.send('DOM.setFileInputFiles', {'nodeId':node,'files':[str(file_path)]})
        return {'ok':True,'element':element_id,'file_name':file_path.name,
                'file_count':1,'trusted':False,'mode':'browser-file-picker'}

    def select_options(self, env: str | None, element_id: str,
                       values: Any = None) -> dict:
        """Select one or more native HTML options through trusted keyboard input.

        The page is inspected for the current option list, then the focused
        native select is driven with Home, ArrowDown, and Ctrl+Space. This
        keeps input events in Chromium's normal trusted path and works for
        collapsed single-selects as well as multi-selects without opening a
        platform popup that the Agent cannot observe.
        """
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'STALE_OBSERVATION',
                    'error': 'Element id is not from the latest observation; observe again before selecting.',
                    'next_action': 'observe'}
        if item.get('tag') != 'select':
            return {'ok': False, 'action_sent': False,
                    'error_code': 'SELECT_CONTROL_REQUIRED',
                    'error': 'select works with native HTML select controls. For a custom dropdown, act on its combobox, observe again, then act on the option.',
                    'next_action': 'act_then_observe'}
        selector = item.get('selector')
        if not selector:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'SELECTOR_UNAVAILABLE',
                    'error': 'The select control has no stable selector; observe again.',
                    'next_action': 'observe'}
        if values is None:
            values = []
        if isinstance(values, str):
            requested = [values]
        elif isinstance(values, list):
            requested = [str(value) for value in values]
        else:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'INVALID_SELECT_VALUES',
                    'error': 'select.values must be a string or an array of strings.'}
        requested = list(dict.fromkeys(requested))
        cdp = self._cdp(env)
        if observation.document_token:
            check = self.wait_for(env, selector=selector,
                                  document_token=observation.document_token,
                                  state='actionable', timeout_ms=2000,
                                  frame_path=item.get('frame_path', []), shadow_path=item.get('shadow_path', []))
            if not check.get('ok'):
                return check
        raw = cdp.evaluate(self._resolve_element_expression(
            selector, item, "if(el.tagName !== 'SELECT') return ''; return JSON.stringify({multiple: el.multiple, options: Array.from(el.options).map(o => ({value:o.value,label:(o.textContent||'').trim().replace(/\\s+/g,' '),disabled:o.disabled})), selected:Array.from(el.selectedOptions).map(o=>o.value)});"), timeout=5)
        state = json.loads(raw or '{}') if raw else {}
        options = state.get('options') or []
        if not options:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'SELECT_OPTIONS_UNAVAILABLE',
                    'error': 'The select control has no available options.'}
        multiple = bool(state.get('multiple'))
        if not multiple and len(requested) != 1:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'SINGLE_SELECT_VALUE_COUNT',
                    'error': 'This control accepts exactly one option value.'}
        wanted = []
        missing = []
        for value in requested:
            exact = next((i for i, option in enumerate(options)
                          if option.get('value') == value and not option.get('disabled')), None)
            if exact is None:
                folded = value.casefold()
                exact = next((i for i, option in enumerate(options)
                              if str(option.get('label', '')).casefold() == folded
                              and not option.get('disabled')), None)
            if exact is None:
                missing.append(value)
            else:
                wanted.append(exact)
        if missing:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'SELECT_OPTION_NOT_FOUND',
                    'error': 'Option value or label not found: ' + ', '.join(missing),
                    'options': [{'value': option.get('value', ''),
                                 'label': option.get('label', '')}
                                for option in options if not option.get('disabled')]}

        x, y, w, h = item['bbox']
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        if item.get('point'):
            cx, cy = item['point']
        self.cursor_event(env, cx, cy, 'select')
        for event_type in ('mousePressed', 'mouseReleased'):
            cdp.send('Input.dispatchMouseEvent', {
                'type': event_type, 'x': cx, 'y': cy,
                'button': 'left', 'clickCount': 1})

        def key(key_name: str, code: str, virtual_key: int,
                modifiers: int = 0) -> None:
            for event_type in ('keyDown', 'keyUp'):
                cdp.send('Input.dispatchKeyEvent', {
                    'type': event_type, 'key': key_name, 'code': code,
                    'windowsVirtualKeyCode': virtual_key,
                    'nativeVirtualKeyCode': virtual_key,
                    'modifiers': modifiers})

        key('Home', 'Home', 36)
        if multiple:
            if 0 not in wanted:
                key(' ', 'Space', 32, 2)
            for index in range(1, len(options)):
                key('ArrowDown', 'ArrowDown', 40, 2)
                if index in wanted:
                    key(' ', 'Space', 32, 2)
        else:
            for _ in range(wanted[0] if wanted else 0):
                key('ArrowDown', 'ArrowDown', 40)
        key('Enter', 'Enter', 13)
        # Input.dispatchKeyEvent acknowledges the browser-side enqueue before
        # the renderer runs the select's default action. Yield one renderer
        # task so the selection and its trusted input/change events settle
        # before reading the result.
        cdp.evaluate('new Promise(resolve => setTimeout(resolve, 0))',
                     timeout=5, await_promise=True)
        expected_indexes = wanted if multiple else [wanted[0] if wanted else 0]
        expected_values = [options[index].get('value', '')
                           for index in expected_indexes]
        selected = []
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            selected_raw = cdp.evaluate(self._resolve_element_expression(
                selector, item, "return JSON.stringify(Array.from(el.selectedOptions).map(o=>({value:o.value,label:(o.textContent||'').trim()})));"), timeout=5)
            selected = json.loads(selected_raw or '[]') if selected_raw else []
            if [option.get('value', '') for option in selected] == expected_values:
                break
            time.sleep(0.01)
        mode = 'keyboard'
        trusted = True
        if [option.get('value', '') for option in selected] != expected_values:
            # Platform select popups can consume keyboard focus before the
            # renderer receives the final selection, especially for collapsed
            # multi-selects. Apply the same selection through the native
            # HTMLSelectElement state and dispatch the standard form events as
            # a deterministic fallback for React and other controlled forms.
            fallback = (
                "const wanted = new Set(" +
                json.dumps(expected_values) + "); Array.from(el.options).forEach(o => {"
                "o.selected = wanted.has(o.value) && !o.disabled; });"
                "el.dispatchEvent(new Event('input', {bubbles:true}));"
                "el.dispatchEvent(new Event('change', {bubbles:true}));"
                "return JSON.stringify(Array.from(el.selectedOptions).map(o => ({"
                "value:o.value,label:(o.textContent || '').trim()})));" )
            fallback = self._resolve_element_expression(selector, item, fallback)
            selected_raw = cdp.evaluate(fallback, timeout=5)
            selected = json.loads(selected_raw or '[]') if selected_raw else []
            mode = 'dom-events'
            trusted = False
        if [option.get('value', '') for option in selected] != expected_values:
            return {'ok': False, 'action_sent': True,
                    'error_code': 'SELECT_NOT_APPLIED',
                    'error': 'The page did not apply the requested selection.',
                    'selected': selected,
                    'expected': expected_values,
                    'outcome_unknown': True,
                    'retry_hint': 'Observe the select again before retrying.'}
        return {'ok': True, 'element': element_id, 'multiple': multiple,
                'selected': selected, 'trusted': trusted, 'mode': mode}

    def set_date_time(self, env: str | None, element_id: str,
                      value: str) -> dict:
        """Set a native date/time input using its HTML value format."""
        resolved_env = env or self.env_state().get("active", "")
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            item = observation.by_id(element_id)
        except KeyError:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'STALE_OBSERVATION',
                    'error': 'Element id is not from the latest observation; observe again before setting date or time.',
                    'next_action': 'observe'}
        input_type = item.get('input_type') or ''
        patterns = {
            'date': r'^\d{4}-\d{2}-\d{2}$',
            'time': r'^\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?$',
            'datetime-local': r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?$',
            'month': r'^\d{4}-\d{2}$',
            'week': r'^\d{4}-W\d{2}$',
        }
        if input_type not in patterns:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'DATE_TIME_CONTROL_REQUIRED',
                    'error': 'date_time works with native date, time, datetime-local, month, or week inputs.'}
        if not isinstance(value, str) or not re.fullmatch(patterns[input_type], value):
            return {'ok': False, 'action_sent': False,
                    'error_code': 'INVALID_DATE_TIME_VALUE',
                    'error': f'Use the HTML {input_type} value format.'}
        selector = item.get('selector')
        cdp = self._cdp(env)
        if observation.document_token:
            check = self.wait_for(env, selector=selector,
                                  document_token=observation.document_token,
                                  state='actionable', timeout_ms=2000,
                                  frame_path=item.get('frame_path', []), shadow_path=item.get('shadow_path', []))
            if not check.get('ok'):
                return check
        x, y, w, h = item['bbox']
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        if item.get('point'):
            cx, cy = item['point']
        self.cursor_event(env, cx, cy, 'date-time')
        for event_type in ('mousePressed', 'mouseReleased'):
            cdp.send('Input.dispatchMouseEvent', {
                'type': event_type, 'x': cx, 'y': cy,
                'button': 'left', 'clickCount': 1})
        script = (
            "if (el.tagName !== 'INPUT' || el.type !== " + json.dumps(input_type) + ") return ''; const setter = "
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;"
            "setter.call(el, " + json.dumps(value) + ");"
            "el.dispatchEvent(new Event('input', {bubbles:true}));"
            "el.dispatchEvent(new Event('change', {bubbles:true}));"
            "el.blur(); return JSON.stringify({value:el.value,valid:el.value === " +
            json.dumps(value) + "});")
        script = self._resolve_element_expression(selector, item, script)
        raw = cdp.evaluate(script, timeout=5)
        state = json.loads(raw or '{}') if raw else {}
        if state.get('value') != value:
            return {'ok': False, 'action_sent': True,
                    'error_code': 'DATE_TIME_NOT_APPLIED',
                    'error': 'The browser rejected the supplied date or time value.',
                    'value': state.get('value', ''), 'outcome_unknown': True,
                    'retry_hint': 'Observe the control and verify its min, max, and step constraints.'}
        return {'ok': True, 'element': element_id, 'input_type': input_type,
                'value': value, 'trusted': False, 'mode': 'native-form-events'}

    def menu_select(self, env: str | None, element_id: str,
                    path: list[str], timeout_ms: int = 10000) -> dict:
        """Click a trigger and then each visible item in a cascading menu path."""
        resolved_env = env or self.env_state().get('active', '')
        observation = self.observations.get(resolved_env)
        try:
            if observation is None:
                raise KeyError(element_id)
            trigger = observation.by_id(element_id)
        except KeyError:
            return {'ok': False, 'action_sent': False,
                    'error_code': 'STALE_OBSERVATION',
                    'error': 'Element id is not from the latest observation; observe again before opening the menu.',
                    'next_action': 'observe'}
        if not isinstance(path, list) or not path or any(not isinstance(label, str) or not label.strip() for label in path):
            return {'ok': False, 'action_sent': False,
                    'error_code': 'INVALID_MENU_PATH',
                    'error': 'menu_select.path must contain one or more non-empty labels.'}
        if not trigger.get('selector'):
            return {'ok': False, 'action_sent': False,
                    'error_code': 'MENU_TRIGGER_UNAVAILABLE',
                    'error': 'The menu trigger has no stable selector; observe again.',
                    'next_action': 'observe'}
        cdp = self._cdp(env)
        if observation.document_token:
            check = self.wait_for(env, selector=trigger['selector'],
                                  document_token=observation.document_token,
                                  state='actionable', timeout_ms=2000)
            if not check.get('ok'):
                return check
        x, y, w, h = trigger['bbox']
        cx, cy = x + max(1, w) // 2, y + max(1, h) // 2
        if trigger.get('point'):
            cx, cy = trigger['point']
        self.cursor_event(env, cx, cy, 'menu')
        for event_type in ('mousePressed', 'mouseReleased'):
            cdp.send('Input.dispatchMouseEvent', {
                'type': event_type, 'x': cx, 'y': cy,
                'button': 'left', 'clickCount': 1})

        matcher = (
            "(label => { const wanted = label.trim().replace(/\\s+/g, ' ').toLocaleLowerCase();"
            " const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);"
            " return r.width >= 4 && r.height >= 4 && r.bottom > 0 && r.right > 0 &&"
            " r.top < innerHeight && r.left < innerWidth && s.visibility !== 'hidden' &&"
            " s.display !== 'none' && s.pointerEvents !== 'none'; };"
            " const nodes = [...document.querySelectorAll('[role=menuitem],[role=option],[role=treeitem],"
            "[data-menu-item],li,[role=gridcell],button,a')].filter(visible);"
            " const exact = nodes.filter(el => (el.innerText || el.textContent || '').trim()"
            ".replace(/\\s+/g, ' ').toLocaleLowerCase() === wanted);"
            " const el = exact[exact.length - 1]; if (!el) return null; const r = el.getBoundingClientRect();"
            " return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2,label:(el.innerText||el.textContent||'').trim(),"
            "expanded:el.getAttribute('aria-expanded')}); })")
        timeout = max(1000, min(30000, int(timeout_ms))) / 1000
        chosen = []
        for label in path:
            deadline = time.monotonic() + timeout
            state = None
            while time.monotonic() < deadline:
                raw = cdp.evaluate(matcher + "(" + json.dumps(label) + ")", timeout=5)
                state = json.loads(raw) if raw else None
                if state:
                    break
                time.sleep(0.05)
            if not state:
                return {'ok': False, 'action_sent': True,
                        'error_code': 'MENU_ITEM_NOT_FOUND',
                        'error': 'Menu item not visible: ' + label,
                        'path': chosen + [label],
                        'next_action': 'observe'}
            self.cursor_event(env, state['x'], state['y'], 'menu')
            for event_type in ('mousePressed', 'mouseReleased'):
                cdp.send('Input.dispatchMouseEvent', {
                    'type': event_type, 'x': state['x'], 'y': state['y'],
                    'button': 'left', 'clickCount': 1})
            chosen.append(state.get('label') or label)
            cdp.evaluate('new Promise(resolve => setTimeout(resolve, 0))',
                         timeout=5, await_promise=True)
        return {'ok': True, 'element': element_id, 'path': chosen,
                'trusted': True, 'mode': 'cascading-menu'}

    def scroll(self, env: str | None, delta_x: float = 0,
               delta_y: float = 640, x: float | None = None,
               y: float | None = None) -> dict:
        cdp = self._cdp(env)
        if x is None or y is None:
            point = cdp.evaluate(
                "JSON.stringify({x: innerWidth / 2, y: innerHeight / 2})")
            viewport = json.loads(point or "{}")
            x = float(viewport.get("x", 0))
            y = float(viewport.get("y", 0))
        self.cursor_event(env, x, y, "scroll")
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": float(delta_x), "deltaY": float(delta_y)})
        return {"at": [x, y], "delta_x": delta_x, "delta_y": delta_y}

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
