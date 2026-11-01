"""Windows DPAPI-backed storage for per-environment account credentials.

Only ciphertext is written to disk. Browser cookies and site storage stay in
Chromium's persistent StoragePartitions and are not handled by this module.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import tempfile
import threading
from functools import wraps
from ctypes import wintypes
from pathlib import Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("本机账号凭据加密仅支持 Windows DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(data)
    target = _DataBlob()
    if protect:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                       ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(_DataBlob)]
        fn.restype = wintypes.BOOL
        ok = fn(ctypes.byref(source), "BlueBean local environment account",
                None, None, None, 0x1, ctypes.byref(target))
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                       ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(_DataBlob)]
        fn.restype = wintypes.BOOL
        description = wintypes.LPWSTR()
        ok = fn(ctypes.byref(source), ctypes.byref(description), None, None, None,
                0x1, ctypes.byref(target))
        if description:
            kernel32.LocalFree(description)
    # Keep the source buffer alive until DPAPI returns.
    _ = source_buffer
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _locked(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return run


class LocalAccountVault:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self._summary_cache = None
        self._summary_stamp = None

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if document.get("version") != 1 or not isinstance(document.get("entries"), dict):
            raise ValueError("local account vault has an unsupported format")
        result = {}
        for env_id, ciphertext in document["entries"].items():
            raw = _dpapi(base64.b64decode(ciphertext, validate=True), protect=False)
            entry = json.loads(raw.decode("utf-8"))
            if isinstance(entry, dict):
                result[env_id] = entry
        return result

    def _write(self, entries: dict[str, dict]) -> None:
        encoded = {}
        for env_id, entry in entries.items():
            raw = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            encoded[env_id] = base64.b64encode(_dpapi(raw, protect=True)).decode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".env-vault-", suffix=".tmp",
                                         dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "entries": encoded}, stream,
                          ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            self._summary_cache = None
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def summary(self, env_id: str) -> dict:
        return self.summaries([env_id]).get(env_id, {"has_account": False})

    @_locked
    def summaries(self, env_ids: list[str]) -> dict[str, dict]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            stamp = None
        if self._summary_cache is None or self._summary_stamp != stamp:
            entries = self._read()
            # Cache public summaries only. Passwords and OTPs are not retained.
            summaries = {env_id: {"has_account": True,
                              "platform": str(entry.get("platform") or ""),
                              "label": str(entry.get("label") or ""),
                              "username": str(entry.get("username") or ""),
                              "has_password": bool(entry.get("password")),
                              "has_otp": bool(entry.get("otp_secret"))}
                         for env_id, entry in entries.items() if entry}
            self._summary_cache, self._summary_stamp = summaries, stamp
        return {env_id: dict(self._summary_cache.get(env_id, {"has_account": False}))
                for env_id in env_ids}

    @_locked
    def save(self, env_id: str, update: dict) -> dict:
        entries = self._read()
        old = entries.get(env_id, {})
        entry = {
            "platform": str(update.get("platform", old.get("platform", ""))).strip()[:80],
            "label": str(update.get("label", old.get("label", ""))).strip()[:120],
            "username": str(update.get("username", old.get("username", ""))).strip()[:240],
            "password": str(update.get("password") or old.get("password", ""))[:2048],
            "otp_secret": str(update.get("otp_secret") or old.get("otp_secret", "")).replace(" ", "")[:256],
        }
        if not any(entry.values()):
            entries.pop(env_id, None)
        else:
            entries[env_id] = entry
        self._write(entries)
        return self.summary(env_id)

    @_locked
    def delete(self, env_id: str) -> None:
        entries = self._read()
        if env_id in entries:
            del entries[env_id]
            self._write(entries)


def valid_env_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None
