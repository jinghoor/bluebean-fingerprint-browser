"""lan_bridge — the only component that talks to both DSH and the browser.

P0 surface:
  GET  /v1/health            liveness + capability flags
  GET  /v1/envs              environments with their live tabs
  POST /v1/task              {task_id?, env|targets, mode?, steps:[...]} -> results
  POST /v1/chat              {ask, session_id?} -> assistant text (proxies the SDK runtime)
  POST /v1/ping              tiny tool used by the DSH plugin to prove the chain

Auth: bearer token from `<workspace>/.lan-bridge.json`, generated on first start.
Every action lands in `logs/agent-audit.jsonl` (one line per step, per environment).
"""

from __future__ import annotations

import argparse
import json
from _nowindow import no_window
import logging
import os
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from _runtime_state import atomic_json

sys.path.insert(0, str(Path(__file__).parent))
from bluebean_paths import APP_ROOT, DATA_ROOT, PROFILE_DIR
from browser_backend import BrowserBackend                           # noqa: E402
from dsh_sessions import SessionManager                              # noqa: E402
from session_browser_scope import SessionBrowserScopes               # noqa: E402
from bridge_runtime import source_revision

# Capture once at import. Reading the disk on each health request would make an
# old interpreter falsely claim it has loaded newly edited modules.
LOADED_RUNTIME_REVISION = source_revision(Path(__file__).resolve().parent)

LOG = logging.getLogger("lan_bridge")
PROTOCOL_VERSION = "1"
TOKEN_FILE = DATA_ROOT / ".lan-bridge.json"
AUDIT_FILE = DATA_ROOT / "logs" / "agent-audit.jsonl"

# Permission preset per §18 of the design doc; `full` is the operator's choice, so the
# bridge allows writes and only keeps the audit trail and the kill switch.
DEFAULT_PERMISSIONS = {
    "preset": "full",
    "envs": "any",
    "browser": {"read": "allow", "navigate": "allow", "click": "allow",
                "type": "allow", "env_activate": "allow", "shot": "allow"},
    "max_parallel": 4,
    "audit": True,
    "kill_switch": False,
    "approval_timeout_s": 120,
}


def load_token(port_hint: int) -> tuple[int, str]:
    if TOKEN_FILE.exists():
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return port_hint, data["token"]
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(json.dumps({"port": port_hint, "token": token},
                                     indent=1), encoding="utf-8")
    return port_hint, token



MUTATING_STEPS = {"act", "type", "goto", "clicks", "batch"}


def needs_approval(preset: str, step: str) -> bool:
    """`full` never asks; `workspace` asks before anything that changes the page."""
    return preset != "full" and step in MUTATING_STEPS


