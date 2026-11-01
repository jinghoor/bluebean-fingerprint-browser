"""Persistent, explicit browser grants owned by individual DSH sessions."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class SessionBrowserScopes:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        if path.exists():
            self.sessions = json.loads(path.read_text(encoding="utf-8"))["sessions"]
        else:
            self.sessions = {}

    @staticmethod
    def session_key(value) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError("session_id required")
        return value

    def get(self, session_id: str) -> dict:
        session_id = self.session_key(session_id)
        with self.lock:
            entry = self.sessions.get(session_id, {"env_ids": [], "revision": 0})
            return {"session_id": session_id, "env_ids": list(entry["env_ids"]),
                    "revision": entry["revision"]}

    def set(self, session_id: str, env_ids, known: set[str]) -> dict:
        session_id = self.session_key(session_id)
        if not isinstance(env_ids, list) or any(not isinstance(x, str) for x in env_ids):
            raise ValueError("env_ids must be an array of environment ids")
        env_ids = list(dict.fromkeys(env_ids))
        unknown = set(env_ids) - known
        if unknown:
            raise ValueError("unknown environments: " + ", ".join(sorted(unknown)))
        with self.lock:
            bindings = self.sessions.get(session_id, {}).get('account_sessions', {})
            entry = {"env_ids": env_ids, "revision": self.get(session_id)["revision"] + 1,
                     "account_sessions": {k:v for k,v in bindings.items() if k in env_ids}}
            self._save(session_id, entry)
            return self.get(session_id)

    def _save(self, session_id, entry):
        # Caller holds the scope lock; publish in-memory state only after disk commit.
        with self.lock:
            updated = {**self.sessions, session_id: entry}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "sessions": updated},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            self.sessions = updated

    def bind_account(self, session_id, parent, child):
        with self.lock:
            self.require(session_id, [parent])
            if self.sessions[session_id].get('account_sessions', {}).get(parent) == child:
                return
            entry = {**self.sessions[session_id]}
            entry['account_sessions'] = {**entry.get('account_sessions', {}), parent:child}
            self._save(session_id, entry)

    def selected_account(self, session_id, parent):
        with self.lock:
            self.require(session_id, [parent])
            return self.sessions[session_id].get('account_sessions', {}).get(parent)

    def require(self, session_id: str, env_ids: list[str]) -> None:
        allowed = self.get(session_id)["env_ids"]
        if not allowed:
            raise PermissionError("请先在本会话的「浏览器环境」中选择允许操作的环境。")
        denied = set(env_ids) - set(allowed)
        if denied:
            raise PermissionError("本会话无权操作环境：" + ", ".join(sorted(denied)))
