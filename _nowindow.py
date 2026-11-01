"""Keep child processes from flashing console windows on Windows.

`CREATE_NO_WINDOW` gives the child a console that has no window; anything that child starts inherits it,
so a chain of console programs (runtime -> pwsh -> tool) never shows a window and still initialises.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Windows only; on other platforms a no-op so the call sites stay portable.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def console_python() -> str:
    """Use the console runtime with CREATE_NO_WINDOW, even from pythonw."""
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if not console.is_file():
            raise FileNotFoundError(f"Missing Python runtime: {console}")
        return str(console)
    return str(executable)


def no_window() -> int:
    """creationflags value for subprocess.* that hides the child's console."""
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def detached_no_window() -> int:
    """Flags for a long-lived background process that owns a hidden console.

    `CREATE_NO_WINDOW` gives the process a console without a window; `CREATE_NEW_PROCESS_GROUP` detaches it
    from the launcher's console so it survives the launcher. Children inherit the hidden console, so they
    never allocate one of their own.
    """
    if sys.platform != "win32":
        return 0
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def hidden_startup_info() -> "subprocess.STARTUPINFO | None":
    """Belt and braces: also ask for a hidden, non-activated window."""
    if sys.platform != "win32":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info
