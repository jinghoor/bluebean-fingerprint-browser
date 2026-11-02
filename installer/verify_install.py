"""Noninteractive acceptance checks for a disposable BlueBean installation.

Does not open a browser window, touch real user data, or require system Python
inside the installed application. Run with the build machine's Python.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
import winreg

from windows_security import file_integrity, sandbox_read_access
from payload_policy import assert_clean_payload


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def has_logo(path: Path, ico: Path) -> bool:
    """Compare embedded icon pixels against the original product ICO."""
    source = ico.read_bytes()
    expected = []
    for index in range(struct.unpack_from("<H", source, 4)[0]):
        size, offset = struct.unpack_from("<II", source, 6 + index * 16 + 8)
        expected.append(hashlib.sha256(source[offset:offset + size]).digest())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, pointer, ctypes.c_uint]
    kernel.LoadLibraryExW.restype = pointer
    kernel.FindResourceW.argtypes = [pointer, pointer, pointer]
    kernel.FindResourceW.restype = pointer
    kernel.SizeofResource.argtypes = [pointer, pointer]
    kernel.LoadResource.argtypes = [pointer, pointer]
    kernel.LoadResource.restype = pointer
    kernel.LockResource.argtypes = [pointer]
    kernel.LockResource.restype = pointer
    kernel.FreeLibrary.argtypes = [pointer]
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, pointer, pointer, pointer, pointer)
    kernel.EnumResourceNamesW.argtypes = [pointer, pointer, callback_type, pointer]
    module = kernel.LoadLibraryExW(str(path), None, 0x22)
    if not module:
        return False

    def resource(name, kind):
        entry = kernel.FindResourceW(module, name, kind)
        return ctypes.string_at(kernel.LockResource(kernel.LoadResource(module, entry)),
                                kernel.SizeofResource(module, entry))

    matches = []

    @callback_type
    def visit(_module, _kind, name, _param):
        group = resource(name, 14)
        pixels = []
        for index in range(struct.unpack_from("<H", group, 4)[0]):
            icon_id = struct.unpack_from("<H", group, 6 + index * 14 + 12)[0]
            pixels.append(hashlib.sha256(resource(icon_id, 3)).digest())
        matches.append(pixels == expected)
        return True

    try:
        kernel.EnumResourceNamesW(module, 14, visit, None)
        return any(matches)
    finally:
        kernel.FreeLibrary(module)


def imports(path: Path) -> list[str]:
    """Read ordinary PE imports without loading or executing the DLL."""
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    sections, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
    optional = pe + 24
    directory = optional + (112 if struct.unpack_from("<H", data, optional)[0] == 0x20B else 96)
    import_rva = struct.unpack_from("<I", data, directory + 8)[0]
    if not import_rva:
        return []

    def offset(rva: int) -> int:
        for index in range(sections):
            entry = optional + optional_size + index * 40
            virtual_size, virtual, raw_size, raw = struct.unpack_from("<IIII", data, entry + 8)
            if virtual <= rva < virtual + max(virtual_size, raw_size):
                return raw + rva - virtual
        raise ValueError(f"Unmapped PE address in {path}")

    result = []
    cursor = offset(import_rva)
    while any(data[cursor:cursor + 20]):
        name = offset(struct.unpack_from("<I", data, cursor + 12)[0])
        result.append(data[name:data.index(b"\0", name)].decode("ascii"))
        cursor += 20
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--require-uninstall-registration", action="store_true")
    args = parser.parse_args()
    app, payload, data = args.app.resolve(), args.payload.resolve(), args.data.resolve()
    assert app != data and not data.is_relative_to(app)
    assert_clean_payload(payload)
    assert_clean_payload(app)
    data.mkdir(parents=True, exist_ok=True)
    (data / "LocalAppData").mkdir(exist_ok=True)
    env = dict(os.environ, BLUEBEAN_HOME=str(app), BLUEBEAN_DATA=str(data),
               DSH_HOME=str(data / "DSH"), TEMP=str(data), TMP=str(data),
               LOCALAPPDATA=str(data / "LocalAppData"),
               BLUEBEAN_DSH_EXE=str(app / "dsh" / "DeepSeek Harness.exe"),
               BLUEBEAN_DSH_BIN=str(app / "dsh" / "resources" / "app.asar" / "dsh" /
                                    "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"))
    windows = Path(os.environ["SystemRoot"])
    env["PATH"] = os.pathsep.join(map(str, [windows / "System32", windows,
                                  windows / "System32/WindowsPowerShell/v1.0"]))
    for key in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH", "DEEPSEEK_API_KEY"):
        env.pop(key, None)
    results: dict = {"app": str(app), "data": str(data), "checks": {}}

    def run(name: str, command: list[str], marker: str, timeout: int = 60) -> None:
        started = time.monotonic()
        process = subprocess.run(command, cwd=app, env=env, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        passed = process.returncode == 0 and marker in process.stdout
        results["checks"][name] = {"passed": passed, "exit": process.returncode,
                                   "seconds": round(time.monotonic() - started, 2)}
        if not passed:
            results["checks"][name]["error"] = (process.stderr or process.stdout)[-2000:]

    paths = [p for p in payload.rglob("*") if p.is_file() and "__pycache__" not in p.parts
             and p.suffix not in {".pyc", ".log", ".dmp"} and p.name != "uninstall.ps1"]
    differences = []
    for source in paths:
        target = app / source.relative_to(payload)
        if not target.is_file() or file_hash(source) != file_hash(target):
            differences.append(str(source.relative_to(payload)))
    results["checks"]["payload_hashes"] = {"passed": not differences, "files": len(paths),
                                             "differences": differences}
    logo_paths = [app / "BlueBean.exe", app / "browser/chrome.exe",
                  payload.parent / "BlueBean-Setup-Windows-x64.exe"]
    logos = {path.name: has_logo(path, payload / "landou-browser.ico") for path in logo_paths}
    results["checks"]["product_icons"] = {"passed": all(logos.values()), **logos}
    results["native_imports"] = {str(p.relative_to(app)): imports(p) for p in
                                 [app / "BlueBean.exe", app / "browser/chrome.exe",
                                  app / "browser/chrome.dll", app / "runtime/python312.dll",
                                  app / "dsh/DeepSeek Harness.exe"]}
    run("private_python", [str(app / "runtime/python.exe"), "-I", "-c",
                           "import tkinter,websockets,ssl,sqlite3,ctypes,requests,socks,zstandard; "
                           "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; "
                           "from zoneinfo import ZoneInfo; ZoneInfo('America/Los_Angeles'); "
                           "a=AESGCM(AESGCM.generate_key(bit_length=256)); n=b'0'*12; "
                           "assert a.decrypt(n,a.encrypt(n,b'bluebean',None),None)==b'bluebean'; "
                           "z=zstandard.ZstdCompressor().compress(b'bluebean'); "
                           "assert zstandard.ZstdDecompressor().decompress(z)==b'bluebean'; "
                           "assert requests.certs.where(); print('runtime-ok')"], "runtime-ok")
    run("product_modules", [str(app / "runtime/python.exe"), "-I", "-c",
                            "import sys,runpy,importlib; "
                            "runpy.run_path('launch_env_window.pyw',run_name='package_probe'); "
                            "[importlib.import_module(n) for n in "
                            "['open_env_window','env_management','lan_bridge','environment_workspace']]; "
                            "print('product-imports-ok')"], "product-imports-ok")
    run("chromium_sandbox", [str(app / "browser/chrome.exe"), "--headless=new", "--disable-gpu",
                             "--no-first-run", "--user-data-dir=" + str(data / "BrowserProfile"),
                             "--dump-dom", "data:text/html,<p>bluebean-render-ok</p>"], "bluebean-render-ok")
    run("dsh_workbench", [str(app / "runtime/python.exe"), "-c",
                          "import _dsh_web;\ntry:\n r=_dsh_web.start(); "
                          "p=_dsh_web.HOME/'profiles'/'web'; "
                          "plugins=all((p/'node_modules'/name/'package.json').is_file() for name in ['lan-browser','lan-seats']); "
                          "print('workbench-ok' if r.get('ok') and plugins else 'workbench-failed')\nfinally:\n _dsh_web.stop()"],
        "workbench-ok", 240)

    try:
        labels = {str(path.relative_to(app)) if path != app else ".": file_integrity(path)
                  for path in [app, app / "BlueBean.exe", app / "runtime/pythonw.exe",
                               app / "browser/chrome.exe"]}
        results["checks"]["installed_integrity"] = {
            "passed": all(label >= 0x2000 for label in labels.values()),
            "integrity_rids": labels,
        }
    except Exception as exc:
        results["checks"]["installed_integrity"] = {
            "passed": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        access = {str(path.relative_to(app)) if path != app else ".": sandbox_read_access(path)
                  for path in [app, app / "BlueBean.exe", app / "runtime/pythonw.exe",
                               app / "browser/chrome.dll"]}
        results["checks"]["sandbox_file_access"] = {
            "passed": all(all(value.values()) for value in access.values()), "paths": access}
    except Exception as exc:
        results["checks"]["sandbox_file_access"] = {
            "passed": False, "error": f"{type(exc).__name__}: {exc}"}

    # Test the real native EXE in a fixture, with a harmless pythonw entry point.
    # The actual application files remain untouched and no UI is opened.
    fixture = data / "launcher path with spaces"
    fixture.mkdir(exist_ok=True)
    shutil.copy2(app / "BlueBean.exe", fixture / "BlueBean.exe")
    runtime_link = fixture / "runtime"
    subprocess.run([str(windows / "System32/cmd.exe"), "/c", "mklink", "/J",
                    str(runtime_link), str(app / "runtime")], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    marker = data / "launcher-result.json"
    (fixture / "launch_env_window.pyw").write_text(
        "import json,sys,pathlib\npathlib.Path(" + repr(str(marker)) + ").write_text("
        "json.dumps({'executable':sys.executable,'cwd':str(pathlib.Path.cwd())}))\n", encoding="utf-8")
    try:
        launch_env = dict(env, PYTHONHOME=str(data / "nonexistent-python"),
                          PYTHONPATH=str(data / "nonexistent-site"))
        subprocess.run([str(fixture / "BlueBean.exe")], cwd=windows, env=launch_env, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        launch = json.loads(marker.read_text()) if marker.exists() else {}
        results["checks"]["native_launcher"] = {
            "passed": Path(launch.get("executable", "")).name.lower() == "pythonw.exe"
            and Path(launch.get("cwd", "")).resolve() == fixture.resolve(), **launch}
    finally:
        runtime_link.rmdir()  # Remove only the junction, never its target.

    (data / "user-data-must-survive.txt").write_text("keep user data", encoding="utf-8")
    if args.require_uninstall_registration:
        key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{4F271ABA-D768-42B2-9A96-CE947AD56CBB}_is1"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as registration:
                install_location = winreg.QueryValueEx(registration, "InstallLocation")[0]
            registered = Path(install_location).resolve() == app
        except OSError:
            registered = False
        shortcut_probe = subprocess.run([
            str(windows / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
            "$w=New-Object -ComObject WScript.Shell; "
            "$r=@(); foreach($f in @('Desktop','Programs')) { "
            "$p=Join-Path ([Environment]::GetFolderPath($f)) '蓝豆指纹浏览器.lnk'; "
            "if(Test-Path -LiteralPath $p) { $s=$w.CreateShortcut($p); "
            "$r+=@{path=$p;target=$s.TargetPath;icon=$s.IconLocation} } }; "
            "ConvertTo-Json -InputObject @($r) -Compress"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        shortcuts = json.loads(shortcut_probe.stdout) if shortcut_probe.returncode == 0 else []
        shortcut = len(shortcuts) == 2 and all(
            Path(link["target"]).resolve() == app / "BlueBean.exe"
            and Path(link["icon"].rsplit(",", 1)[0]).resolve() == app / "landou-browser.ico"
            for link in shortcuts)
        results["checks"]["normal_install_registration"] = {
            "passed": registered and shortcut,
            "uninstall_registered": registered,
            "shortcut_created": shortcut,
            "shortcut_targets": shortcuts,
        }
    results["checks"]["plugin_template_after_startup"] = {"passed":
        file_hash(app / "lan_seats/lib/client.js") == file_hash(payload / "lan_seats/lib/client.js")}
    results["passed"] = all(item["passed"] for item in results["checks"].values())
    args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": results["passed"], "checks": results["checks"]}, ensure_ascii=True))
    raise SystemExit(0 if results["passed"] else 1)


if __name__ == "__main__":
    main()
