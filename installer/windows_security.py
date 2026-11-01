"""Windows integrity checks and finalization for locally delivered installers."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import subprocess


def _sid_integrity(sid: int) -> int:
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    api.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    api.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_uint32)
    return api.GetSidSubAuthority(sid, api.GetSidSubAuthorityCount(sid)[0] - 1)[0]


def file_integrity(path: Path) -> int:
    """Read the mandatory label by SID, independent of Windows display language."""
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    api.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.c_uint32, pointer, pointer, pointer, ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    api.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    api.GetAce.argtypes = [pointer, ctypes.c_uint32, ctypes.POINTER(pointer)]
    api.GetAce.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    sacl, descriptor = pointer(), pointer()
    error = api.GetNamedSecurityInfoW(str(path.resolve()), 1, 0x10, None, None, None,
                                     ctypes.byref(sacl), ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if sacl.value:
            count = ctypes.c_ushort.from_address(sacl.value + 4).value
            for index in range(count):
                ace = pointer()
                if not api.GetAce(sacl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if ctypes.c_ubyte.from_address(ace.value).value == 0x11:
                    return _sid_integrity(ace.value + 8)
        # Unlabelled Windows objects are treated as Medium integrity.
        return 0x2000
    finally:
        kernel.LocalFree(descriptor)


def process_integrity(pid: int) -> int:
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = pointer
    kernel.CloseHandle.argtypes = [pointer]
    api.OpenProcessToken.argtypes = [pointer, ctypes.c_uint32, ctypes.POINTER(pointer)]
    api.GetTokenInformation.argtypes = [pointer, ctypes.c_uint32, pointer,
                                        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    token = pointer()
    try:
        if not api.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        required = ctypes.c_uint32()
        api.GetTokenInformation(token, 25, None, 0, ctypes.byref(required))
        buffer = ctypes.create_string_buffer(required.value)
        if not api.GetTokenInformation(token, 25, buffer, required, ctypes.byref(required)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_integrity(pointer.from_buffer(buffer).value)
    finally:
        if token.value:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def sandbox_read_access(path: Path) -> dict[str, bool]:
    """Check explicit/inherited package read+execute ACEs on a file or folder."""
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    api.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.c_uint32, pointer, pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer)]
    api.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    api.GetAce.argtypes = [pointer, ctypes.c_uint32, ctypes.POINTER(pointer)]
    api.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(ctypes.c_wchar_p)]
    kernel.LocalFree.argtypes = [pointer]
    rights = {sid: [0, 0] for sid in ("S-1-15-2-1", "S-1-15-2-2")}
    dacl, descriptor = pointer(), pointer()
    error = api.GetNamedSecurityInfoW(str(path.resolve()), 1, 4, None, None,
                                     ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if dacl.value:
            count = ctypes.c_ushort.from_address(dacl.value + 4).value
            for index in range(count):
                ace = pointer()
                if not api.GetAce(dacl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                kind = ctypes.c_ubyte.from_address(ace.value).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                if kind not in (0, 1) or flags & 8:  # INHERIT_ONLY
                    continue
                sid = ctypes.c_wchar_p()
                if not api.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(sid)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if sid.value in rights:
                        rights[sid.value][kind] |= ctypes.c_uint32.from_address(ace.value + 4).value
                finally:
                    kernel.LocalFree(ctypes.cast(sid, pointer))
        required = 0x1200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
        return {sid: allowed & required == required and not denied & required
                for sid, (allowed, denied) in rights.items()}
    finally:
        kernel.LocalFree(descriptor)


def finalize_installer(path: Path) -> None:
    """Apply after the LAST copy into a delivery folder that may inherit Low IL."""
    path = path.resolve(strict=True)
    if path.suffix.lower() != ".exe":
        raise ValueError("Expected an installer EXE")
    subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/icacls.exe"),
                    str(path), "/setintegritylevel", "medium"], check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   creationflags=subprocess.CREATE_NO_WINDOW)
    if file_integrity(path) != 0x2000:
        raise RuntimeError("Final installer must have Medium integrity")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    args = parser.parse_args()
    finalize_installer(args.installer)
    print(f"Verified Medium integrity: {args.installer.resolve()}")