def audit(record: dict) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def validate_proxy_config(value: str) -> str:
    """Validate Chromium's per-environment proxy URL form; never accept userinfo."""
    proxy = value.strip()
    if proxy.lower() in ("", "direct", "system"):
        return proxy.lower()
    try:
        parsed = urlsplit(proxy)
    except ValueError as exc:
        raise ValueError("代理地址格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
        raise ValueError("代理协议仅支持 http、https、socks4、socks5、direct 或 system")
    if (not parsed.hostname or any(ch.isspace() for ch in parsed.hostname) or
            parsed.username is not None or parsed.password is not None):
        raise ValueError("代理地址需要主机名和端口；当前版本不支持代理用户名/密码认证")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("代理端口必须是 1–65535")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址只能包含协议、主机和端口")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme.lower()}://{host}:{port}"


class Bridge:
    def __init__(self) -> None:
        self.backend = BrowserBackend()
        self.snapshots: dict[str, dict] = {}
        self.udd = PROFILE_DIR
        self.approval_seq = 0
        self.sessions = SessionManager(DATA_ROOT / ".dsh-sdk")
        self.permissions = dict(DEFAULT_PERMISSIONS)
        from environment_status import AgentActivity
        self.agent_activity = AgentActivity()
        self.browser_scopes = SessionBrowserScopes(
            DATA_ROOT / ".session-browser-scopes.json")
        from local_account_vault import LocalAccountVault
        profile_dir = self.udd / "Default"
        if not profile_dir.is_dir():
            profile_dir = self.udd
        self.account_vault = LocalAccountVault(profile_dir / "env-account-vault.dpapi.json")
        from env_management import EnvironmentManagement
        self.env_management = EnvironmentManagement(self)
        # Switching a parked environment changes the shared native window.
        self.execution_lock = threading.RLock()
        perm_file = DATA_ROOT / "lan-permissions.json"
        if perm_file.exists():
            try:
                self.permissions.update(json.loads(perm_file.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass

    # ------------------------------------------------------------------ steps

    # ------------------------------------------------------------------ approvals
    def ask_approval(self, env: str | None, step: str, args: dict) -> tuple[bool, str]:
        """Ask the UI and wait for an answer; returns (allowed, decision_string)."""
        self.approval_seq += 1
        request_id = f"ap-{int(time.time())}-{self.approval_seq}"
        request_path = self.udd / "approval-request.json"
        response_path = self.udd / "approval-response.json"
        if response_path.exists():
            response_path.unlink()
        request_path.write_text(json.dumps({
            "id": request_id, "env": env or "", "step": step, "args": args,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
        deadline = time.time() + float(self.permissions.get("approval_timeout_s", 120))
        while time.time() < deadline:
            try:
                answer = json.loads(response_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if answer.get("id") == request_id:
                allowed = bool(answer.get("allow"))
                print(f"[approval] {request_id} {step} -> "
                      f"{'allow' if allowed else 'deny'}", flush=True)
                return allowed, ("allow" if allowed else "deny")
        print(f"[approval] {request_id} {step} -> timeout", flush=True)
        return False, "timeout"


    def ensure_live(self, env: str) -> None:
        """Activate the environment when it has no live tab.

        Single-window mode parks inactive environments: their CDP targets disappear, so a step
        would fail with "no live tab". Activating brings the tab back (and, per §33.3, is also
        what a trusted click needs).
        """
        if not env:
            raise ValueError("explicit environment required")
        live = self.backend.live_state(max_age=0)
        active = live.get("active")
        if active == env:
            return
        known = {entry.get("env_id") for entry in live.get("envs", [])}
        if env not in known:
            raise ValueError(f"unknown environment {env}")
        try:
            import env_cli
            profile = env_cli.resolve_profile_dir(self.udd)
            reply = env_cli.send(profile, {"command": "activate", "env_id": env}, 20)
        except Exception as exc:                                   # noqa: BLE001
            reply = {"ok": False, "error": str(exc)[:120]}
        audit({"step": "env_activate", "env": env, "args": {"from": active},
               "ms": 0, "preset": self.permissions.get("preset"),
               "decision": "allow" if reply.get("ok") else "failed"})
        if not reply.get("ok"):
            raise RuntimeError(f"cannot activate {env}: {reply.get('error', 'activation failed')}")
        # The native command reply is the authoritative acknowledgement. A second
        # list round-trip added latency to every click and could race with a status
        # reader on the single native command channel. Refresh lazily on next use.
        self.backend._live_cache = None
        self.backend._runtime_cache = None
        acknowledged = reply.get("active")
        if acknowledged and acknowledged != env:
            raise RuntimeError(f"environment activation returned unexpected active environment: {acknowledged}")

    def run_step(self, env: str | None, step: dict, session_id: str | None = None) -> dict:
        with self.backend.step_context():
            return self._run_step(env, step, session_id)

    def _run_step(self, env: str | None, step: dict, session_id: str | None = None) -> dict:
        if len(step) != 1:
            raise ValueError(f"a step must have exactly one key, got {list(step)}")
        name, args = next(iter(step.items()))
        args = args or {}
        decision = "allow"
        preset = str(self.permissions.get("preset", "full"))
        if needs_approval(preset, name):
            allowed, decision = self.ask_approval(env, name, args)
            if not allowed:
                audit({"step": name, "env": env, "args": args, "ms": 0,
                       "preset": preset, "decision": decision})
                return {"step": name, "denied": True, "decision": decision,
                        "error": f"approval {decision}"}
        started = time.perf_counter()
        if name == "observe":
            result = self.backend.observe(env, int(args.get("limit", 80)))
            observation = self.backend.observations.get(result.get('env',env or ''))
            summary = getattr(observation,'summary',None)
            if isinstance(summary,dict):
                self.snapshots[env or ''] = summary
            else:
                self.snapshots.pop(env or '',None)
        elif name == "act":
            result = self.backend.act(env, args["id"], args.get("action", "click"))
        elif name == "type":
            result = self.backend.type_text(env, args["id"], args.get("text", ""))
        elif name == "goto":
            result = self.backend.goto(env, args["url"], int(args["wait_ms"]) if "wait_ms" in args else None)
        elif name == "clicks":
            selectors = args.get("selectors") or []
            trusted = bool(args.get("trusted", True))
            if args.get("selectorsFromObserve"):
                observation = self.backend.observations.get(env or "")
                ids = args.get("ids") or []
                if observation is None:
                    raise RuntimeError("clicks needs a prior observe in this env")
                selectors = [observation.by_id(item).get("href") or "" for item in ids]
            result = self.backend.click_selectors(env, selectors, trusted)
        elif name == "batch":
            result = self.backend.batch(env, args.get("ops") or [])
        elif name == "read":
            result = self.backend.read(env, args.get("selector", "body"),
                                       int(args.get("limit", 2000)))
        elif name == "shot":
            result = self.backend.screenshot(env, args.get("id"))
        elif name == "wait":
            time.sleep(float(args.get("ms", 200)) / 1000)
            result = {"waited_ms": args.get("ms", 200)}
        elif name == 'wait_for':
            result = self.backend.wait_for(env, **args)
        elif name == "env":
            result = {"note": "env switching happens through env_cli; use targets"}
        else:
            raise ValueError(f"unknown step {name!r}")
        elapsed = (time.perf_counter() - started) * 1000
        changed = None
        snapshot_warning = None
        # Mutating steps answer "what happened" without a second round trip from the agent.
        if name in ("act", "type", "goto") and result.get('ok') is not False:
            key = env or ""
            before = self.snapshots.get(key, {})
            try:
                after = self.backend.snapshot(env)
                self.snapshots[key] = after
                changed = BrowserBackend.diff(before, after)
            except Exception as exc:
                # The action already succeeded. A failed optional fingerprint
                # must not make the agent replay a click/type/navigation.
                self.snapshots.pop(key, None)
                snapshot_warning = str(exc)[:200]
                self.backend._drop_session(key)
        elapsed = (time.perf_counter() - started) * 1000
        record = {"step": name, "env": env, "args": args, "ms": round(elapsed, 2),
                  "session_id": session_id,
                  "preset": preset,
                  "decision": decision}
        if changed is not None:
            record["changed"] = changed
        audit(record)
        payload = {"step": name, **result, "ms": round(elapsed, 2)}
        if snapshot_warning:
            payload['snapshot_warning'] = snapshot_warning
        if changed is not None:
            payload["changed"] = changed
        return payload

    def execution_busy(self) -> bool:
        # Health requests run on another HTTP thread. Observe the task/focus
        # lock without waiting or interrupting the operation holding it.
        lock = getattr(self, 'execution_lock', None)
        if lock is None:
            return False
        if not lock.acquire(blocking=False):
            return True
        lock.release()
        return False

    def run_task(self, payload: dict, *, session_id: str | None = None) -> dict:
        with self.execution_lock:
            self.backend.strict_resolution = session_id is not None
            try:
                return self._run_task(payload, session_id=session_id)
            finally:
                self.backend.strict_resolution = False

    def _run_task(self, payload: dict, *, session_id: str | None = None) -> dict:
        if self.permissions.get("kill_switch"):
            return {"ok": False, "error": "bridge disabled by kill_switch"}
        steps = payload.get("steps") or []
        targets = payload.get("targets")
        mode = payload.get("mode", "parallel")
        fail_fast = bool(payload.get("failFast", False))
        env = payload.get("env")
        if not steps:
            return {"ok": False, "error": "no steps"}
        if not isinstance(steps, list) or any(not isinstance(step, dict) or len(step) != 1 for step in steps):
            return {"ok": False, "error": "steps must be an array of single-action objects"}
        supported = {'observe', 'act', 'type', 'goto', 'clicks', 'batch', 'read', 'shot', 'wait', 'wait_for', 'env'}
        for step in steps:
            name, args = next(iter(step.items()))
            if name not in supported or (args is not None and not isinstance(args, dict)):
                return {"ok": False, "error": "invalid browser step: " + name}
        if session_id is not None:
            allowed = self.browser_scopes.get(session_id)["env_ids"]
            if targets is not None and not isinstance(targets, list):
                return {"ok": False, "error": "targets must be an array"}
            if targets and env:
                return {"ok": False, "error": "use env or targets, not both"}
            targets = targets or ([env] if env else allowed)
            if targets == ["*"]:
                targets = allowed
            if any(not isinstance(x, str) or not x for x in targets):
                return {"ok": False, "error": "invalid environment id"}
            try:
                self.browser_scopes.require(session_id, targets)
                known = {e["env_id"] for e in self.backend.env_state().get("envs", [])}
                if set(targets) - known:
                    raise PermissionError("选择的环境已不存在，请重新选择浏览器环境。")
            except PermissionError as exc:
                audit({"session_id": session_id, "step": "scope_denied", "envs": targets,
                       "decision": "deny", "error": str(exc)})
                return {"ok": False, "error": str(exc), "scope_denied": True}
            targets = list(dict.fromkeys(targets))
            mode = "sequential"
        elif not targets and not env:
            targets = [self.backend.live_state().get("active", "")]
        if targets == ["*"] or targets == "*":
            targets = [e.get("env_id") for e in self.backend.live_state().get("envs", [])]
        if not targets:
            targets = [env]
        if not isinstance(targets, list) or any(not isinstance(target, str) or not target for target in targets):
            return {'ok': False, 'error': 'targets must contain valid environment ids'}
        targets = list(dict.fromkeys(targets))

        started = time.perf_counter()
        results: dict[str, dict] = {}

        def execute_for(one_env: str, effective_env: str) -> None:
            steps_out = []
            activated = False
            for step in steps:
                dispatched = False
                try:
                    if session_id is not None:
                        # Starting authorization is atomic with saves. Allow
                        # this in-flight step to finish, but don't hold the
                        # grant lock during browser I/O or approval waits.
                        self.browser_scopes.require(session_id, [one_env])
                        manager = getattr(self, 'env_management', None)
                        if manager and hasattr(manager, 'agent_session') and manager.agent_session(self.browser_scopes, session_id, one_env) != effective_env:
                            raise ValueError('本会话账号目标已变化，请重新选择后再执行')
                        # Read-only steps use an exact target and do not
                        # depend on which environment currently has focus.
                        # Mutations recheck focus; every step checks ownership.
                        if not activated or next(iter(step)) not in {'read', 'observe', 'shot', 'wait', 'wait_for'}:
                            self.ensure_live(effective_env)
                            activated = True
                        dispatched = True
                        steps_out.append(self.run_step(effective_env, step, session_id))
                    else:
                        self.ensure_live(effective_env)
                        dispatched = True
                        steps_out.append(self.run_step(effective_env, step))
                    outcome = steps_out[-1]
                    if outcome.get('denied') or outcome.get('ok') is False or outcome.get('error'):
                        results[one_env] = {'ok': False, 'results': steps_out,
                                            'error': outcome.get('error') or 'browser step failed'}
                        return
                except Exception as exc:                          # noqa: BLE001
                    failure = {"step": next(iter(step)), "error": str(exc)[:300]}
                    if getattr(exc,'action_sent',None) is False:
                        failure.update(action_sent=False,error_code=getattr(exc,'error_code','BROWSER_CONTROL_UNAVAILABLE'),
                            next_action='restore_browser_connection; do not reopen environments or fall back to shell')
                    elif dispatched and next(iter(step)) in MUTATING_STEPS:
                        failure.update(outcome_unknown=True,
                                       retry_hint='操作可能已经执行；先读取页面确认结果，不要自动重复操作。')
                    steps_out.append(failure)
                    results[one_env] = {"ok": False, "results": steps_out,
                                        "error": str(exc)[:300]}
                    return
            results[one_env] = {"ok": True, "results": steps_out}

        def run_for(one_env: str) -> None:
            tracker = getattr(self, 'agent_activity', None)
            token = None
            try:
                manager = getattr(self, 'env_management', None)
                if manager and session_id is not None and hasattr(manager, 'agent_session'):
                    effective_env = manager.agent_session(self.browser_scopes, session_id, one_env)
                else:
                    effective_env = manager.selected_session(one_env) if manager else one_env
                token = tracker.begin(effective_env) if tracker else None
                execute_for(one_env, effective_env)
            except Exception as exc:
                # A failed account mapping must not silently disappear from a
                # multi-environment task or turn its summary into success.
                results[one_env] = {'ok': False, 'results': [], 'error': str(exc)[:300]}
                if getattr(exc, 'reply', {}).get('account_target_changed'):
                    results[one_env].update(error_code='ACCOUNT_TARGET_CHANGED', next_action={
                        'tool':'environment_accounts','arguments':{'env_id':one_env,'action':'list'},
                        'message':'刷新账号列表并重新选择目标账号，不要回退或重复旧操作。'})
            finally:
                if tracker and token:
                    manager = getattr(self, 'env_management', None)
                    if manager and hasattr(manager, '_invalidate_live'):
                        manager._invalidate_live()
                    outcome = results.get(one_env, {})
                    tracker.finish(token, bool(outcome.get('ok')), outcome.get('error', ''))

        # The browser knows whether it runs one window per environment; the permissions file
        # does not (and has been rewritten more than once).
        window_per_env = False
        if session_id is None:
            try:
                window_per_env = bool(self.backend.live_state().get("window_per_env"))
            except Exception:                                      # noqa: BLE001
                window_per_env = bool(self.permissions.get("window_per_env", False))
        effective_mode = mode
        if len(targets) > 1 and not window_per_env:
            # One window can only keep one environment live; parallel activation would make one
            # of them lose its tab mid-run (measured).
            effective_mode = "sequential"

        if len(targets) == 1:
            run_for(targets[0])
        elif effective_mode == "sequential":
            for one_env in targets:
                run_for(one_env)
                if fail_fast and not results[one_env]["ok"]:
                    break
        else:
            from concurrent.futures import ThreadPoolExecutor
            limit = max(1, min(32, int(self.permissions.get("max_parallel", 4))))
            with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='environment-task') as executor:
                # The limit caps concurrency, never the number of requested environments.
                list(executor.map(run_for, targets))

        ok = all(entry.get("ok") for entry in results.values()) if results else False
        return {"ok": ok, "protocol": PROTOCOL_VERSION, "mode": effective_mode,
                "requested_mode": mode,
                "per_env": results,
                "summary": {"envs": len(results),
                            "ok": sum(1 for e in results.values() if e.get("ok")),
                            "failed": sum(1 for e in results.values() if not e.get("ok")),
                            "ms": round((time.perf_counter() - started) * 1000, 1)}}



def watch_chat_channel(bridge: "Bridge", profile: Path) -> None:
    """Serve `<profile>/chat-request.json` -> `chat-response.json` for the float window."""
    request_path = profile / "chat-request.json"
    response_path = profile / "chat-response.json"
    handled = ""
    # A leftover request from an earlier run already has its answer on disk; without this the new
    # bridge replayed it (measured: 16-23 s of model time and a spurious "busy" for the next message).
    try:
        stale = json.loads(request_path.read_text(encoding="utf-8"))
        answered = json.loads(response_path.read_text(encoding="utf-8"))
        if stale.get("nonce") and stale.get("nonce") == answered.get("nonce"):
            handled = str(stale["nonce"])
            print(f"chat channel: ignoring already answered request {handled}", flush=True)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    print(f"chat channel watching {request_path}", flush=True)
    while True:
        try:
            raw = request_path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError):
            time.sleep(0.15)
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            time.sleep(0.15)
            continue
        nonce = str(request.get("nonce", ""))
        if not nonce or nonce == handled:
            time.sleep(0.15)
            continue
        handled = nonce
        ask = str(request.get("ask", ""))
        session_id = str(request.get("session_id", "ui-session"))
        started = time.perf_counter()
        try:
            result = bridge.sessions.chat(session_id, ask)
        except Exception as exc:                                  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["nonce"] = nonce
        response_path.write_text(json.dumps(result, ensure_ascii=False),
                                 encoding="utf-8")
        # Consume the request: leaving it behind is what made a restart replay it.
        try:
            request_path.unlink()
        except OSError:
            pass
        print(f"[{time.strftime('%H:%M:%S')}] {session_id}: "
              f"{(time.perf_counter() - started):.1f}s "
              f"{'ok' if result.get('ok') else result.get('error')}", flush=True)



_TITLE_CACHE: dict[tuple[str, float], str] = {}


def session_title(log: Path) -> str:
    """Read `session/title` out of a compressed session log, cached by mtime."""
    try:
        key = (str(log), log.stat().st_mtime)
    except OSError:
        return ""
    if key in _TITLE_CACHE:
        return _TITLE_CACHE[key]
    title = ""
    try:
        import zstandard
        with log.open("rb") as handle:
            text = zstandard.ZstdDecompressor().stream_reader(handle).read().decode(
                "utf-8", "replace")
        for line in text.splitlines()[:120]:
            if '"session/title"' not in line:
                continue
            title = json.loads(line).get("data", {}).get("title", "") or ""
            break
    except Exception:                                             # noqa: BLE001
        title = ""
    _TITLE_CACHE[key] = title
    return title


_TURNS_CACHE: dict[tuple[str, float], int] = {}

# Sessions created by this project's own probes; they are not conversations and must not show up as
# if they were. Matched as prefixes, so `e2e-1793041135` and `p0-session` both drop out.
TEST_SESSION_PREFIXES = ("e2e-", "p0-", "diag-", "diagnostic-", "refresh-check-",
                         "http-check", "cli-check", "official-check",
                         "crashloop-", "reuse-", "child-test", "zrt-test", "soak-",
                         "layer-", "follow-", "accept-", "ui-accept")


def session_turns(log: Path) -> int:
    """Count completed turns in a compressed session log, cached by mtime."""
    try:
        key = (str(log), log.stat().st_mtime)
    except OSError:
        return 0
    if key in _TURNS_CACHE:
        return _TURNS_CACHE[key]
    turns = 0
    try:
        import zstandard
        with log.open("rb") as handle:
            text = zstandard.ZstdDecompressor().stream_reader(handle).read().decode(
                "utf-8", "replace")
        turns = text.count('"turn/end"')
    except Exception:                                             # noqa: BLE001
        turns = 0
    _TURNS_CACHE[key] = turns
    return turns


def is_test_session(session_id: str) -> bool:
    return any(session_id.startswith(prefix) for prefix in TEST_SESSION_PREFIXES)


def watch_sessions(bridge: "Bridge", home: Path, udd: Path) -> None:
    """Snapshot persisted DSH sessions (plus live state) into <udd>/sessions.json."""
    target = udd / "sessions.json"
    sessions_root = home / "sessions"
    while True:
        items: list[dict] = []
        try:
            for session_dir in sorted(sessions_root.glob("*/*")):
                if not session_dir.is_dir():
                    continue
                if is_test_session(session_dir.name):
                    continue
                log = session_dir / "session.v4.jsonl.zstd"
                items.append({
                    "id": session_dir.name,
                    "workspace": session_dir.parent.name,
                    "mtime": log.stat().st_mtime if log.exists() else 0.0,
                    "bytes": log.stat().st_size if log.exists() else 0,
                    "title": session_title(log) if log.exists() else "",
                    "turns": session_turns(log) if log.exists() else 0,
                })
        except Exception:                                         # noqa: BLE001
            items = []
        live = {entry["session_id"]: entry for entry in bridge.sessions.list_sessions()}
        for item in items:
            entry = live.get(item["id"], {})
            item["live"] = bool(entry)
            item["title"] = entry.get("title") or item.get("title", "")
            # A live session knows its own turn count; a persisted one is counted from its log.
            item["turns"] = max(int(entry.get("turns", 0)), int(item.get("turns", 0)))
        items.sort(key=lambda entry: entry["mtime"], reverse=True)
        try:
            target.write_text(json.dumps({"sessions": items[:60]}, ensure_ascii=False),
                              encoding="utf-8")
        except OSError:
            pass
        time.sleep(2.0)


# --------------------------------------------------------------------------- workbench watchdog
def workbench_watchdog(bridge: "Bridge") -> None:
    """Keep the embedded workbench alive and keep the layer pointed at it.

    A dead workbench behind a live-looking layer is the worst failure this product can show; this loop restarts it and
    re-points the overlay so the operator never sees a stale page.
    """
    import _dsh_web
    import env_cli

    failures = 0
    pause = 15.0
    while True:
        time.sleep(pause)
        try:
            state = _dsh_web.load_state()
            healthy = _dsh_web.owns_state(state) and _dsh_web.probe(str(state.get("url") or ""))[0]
            if healthy:
                failures, pause = 0, 15.0
                continue
            failures += 1
            # Give startup, transient load and active agent turns time to finish.
            if failures < 3 or bridge.sessions.turn_started:
                continue
            gateway = getattr(bridge, "environment_agent", None)
            if gateway and gateway.busy():
                continue
            LOG.warning("workbench health unavailable; recovery attempt %s", failures - 2)
            replacement = _dsh_web.ensure()
            if not replacement.get("ok"):
                pause = min(300.0, pause * 2)
                continue
            profile = env_cli.resolve_profile_dir(bridge.udd)
            # Reconnect without reopening a panel the user deliberately hid.
            env_cli.send(profile, {"command": "overlay", "mode": "refresh",
                                   "url": replacement["url"]}, 3)
            failures, pause = 0, 15.0
        except Exception as exc:
            pause = min(300.0, pause * 2)
            LOG.warning("workbench recovery deferred: %s", type(exc).__name__)


def start_workbench_watchdog(bridge: "Bridge") -> None:
    import threading

    thread = threading.Thread(target=workbench_watchdog, args=(bridge,),
                              name="workbench-watchdog", daemon=True)
    thread.start()
    LOG.info("workbench watchdog started")


def make_handler(bridge: Bridge, token: str):
    agent_gateway_lock = threading.Lock()
    def agent_gateway():
        with agent_gateway_lock:
            if not hasattr(bridge, 'environment_agent'):
                from environment_agent import EnvironmentAgentService
                bridge.environment_agent = EnvironmentAgentService(bridge, token)
            return bridge.environment_agent

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:        # noqa: A003
            return

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if header == f"Bearer {token}":
                return True
            # The seat runs inside the product's own workbench page. Its origin is unique to that page on loopback,
            # and baking a token into the bundle goes stale whenever either side restarts.
            origin = self.headers.get("Origin", "")
            if not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
                return False
            try:
                import pathlib as _pl

                state = json.loads((DATA_ROOT / ".dsh-web.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            workbench_port = str(state.get("port") or "")
            return bool(workbench_port) and origin.rsplit(":", 1)[-1] == workbench_port

        def _cors(self) -> None:
            """Allow the local workbench page to call the bridge from the browser."""
            origin = self.headers.get("Origin", "")
            allowed = origin.startswith("http://127.0.0.1:") or origin.startswith(
                "http://localhost:")
            if allowed:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers",
                                 "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")

        def _send(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:                          # noqa: N802
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:                              # noqa: N802
            if not self._authorized():
                self._send({"ok": False, "error": "unauthorized"}, 401)
                return
            if self.path == "/v1/health":
                self._send({"ok": True, "protocol": PROTOCOL_VERSION,
                            "pid": os.getpid(), "data_root": str(DATA_ROOT.resolve()),
                            "app_root": str(APP_ROOT.resolve()),
                            "profile_root": str(PROFILE_DIR.resolve()),
                            "runtime_revision": LOADED_RUNTIME_REVISION,
                            "env_management_api": 2,
                            "environment_status_api": 1,
                            "environment_activity_api": 2,
                            "environment_import_api": 2,
                            "environment_agent_api": 2,
                            "environment_agent_busy": getattr(getattr(bridge, 'environment_agent', None), 'busy', lambda: False)(),
                            "environment_import_busy": getattr(getattr(getattr(bridge, 'env_management', None), 'imports', None), 'busy', lambda: False)(),
                            "browser_busy": getattr(bridge, 'execution_busy', lambda: False)(),
                            "chat_busy": bool(getattr(bridge.sessions, 'turn_started', None)),
                            "features": ["observe", "act", "batch", "targets", "chat", "wait_for"],
                            "backend": "cdp", "preset": bridge.permissions.get("preset")})
            elif self.path in ('/v1/lan/manage/openapi', '/v1/lan/manage/catalog'):
                from environment_api import openapi, catalogue
                document = openapi() if self.path.endswith('/openapi') else catalogue()
                if 'servers' in document:
                    document['servers'] = [{'url': 'http://127.0.0.1:' + str(self.server.server_port)}]
                self._send(document)
            elif self.path == "/v1/envs":
                self._send({"ok": True, **bridge.backend.live_state()})
            elif self.path == "/v1/sessions":
                self._send({"ok": True, "sessions": bridge.sessions.list_sessions()})
            else:
                self._send({"ok": False, "error": "not found"}, 404)

        def _lan_state(self) -> dict:
            live = bridge.backend.live_state(max_age=0)
            persisted = {e.get("env_id"): e for e in
                         bridge.backend.env_state().get("envs", [])}
            try:
                account_summaries = bridge.account_vault.summaries(
                    [e.get("env_id", "") for e in live.get("envs", [])])
            except Exception:
                # A locked/corrupt vault must not make browser environments unavailable.
                LOG.exception("local account metadata could not be read")
                account_summaries = {}
            envs = []
            for live_env in live.get("envs", []):
                env_id = live_env.get("env_id", "")
                row = dict(live_env)
                row.update({k: persisted.get(env_id, {}).get(k, row.get(k, ""))
                            for k in ("group", "tags", "notes", "homepage",
                                      "start_url", "locale", "accept_languages",
                                      "fingerprint_seed")})
                row.update(account_summaries.get(env_id, {"has_account": False}))
                envs.append(row)
            return {"ok": True, "active": live.get("active", ""), "envs": envs}

        def do_POST(self) -> None:                             # noqa: N802
            if self.path == '/v1/session/manage':
                # This route is for trusted DSH hosts only. The UI Origin
                # fallback must never authorize approval grants/session claims.
                if self.headers.get('Authorization', '') != f'Bearer {token}':
                    self._send({'ok': False, 'error': 'unauthorized'}, 401)
                    return
                from env_management import ManagementError
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 <= length <= 4 * 1024 * 1024:
                        self.close_connection = True
                        self._send({'ok': False, 'error': '请求过大'}, 413)
                        return
                    payload = json.loads(self.rfile.read(length) or b'{}')
                    self._send(agent_gateway().dispatch(payload))
                except ManagementError as exc:
                    self._send(agent_gateway().failure(exc, payload.get('tool') if isinstance(payload,dict) else None,
                        payload.get('arguments') if isinstance(payload,dict) else None), exc.status)
                except (ValueError, TypeError):
                    self._send({'ok': False, 'error_code': 'VALIDATION_ERROR',
                        'error': '管理请求格式无效，未执行后续操作',
                        'next_action': {'tool': 'environment_catalog', 'arguments': {},
                                        'message': '先核对工具参数和导入格式，再提交修正后的请求。'}}, 400)
                except Exception:
                    # Arguments can contain credentials; do not log raw exceptions.
                    self._send({'ok': False, 'outcome_unknown': True,
                                'error': '管理请求结果待核实，请先查询请求状态再决定下一步'}, 500)
                return
            if self.path.startswith('/v1/lan/manage/'):
                if not self._authorized():
                    self._send({'ok': False, 'error': 'unauthorized'}, 401)
                    return
                from env_management import ManagementError
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if not 0 <= length <= 4 * 1024 * 1024:
                        self._send({'ok': False, 'error': '请求过大'}, 413)
                        self.close_connection = True
                        return
                    payload = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(payload, dict):
                        raise ManagementError('请求必须是对象')
                    self._send(bridge.env_management.dispatch(self.path.rsplit('/', 1)[-1], payload))
                except ManagementError as exc:
                    self._send(exc.reply, exc.status)
                except (ValueError, TypeError):
                    self._send({'ok': False, 'error': '配置数据格式无效，未执行后续操作'}, 400)
                except Exception:
                    LOG.exception('environment management request failed')
                    self._send({'ok': False, 'error': '本地管理服务暂时不可用，请保留输入后重试'}, 500)
                return
            if self.path in ("/v1/lan/envs", "/v1/lan/envs/save", "/v1/lan/envs/delete",
                             "/v1/lan/envs/account", "/v1/lan/proxy/test",
                             "/v1/lan/activate", "/v1/lan/hide", "/v1/lan/layer",
                             "/v1/lan/expand", "/v1/lan/rail"):
                if not self._authorized():
                    self._send({"ok": False, "error": "unauthorized"}, 401)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if self.path == "/v1/lan/envs/save":
                    import env_cli
                    from local_account_vault import valid_env_id
                    env_id = payload.get("env_id")
                    if not valid_env_id(env_id):
                        self._send({"ok": False, "error": "环境编号仅能使用字母、数字、- 和 _，长度 1–64"}, 400)
                        return
                    allowed = ("display_name", "group", "tags", "notes", "proxy",
                               "expected_exit_ip", "locale", "accept_languages",
                               "fingerprint_seed", "start_url", "homepage", "ip_mode")
                    command = {"command": "create", "env_id": env_id}
                    for key in allowed:
                        value = payload.get(key)
                        if value is not None:
                            if not isinstance(value, str) or len(value) > 2048:
                                self._send({"ok": False, "error": f"字段格式不正确：{key}"}, 400)
                                return
                            command[key] = value.strip()
                    try:
                        if "proxy" in command:
                            command["proxy"] = validate_proxy_config(command["proxy"])
                    except ValueError as exc:
                        self._send({"ok": False, "error": str(exc)}, 400)
                        return
                    state = bridge.backend.env_state()
                    exists = any(e.get("env_id") == env_id for e in state.get("envs", []))
                    if payload.get("create_only") and exists:
                        self._send({"ok": False, "error": "环境编号已存在，请重新生成编号"}, 409)
                        return
                    if not exists and not command.get("display_name"):
                        self._send({"ok": False, "error": "请填写环境名称"}, 400)
                        return
                    profile = env_cli.resolve_profile_dir(bridge.udd)
                    reply = env_cli.send(profile, command, 20)
                    if not reply.get("ok"):
                        self._send({"ok": False, "error": reply.get("error", "保存环境失败")}, 400)
                        return
                    bridge.backend._live_cache = None
                    LOG.info("lan: saved local environment %s", env_id)
                    self._send(self._lan_state())
                    return
                if self.path == "/v1/lan/envs/delete":
                    import env_cli
                    from local_account_vault import valid_env_id
                    env_id = payload.get("env_id")
                    if not valid_env_id(env_id):
                        self._send({"ok": False, "error": "无效的环境编号"}, 400)
                        return
                    profile = env_cli.resolve_profile_dir(bridge.udd)
                    reply = env_cli.send(profile, {"command": "delete", "env_id": env_id}, 20)
                    if not reply.get("ok"):
                        self._send({"ok": False, "error": reply.get("error", "删除环境失败")}, 400)
                        return
                    bridge.backend._live_cache = None
                    LOG.info("lan: removed environment definition %s; partition data kept", env_id)
                    self._send({**self._lan_state(), "storage_preserved": True})
                    return
                if self.path == "/v1/lan/envs/account":
                    from local_account_vault import valid_env_id
                    env_id = payload.get("env_id")
                    known = {e.get("env_id") for e in bridge.backend.env_state().get("envs", [])}
                    if not valid_env_id(env_id) or env_id not in known:
                        self._send({"ok": False, "error": "环境不存在"}, 404)
                        return
                    if payload.get("action") == "clear":
                        bridge.account_vault.delete(env_id)
                        self._send({"ok": True, "account": {"has_account": False}})
                        return
                    account = payload.get("account")
                    if not isinstance(account, dict):
                        self._send({"ok": False, "error": "账号信息格式错误"}, 400)
                        return
                    try:
                        summary = bridge.account_vault.save(env_id, account)
                    except Exception as exc:
                        LOG.exception("local account vault write failed")
                        self._send({"ok": False, "error": f"本机加密保存失败：{str(exc)[:160]}"}, 500)
                        return
                    self._send({"ok": True, "account": summary})
                    return
                if self.path == "/v1/lan/proxy/test":
                    import urllib.request
                    proxy = str(payload.get("proxy") or "").strip()
                    if proxy in ("", "direct", "system"):
                        self._send({"ok": False, "error": "请先填写代理地址"}, 400)
                        return
                    try:
                        proxy = validate_proxy_config(proxy)
                    except ValueError as exc:
                        self._send({"ok": False, "error": str(exc)}, 400)
                        return
                    if proxy.startswith(("socks4://", "socks5://")):
                        self._send({"ok": False, "error": "当前 Python 代理检测只支持 HTTP/HTTPS 代理；SOCKS 配置仍可由浏览器使用"}, 400)
                        return
                    try:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                            "http": proxy, "https": proxy}))
                        with opener.open("https://api.ipify.org?format=json", timeout=8) as response:
                            data = json.loads(response.read(512).decode("utf-8"))
                        self._send({"ok": True, "exit_ip": str(data.get("ip") or ""),
                                    "note": "连接测试成功"})
                    except Exception as exc:
                        self._send({"ok": False, "error": f"代理连接失败：{str(exc)[:180]}"}, 400)
                    return
                if self.path == "/v1/lan/layer":
                    # Served from the file the browser writes on every collapse/expand: no command-channel round trip.
                    import pathlib as _pl
                    state_file = _pl.Path(bridge.udd) / "Default" / "lan-layer-state.json"
                    if not state_file.exists():
                        state_file = _pl.Path(bridge.udd) / "lan-layer-state.json"
                    if state_file.exists():
                        try:
                            payload = json.loads(state_file.read_text(encoding="utf-8"))
                            self._send({"ok": True, "visible": bool(payload.get("visible", True)),
                                        "collapsed": bool(payload.get("collapsed", False)),
                                        "source": "file"})
                            return
                        except (OSError, ValueError):
                            pass
                    import env_cli
                    profile = env_cli.resolve_profile_dir(bridge.udd)
                    reply = env_cli.send(profile, {"command": "overlay", "query": True}, 25)
                    geometry = str(reply.get("geometry") or "")
                    self._send({"ok": True,
                                "visible": bool(reply.get("visible")),
                                "collapsed": "collapsed=1" in geometry,
                                "geometry": geometry})
                    return
                if self.path in ("/v1/lan/expand", "/v1/lan/rail"):
                    import env_cli
                    profile = env_cli.resolve_profile_dir(bridge.udd)
                    mode = "expand" if self.path.endswith("expand") else "rail"
                    reply = env_cli.send(profile, {"command": "overlay", "on": True, "mode": mode}, 30)
                    LOG.info("lan: layer mode %s -> %s", mode, str(reply.get("geometry"))[:60])
                    self._send({"ok": bool(reply.get("ok")), "mode": mode,
                                "geometry": reply.get("geometry")})
                    return
                if self.path == "/v1/lan/hide":
                    import env_cli
                    profile = env_cli.resolve_profile_dir(bridge.udd)
                    env_cli.send(profile, {"command": "overlay", "on": False, "mode": "hide"}, 6)
                    LOG.info("lan: hiding the assistant layer on request")
                    self._send({"ok": True, "hidden": True})
                    return
                if self.path == "/v1/lan/activate":
                    env_id = str(payload.get("env_id") or "")
                    if not env_id:
                        self._send({"ok": False, "error": "env_id required"}, 400)
                        return
                    known = {entry.get("env_id")
                             for entry in bridge.backend.live_state().get("envs", [])}
                    if env_id not in known:
                        self._send({"ok": False, "error": f"unknown env {env_id}",
                                    "envs": sorted(known)}, 404)
                        return
                    # ensure_live is the same path the agent's steps use: it drives the browser's own
                    # activate command, so the seat and the panel cannot drift apart.
                    bridge.ensure_live(env_id)
                    LOG.info("lan: activate %s", env_id)
                self._send(self._lan_state())
                return
            if not self._authorized():
                self._send({"ok": False, "error": "unauthorized"}, 401)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._send({"ok": False, "error": f"bad json: {exc}"}, 400)
                return
            try:
                if self.path == "/v1/lan/session-scope":
                    session_id = bridge.browser_scopes.session_key(payload.get("session_id"))
                    # Selecting a grant needs metadata, not a native command
                    # round trip (which can contend with live browser tasks).
                    live = bridge.backend.env_state()
                    internal = bridge.env_management.internal_session_ids()
                    allowed_envs = {e["env_id"] for e in live.get("envs", [])} - internal
                    if "env_ids" in payload:
                        scope = bridge.browser_scopes.set(session_id, payload["env_ids"],
                            allowed_envs)
                    else:
                        scope = bridge.browser_scopes.get(session_id)
                    envs = [{"env_id": e["env_id"], "display_name": e.get("display_name", e["env_id"])}
                            for e in live.get("envs", []) if e["env_id"] in allowed_envs]
                    self._send({"ok": True, **scope, "envs": envs})
                elif self.path == "/v1/session/envs":
                    scope = bridge.browser_scopes.get(payload.get("session_id"))
                    envs = [{"env_id": e["env_id"], "display_name": e.get("display_name", e["env_id"])}
                            for e in bridge.backend.env_state().get("envs", [])
                            if e["env_id"] in scope["env_ids"]]
                    self._send({"ok": True, **scope, "envs": envs})
                elif self.path == "/v1/session/task":
                    session_id = bridge.browser_scopes.session_key(payload.get("session_id"))
                    self._send(bridge.run_task(payload.get("task") or {}, session_id=session_id))
                elif self.path == "/v1/task":
                    self._send(bridge.run_task(payload))
                elif self.path == "/v1/ping":
                    self._send({"ok": True, "pong": True,
                                "browser": bridge.backend.debug_port(),
                                "active": bridge.backend.live_state().get("active"),
                                "ts": time.time()})
                elif self.path == "/v1/chat":
                    self._send(bridge.sessions.chat(
                        payload.get("session_id") or "ui-session",
                        payload.get("ask") or "",
                        float(payload.get("timeout", 180))))
                else:
                    self._send({"ok": False, "error": "not found"}, 404)
            except Exception as exc:                            # noqa: BLE001
                self._send({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    port, token = load_token(args.port)
    bridge = Bridge()
    profile_dir = PROFILE_DIR
    threading.Thread(target=watch_chat_channel, args=(bridge, profile_dir),
                     daemon=True).start()
    threading.Thread(target=watch_sessions,
                     args=(bridge, DATA_ROOT / ".dsh-sdk", profile_dir),
                     daemon=True).start()
    # Never reuse another installation's socket on Windows.
    class PrivateHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = False
        def server_bind(self):
            import socket
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    server = PrivateHTTPServer(("127.0.0.1", port), make_handler(bridge, token))
    port = server.server_port
    atomic_json(TOKEN_FILE, {"port": port, "token": token, "pid": os.getpid(),
                            "app_root": str(APP_ROOT.resolve()),
                            "data_root": str(DATA_ROOT.resolve())})
    print(f"lan-bridge listening on http://127.0.0.1:{port} "
          f"(token in {TOKEN_FILE.name})", flush=True)
    start_workbench_watchdog(bridge)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
