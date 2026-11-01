"""Drive the packaged DSH SDK runtime over stdio JSON-RPC.

No source checkout and no pip package: the installed app ships both the runtime and
the CLI, and `dsh --profile sdk` speaks line-delimited JSON-RPC 2.0 on stdin/stdout.

Method surface (read out of `@deepseek-ai/dsh-sdk-jsonrpc-server`):
  initialize                       {cwd, provider, model, reasoningEffort?, maxTokens?}
  session/prompt                   {sessionId, contentBlocks:[{type:"text",text}]}
  session/resume                   {sessionId}
  session/wait                     {sessionId}
  session/working-directory/get    {sessionId}
  session/working-directory/set    {sessionId, path}
  shutdown                         {}

Notifications: session.event {sessionId, event}, session.status {sessionId, status},
subagent.started, subagent.finished.

Usage:
  python dsh_client.py --ask "Reply with exactly: pong"
  python dsh_client.py --ask "..." --model deepseek-v4-flash --session demo-1
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess

from _nowindow import hidden_startup_info, no_window
from bluebean_paths import DATA_ROOT, DSH_BIN, DSH_EXE
import sys
import threading
import time
import uuid
from pathlib import Path

EXE = str(DSH_EXE)
BIN = str(DSH_BIN)
DEFAULT_HOME = DATA_ROOT / ".dsh-sdk"
CREDENTIALS = DATA_ROOT / "DSH" / ".credentials.yaml"


def api_key_from_credentials() -> str | None:
    """Pull DEEPSEEK_API_KEY out of the operator's DSH credentials, without echoing it."""
    if not CREDENTIALS.exists():
        return None
    text = CREDENTIALS.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(sk-[A-Za-z0-9_\-]{8,})", text)
    return match.group(1) if match else None


class DshRuntime:
    """One `dsh --profile sdk` process, spoken to over line-delimited JSON-RPC."""

    def __init__(self, home: Path, log: bool = True) -> None:
        self.home = home
        self.log = log
        home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        env["DSH_HOME"] = str(home)
        # dsh-base reads the permission mode from this variable and defaults to workspace-write,
        # which on Windows runs tools under an ACL restricted token - powershell then fails to
        # initialise (0xC0000142) after flashing a console window. The operator's own GUI session
        # runs as danger-full-access, so match it; LAN_PERMISSION_MODE can tighten it again.
        env.setdefault("DSH_PERMISSION_MODE",
                       os.environ.get("LAN_PERMISSION_MODE", "danger-full-access"))
        # The lan-browser plugin authenticates with these; without them every tool call comes
        # back "unauthorized" (measured end-to-end).
        bridge_config = DATA_ROOT / ".lan-bridge.json"
        if bridge_config.exists():
            try:
                import json as _json
                config = _json.loads(bridge_config.read_text(encoding="utf-8"))
                env.setdefault("LAN_BRIDGE_TOKEN", config.get("token", ""))
                env.setdefault("LAN_BRIDGE_URL",
                               f"http://127.0.0.1:{config.get('port', 8799)}")
            except Exception:                                    # noqa: BLE001
                pass
        if "DEEPSEEK_API_KEY" not in env:
            # Priority: workspace key file, then the DSH credential store. The store's key
            # measured 401, so a working key usually comes from the file.
            key_file = DATA_ROOT / "DSH" / ".deepseek-key"
            key = (key_file.read_text(encoding="utf-8").strip()
                   if key_file.exists() else "")
            if not key:
                key = api_key_from_credentials() or ""
            if key:
                env["DEEPSEEK_API_KEY"] = key
        self.proc = subprocess.Popen(
            [EXE, BIN, "--profile", "sdk"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(home), text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            # No console window for the runtime, and therefore none for the shells it starts.
            creationflags=no_window(), startupinfo=hidden_startup_info())
        self.responses: dict[str, dict] = {}
        self.events: queue.Queue[dict] = queue.Queue()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    # ------------------------------------------------------------------ plumbing
    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.stderr_lines.append(f"non-json: {line[:200]}")
                continue
            if "id" in message and ("result" in message or "error" in message):
                self.responses[str(message["id"])] = message
            elif "method" in message:
                self.events.put(message)

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def request(self, method: str, params: dict | None = None,
                timeout: float = 120.0) -> dict:
        request_id = f"req_{uuid.uuid4().hex}"
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method,
                 "params": params or {}}
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            reply = self.responses.pop(request_id, None)
            if reply is not None:
                if "error" in reply:
                    raise RuntimeError(f"{method} failed: {reply['error']}")
                return reply.get("result") or {}
            time.sleep(0.02)
        raise TimeoutError(f"{method} timed out after {timeout:.0f}s")

    # ----------------------------------------------------------------- protocol
    def initialize(self, cwd: str, provider: str, model: str,
                   max_tokens: int = 1024) -> dict:
        return self.request("initialize", {
            "cwd": cwd, "provider": provider, "model": model,
            "maxTokens": max_tokens})

    def prompt(self, session_id: str, text: str) -> dict:
        body = {"sessionId": session_id,
                "contentBlocks": [{"type": "text", "text": text}]}
        try:
            return self.request("session/prompt", body)
        except RuntimeError as error:
            # The SDK runtime refuses an id that already exists in the persistence store.
            # Measured right after a restart: `session "ui-1" already exists`. Resuming is
            # the supported way back into a persisted session.
            if "already exists" not in str(error):
                raise
            self.request("session/resume", {"sessionId": session_id})
            return self.request("session/prompt", body)

    def wait(self, session_id: str, timeout: float = 180.0) -> dict:
        return self.request("session/wait", {"sessionId": session_id},
                            timeout=timeout)

    def drain_events(self, seconds: float = 1.0) -> list[dict]:
        out: list[dict] = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                out.append(self.events.get(timeout=0.1))
            except queue.Empty:
                continue
        return out

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=10)
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:                                          # noqa: BLE001
            self.proc.kill()


def assistant_text(event: dict) -> str | None:
    """Pull plain assistant text out of a session event, whichever shape it has."""
    payload = event.get("params", {})
    inner = payload.get("event", {})
    for key in ("message", "delta", "content", "text"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            blocks = value.get("content") or value.get("blocks")
            if isinstance(blocks, list):
                texts = [b.get("text", "") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text"]
                joined = "".join(texts).strip()
                if joined:
                    return joined
    return None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", default="Reply with exactly one word: pong")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--provider", default="deepseek-official")
    parser.add_argument("--session", default="p0-session")
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args()

    runtime = DshRuntime(Path(args.home))
    try:
        print("initialize:", json.dumps(runtime.initialize(
            args.cwd, args.provider, args.model, args.max_tokens)))
        print("prompt    :", json.dumps(runtime.prompt(args.session, args.ask)))

        deadline = time.time() + 150
        seen: list[str] = []
        while time.time() < deadline:
            for event in runtime.drain_events(1.0):
                method = event.get("method", "")
                text = assistant_text(event)
                if method == "session.status":
                    status = event.get("params", {}).get("status")
                    print(f"  status   : {status}")
                    if status == "idle" and seen:
                        deadline = 0
                elif text:
                    seen.append(text)
                    print(f"  event    : {method} -> {text[:160]}")
                    if "turn/end" in json.dumps(event)[:400]:
                        deadline = 0
            if deadline == 0:
                break
        print("collected :", "".join(seen)[-400:] or "(no assistant text seen)")
        if runtime.stderr_lines:
            print("stderr    :", "\n            ".join(runtime.stderr_lines[-6:]))
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
