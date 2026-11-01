"""Warm DSH sessions for the UI.

One `dsh --profile sdk` runtime per session, kept alive so a conversation keeps its
context and does not pay a 3–5 s cold start per message. Idle runtimes are closed
lazily (checked when a new message arrives) so a long-lived browser does not accumulate
processes.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from dsh_client import DshRuntime                                    # noqa: E402

IDLE_SECONDS = 900.0          # 15 min without traffic → close the runtime
MAX_SESSIONS = 4              # hard cap; oldest idle session is closed above it
DEFAULT_MAX_TOKENS = 4096     # 1024 truncated multi-step browser_task runs


@dataclass
class Session:
    session_id: str
    runtime: DshRuntime
    last_used: float = field(default_factory=time.time)
    turns: int = 0
    title: str = ""


class SessionManager:
    def __init__(self, home: Path, provider: str | None = None,
                 model: str = "deepseek-flash",
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.home = home
        # The official route needs a working API key; without one fall back to the
        # account-backed provider that the desktop app uses.
        key_file = HERE / ".deepseek-key"
        has_key = bool(key_file.exists() and key_file.read_text(encoding="utf-8").strip())
        self.provider = provider or ("deepseek-official" if has_key
                                    else "deepseek-account")
        self.model = model
        self.max_tokens = max_tokens
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        # One turn at a time: a second ask must not sit behind the first with no feedback.
        self.turn_gate = threading.Lock()
        self.turn_started: float | None = None

    # ------------------------------------------------------------------ helpers
    def _close_locked(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        try:
            session.runtime.close()
        except Exception:                                          # noqa: BLE001
            pass

    def _evict_locked(self) -> None:
        now = time.time()
        for session_id in [sid for sid, s in self.sessions.items()
                           if now - s.last_used > IDLE_SECONDS]:
            self._close_locked(session_id)
        while len(self.sessions) > MAX_SESSIONS:
            oldest = min(self.sessions.values(), key=lambda s: s.last_used)
            self._close_locked(oldest.session_id)

    def _ensure_locked(self, session_id: str) -> Session:
        self._evict_locked()
        session = self.sessions.get(session_id)
        if session is not None and session.runtime.proc.poll() is not None:
            # The child died; drop it and start a fresh one for this session id.
            self._close_locked(session_id)
            session = None
        if session is None:
            runtime = DshRuntime(self.home)
            runtime.initialize(str(HERE), self.provider, self.model, self.max_tokens)
            session = Session(session_id=session_id, runtime=runtime)
            self.sessions[session_id] = session
        return session

    # -------------------------------------------------------------------- public
    def chat(self, session_id: str, ask: str, timeout: float = 180.0) -> dict:
        """Send one prompt and return the whole assistant text for that turn."""
        if not self.turn_gate.acquire(blocking=False):
            waited = (time.time() - self.turn_started) if self.turn_started else 0.0
            return {"ok": False, "busy": True,
                    "error": f"上一条消息仍在处理中（已等待 {waited:.0f} 秒），请稍后重试。"}
        self.turn_started = time.time()
        try:
            return self._chat_locked(session_id, ask, timeout)
        finally:
            self.turn_started = None
            self.turn_gate.release()

    def _chat_locked(self, session_id: str, ask: str, timeout: float) -> dict:
        with self.lock:
            session = self._ensure_locked(session_id)
            try:
                prompt_result = session.runtime.prompt(session_id, ask)
            except Exception as exc:                              # noqa: BLE001
                self._close_locked(session_id)
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        texts: list[str] = []
        tool_calls: list[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            for event in session.runtime.drain_events(0.5):
                inner = event.get("params", {}).get("event", {})
                kind = inner.get("type")
                if kind == "assistant/message":
                    for block in inner.get("data", {}).get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            texts.append(block["text"])
                elif kind == "tool/call":
                    tool_calls.append(inner.get("data", {}).get("name", "?"))
                elif kind == "session/title":
                    session.title = (inner.get("data", {}).get("title") or session.title)
                elif kind == "turn/end":
                    with self.lock:
                        session.turns += 1
                        session.last_used = time.time()
                    return {"ok": True, "text": "".join(texts),
                            "tools": tool_calls, "session_id": session_id,
                            "message_id": prompt_result.get("messageId", ""),
                            "turns": session.turns, "title": session.title}
        with self.lock:
            session.last_used = time.time()
        return {"ok": False, "error": "timeout waiting for turn/end",
                "text": "".join(texts), "tools": tool_calls}

    def list_sessions(self) -> list[dict]:
        with self.lock:
            self._evict_locked()
            return [{"session_id": s.session_id, "turns": s.turns,
                     "title": s.title,
                     "idle_s": round(time.time() - s.last_used, 1)}
                    for s in self.sessions.values()]

    def close_all(self) -> None:
        with self.lock:
            for session_id in list(self.sessions):
                self._close_locked(session_id)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    manager = SessionManager(HERE / ".dsh-sdk")
    try:
        print("turn 1:", json.dumps(manager.chat("cli-check", "Remember the number 4711. "
                                                 "Reply with just OK.", 120),
                                    ensure_ascii=False)[:200])
        print("turn 2:", json.dumps(manager.chat("cli-check", "What number did I ask you to "
                                                 "remember? Reply with just the number.", 120),
                                    ensure_ascii=False)[:200])
        print("sessions:", manager.list_sessions())
    finally:
        manager.close_all()
