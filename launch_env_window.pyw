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
PRODUCT_DATA = pathlib.Path(os.environ.get("BLUEBEAN_DATA") or
                           LOCAL_APP_DATA / "BlueBean Fingerprint Browser" / "Data")
LOG = PRODUCT_DATA / "logs" / "shortcut-launch.log"
FIRST_RUN_MARKER = PRODUCT_DATA / ".first-run-runtime-ready-v1.json"

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
        canvas.create_text(width // 2, 211, text=("正在启动中" if FIRST_RUN_MARKER.is_file()
                                               else "首次启动，正在准备运行环境"),
                           fill="#64748b", font=("Microsoft YaHei UI", 11))
        if not FIRST_RUN_MARKER.is_file():
            canvas.create_text(width // 2, 235, text="仅首次需要准备运行环境，后续启动将自动跳过",
                               fill="#64748b", font=("Microsoft YaHei UI", 9))

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


def startup_failure_message(started_epoch: float) -> str:
    message = "蓝豆指纹浏览器未能启动。"
    try:
        import json
        status = json.loads((PRODUCT_DATA / ".startup-browser.json").read_text(encoding="utf-8"))
        if status.get("ok") is False and status.get("recorded_at", 0) >= started_epoch - 1:
            code = status.get("exit_code")
            if code is not None:
                message += f"\n浏览器进程提前退出，退出码：{code}（0x{code & 0xffffffff:08X}）。"
            else:
                message += "\n" + status.get("error", "启动未完成。")
            if status.get("log"):
                message += "\n浏览器日志：" + status["log"]
    except (OSError, ValueError, TypeError):
        pass
    return message + "\n启动日志：" + str(LOG)


def show_startup_failure(started_epoch: float, root=None) -> None:
    message = startup_failure_message(started_epoch)
    print(message, flush=True)
    try:
        if root is not None:
            from tkinter import messagebox
            messagebox.showerror("蓝豆指纹浏览器 — 启动失败", message, parent=root)
        elif sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "蓝豆指纹浏览器 — 启动失败", 0x10)
    except Exception as exc:
        print(f"startup error notice unavailable: {exc}", flush=True)


def run_without_splash() -> int:
    started = time.time()
    code = run_target()
    if code:
        show_startup_failure(started)
    return code


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    # Line buffered so the log is readable while the browser is still starting.
    stream = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    print(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} shortcut launch "
          f"(args={sys.argv[1:]})", flush=True)

    # Setup may delete its TEMP after launching us. Establish a persistent
    # product temp directory before Tk, Chromium or any helper is started.
    try:
        from _runtime_state import runtime_environment
        os.environ.update(runtime_environment(PRODUCT_DATA))
        import tempfile
        tempfile.tempdir = os.environ["TEMP"]
    except OSError:
        traceback.print_exc()
        show_startup_failure(time.time())
        return 1

    # The shortcut uses pythonw, so this small Tk window gives the operator
    # feedback while quiet proxy/assistant/browser initialization runs. The
    # worker keeps Tk responsive and reports completion back to the UI thread.
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return run_without_splash()

    print("startup notice: " + ("normal-startup" if FIRST_RUN_MARKER.is_file()
                                else "first-run-runtime-preparation"), flush=True)
    root = create_splash(tk, ttk)
    if root is None:
        return run_without_splash()

    result: dict[str, int] = {}
    started_at = time.monotonic()
    started_epoch = time.time()
    first_run = not FIRST_RUN_MARKER.is_file()

    def worker() -> None:
        result["code"] = run_target()
        result["done"] = 1

    def finish_when_ready() -> None:
        if result.get("done") and time.monotonic() - started_at >= 0.8:
            if result.get("code"):
                show_startup_failure(started_epoch, root)
            if first_run and result.get("code") == 0 and not FIRST_RUN_MARKER.is_file():
                failed = False
                try:
                    import json
                    status = json.loads((PRODUCT_DATA / ".startup-services.json").read_text(encoding="utf-8"))
                    failed = (status.get("started_at", 0) >= started_epoch - 1 and
                              status.get("status") in ("failed", "bridge-unavailable", "workbench-unavailable", "attachment-unavailable"))
                except (OSError, ValueError):
                    pass
                if not failed and time.monotonic() - started_at < 90:
                    root.after(100, finish_when_ready)
                    return
            root.destroy()
            return
        root.after(80, finish_when_ready)

    threading.Thread(target=worker, name="product-startup", daemon=True).start()
    root.after(80, finish_when_ready)
    root.mainloop()
    return result.get("code", 1)


def run_target() -> int:
    sys.argv = [str(TARGET), "--product-startup", *sys.argv[1:]]
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
