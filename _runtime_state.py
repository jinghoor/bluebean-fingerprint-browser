"""Private service state, process identity and startup serialization."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import tempfile
import time


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def atomic_text(path: Path, value: str) -> None:
    """Replace a file without modifying any other hardlink to its old inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid: int) -> dict:
    if not pid:
        return {"alive": False}
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return {"alive": True}
        except OSError:
            return {"alive": False}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = pointer
    kernel.CloseHandle.argtypes = [pointer]
    kernel.WaitForSingleObject.argtypes = [pointer, ctypes.c_uint32]
    kernel.QueryFullProcessImageNameW.argtypes = [pointer, ctypes.c_uint32,
                                                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.GetProcessTimes.argtypes = [pointer] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    handle = kernel.OpenProcess(0x101000, False, pid)
    if not handle:
        return {"alive": False}
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:
            return {"alive": False}
        image = ctypes.create_unicode_buffer(32768)
        length = ctypes.c_uint32(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            return {"alive": True}
        times = [ctypes.c_uint64() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return {"alive": True, "image": image.value}
        return {"alive": True, "image": image.value, "started": times[0].value}
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def startup_lock(path: Path, timeout: float = 30):
    """Serialize launchers for one data directory; never lock another install."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Service startup still busy: {path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
