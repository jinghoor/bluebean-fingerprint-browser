"""Desktop-shortcut entry point: no console window, everything logged to a file.

`open_env_window.py` prints progress, and under `pythonw.exe` `sys.stdout` is `None`, so a plain
`print()` there raises AttributeError and the launch dies silently. This wrapper redirects both streams
to logs/shortcut-launch.log before running the real script, so a double-click either opens the window or
leaves a readable reason in the log.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import runpy
import sys
import threading
import time
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TARGET = HERE / "open_env_window.py"
LOCAL_APP_DATA = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / "AppData" / "Local")
PRODUCT_DATA = LOCAL_APP_DATA / "BlueBean Fingerprint Browser" / "Data"
LOG = PRODUCT_DATA / "logs" / "shortcut-launch.log"

# The installed runtime must not inherit another Python installation's paths.
for _python_variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
    os.environ.pop(_python_variable, None)

os.environ["BLUEBEAN_HOME"] = str(HERE)
os.environ["BLUEBEAN_DATA"] = str(PRODUCT_DATA)
os.environ["BLUEBEAN_CHROME"] = str(HERE / "browser" / "chrome.exe")
os.environ["BLUEBEAN_PROFILE"] = str(PRODUCT_DATA / "BrowserProfile")
os.environ["BLUEBEAN_PROFILE_ROOT"] = str(PRODUCT_DATA)
os.environ["BLUEBEAN_PROXY_CONFIG"] = str(PRODUCT_DATA / "proxies.json")
os.environ["BLUEBEAN_DSH_EXE"] = str(HERE / "dsh" / "DeepSeek Harness.exe")
os.environ["BLUEBEAN_DSH_BIN"] = str(HERE / "dsh" / "resources" / "app.asar" / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")
os.environ["DSH_HOME"] = str(PRODUCT_DATA / "DSH")


def create_splash(tk, ttk):
    """Create a compact, polished startup notice; Tk remains optional."""
    try:
        root = tk.Tk()
        root.withdraw()
        root.title("蓝豆指纹浏览器")
        root.resizable(False, False)
        root.configure(background="#ffffff")
        root.overrideredirect(True)
        width, height = 450, 300
        root.attributes("-topmost", True)

        canvas = tk.Canvas(root, width=width, height=height, background="#ffffff",
                           highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        # Use the transparent product mark so the splash and browser chrome
        # share the same silhouette without a white square behind it.
        logo_path = HERE / "assets" / "startup-logo-transparent.png"
        logo = tk.PhotoImage(file=str(logo_path))
        root._startup_logo = logo  # Tk images must remain strongly referenced.
        canvas.create_image(width // 2, 87, image=logo)
        canvas.create_text(width // 2, 177, text="蓝豆指纹浏览器",
                           fill="#1f2937", font=("Microsoft YaHei UI", 17, "bold"))
        canvas.create_text(width // 2, 211, text="正在启动中",
                           fill="#64748b", font=("Microsoft YaHei UI", 11))

        # A small indeterminate track communicates progress without a timer-heavy widget.
        left, right, top, bottom = 86, width - 86, 258, 264
        radius = 3
        canvas.create_rectangle(left + radius, top, right - radius, bottom,
                                fill="#e9f1ff", outline="")
        canvas.create_oval(left, top, left + radius * 2, bottom,
                           fill="#e9f1ff", outline="")
        canvas.create_oval(right - radius * 2, top, right, bottom,
                           fill="#e9f1ff", outline="")
        segment_width = 82
        segment = canvas.create_rectangle(left, top, left + segment_width, bottom,
                                          fill="#4d8dff", outline="")

        def animate(offset=0):
            if not root.winfo_exists():
                return
            span = right - left - segment_width
            x1 = left + (offset % (span * 2))
            if x1 > right - segment_width:
                x1 = right - segment_width - (x1 - (right - segment_width))
            canvas.coords(segment, x1, top, x1 + segment_width, bottom)
            root.after(18, animate, offset + 4)

        def center_window():
            # Apply the position both before and just after mapping. Windows can
            # ignore geometry on a withdrawn, borderless Tk window until it has
            # been mapped, which otherwise leaves the splash at (0, 0).
            if not root.winfo_exists():
                return
            root.update_idletasks()
            x = max(0, (root.winfo_screenwidth() - width) // 2)
            y = max(0, (root.winfo_screenheight() - height) // 2)
            root.geometry(f"{width}x{height}+{x}+{y}")

        center_window()
        root.deiconify()
        root.after(80, center_window)
        root.after(18, animate)
        return root
    except Exception as exc:                                       # noqa: BLE001
        print(f"startup notice unavailable: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    # Line buffered so the log is readable while the browser is still starting.
    stream = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    print(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} shortcut launch "
          f"(args={sys.argv[1:]})", flush=True)

    # The shortcut uses pythonw, so this small Tk window gives the operator
    # feedback while quiet proxy/assistant/browser initialization runs. The
    # worker keeps Tk responsive and reports completion back to the UI thread.
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return run_target()

    root = create_splash(tk, ttk)
    if root is None:
        return run_target()

    result: dict[str, int] = {}
    started_at = time.monotonic()

    def worker() -> None:
        result["code"] = run_target()
        result["done"] = 1

    def finish_when_ready() -> None:
        if result.get("done") and time.monotonic() - started_at >= 0.8:
            root.destroy()
            return
        root.after(80, finish_when_ready)

    threading.Thread(target=worker, name="product-startup", daemon=True).start()
    root.after(80, finish_when_ready)
    root.mainloop()
    return result.get("code", 1)


def run_target() -> int:
    sys.argv = [str(TARGET), *sys.argv[1:]]
    try:
        runpy.run_path(str(TARGET), run_name="__main__")
    except SystemExit as exit_signal:
        print(f"=== exit {exit_signal.code}", flush=True)
        return int(exit_signal.code or 0)
    except BaseException:
        traceback.print_exc()
        print("=== FAILED (traceback above)", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
