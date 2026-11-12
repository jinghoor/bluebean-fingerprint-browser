"""Build a standard Windows setup with private Chromium, DSH and Python runtimes."""

from __future__ import annotations

import os
import importlib.metadata
import hashlib
import json
import shutil
import subprocess
import sys
import struct
import winreg
from datetime import datetime
from pathlib import Path
from installer.windows_security import finalize_installer
from installer.payload_policy import assert_clean_payload
from bluebean_version import VERSION


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "chromium"
OUT = SOURCE / "out" / "Release-pgo"
GN = SOURCE / "buildtools" / "win" / "gn.exe"
DSH = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "DeepSeek Harness"
if not DSH.exists():
    DSH = Path.home() / "AppData" / "Local" / "Programs" / "DeepSeek Harness"

ENTRY_MODULES = {
    "launch_env_window.pyw", "open_env_window.py", "_env_driver.py", "_nowindow.py",
    "_runtime_state.py", "_startup_services.py", "env_startup_check.py",
    "bluebean_paths.py", "bind_env_proxies.py", "fp_launcher.py", "fp_proxy.py",
    "env_seed.py", "env_cli.py", "_ensure_bridge.py", "_ensure_relays.py",
    "_dsh_web.py", "lan_bridge.py", "browser_backend.py", "dsh_sessions.py",
    "session_browser_scope.py", "dsh_client.py", "local_account_vault.py",
    "environment_agent.py", "environment_api.py", "environment_accounts.py",
    "environment_import.py", "environment_status.py",
    "environment_workspace.py", "environment_proxy.py", "env_management.py", "fp_identity.py",
}

# Include delayed imports as well as startup imports. These support SOCKS proxy
# testing, encrypted migration, compressed session logs, and IANA time zones.
RUNTIME_DISTRIBUTIONS = (
    "websockets", "requests", "certifi", "charset-normalizer", "idna", "urllib3",
    "PySocks", "cryptography", "cffi", "pycparser", "zstandard", "tzdata",
)


def copy_runtime_packages(destination: Path) -> None:
    site = destination / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    versions = {}
    for name in RUNTIME_DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        source_site = Path(distribution.locate_file("")).resolve()
        versions[name] = distribution.version
        if not distribution.files:
            raise RuntimeError(f"Runtime package has no file manifest: {name}")
        for relative in distribution.files:
            source = Path(distribution.locate_file(relative)).resolve()
            if (not source.is_relative_to(source_site) or not source.is_file()
                    or source.suffix == ".pyc" or "__pycache__" in source.parts):
                continue
            target = site / source.relative_to(source_site)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (destination / "bundled-packages.json").write_text(
        json.dumps({"python": sys.version, "packages": versions}, indent=2), encoding="utf-8")


def windows_sdk() -> Path:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows Kits\Installed Roots") as key:
        return Path(winreg.QueryValueEx(key, "KitsRoot10")[0])


def runtime_files() -> list[Path]:
    env = dict(os.environ)
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    env["WINDOWSSDKDIR"] = str(windows_sdk())
    result = subprocess.run([str(GN), "desc", str(OUT), "//chrome:chrome", "runtime_deps"],
                            cwd=SOURCE, env=env, check=True, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    files = []
    for line in result.stdout.splitlines():
        relative = line.strip().removeprefix("./")
        if not relative or Path(relative).suffix.lower() in {".pdb", ".map"}:
            continue
        path = (OUT / Path(relative)).resolve()
        if not path.is_relative_to(OUT.resolve()):
            raise RuntimeError(f"Chromium runtime dependency escaped build output: {relative}")
        if not path.is_file():
            raise FileNotFoundError(f"Missing Chromium runtime dependency: {path}")
        files.append(path)
    if not any(path.name.lower() == "chrome.exe" and path.parent == OUT.resolve() for path in files):
        raise RuntimeError("GN runtime dependency list does not include chrome.exe")
    return files


def local_python_modules() -> set[str]:
    import ast

    modules = set(ENTRY_MODULES)
    pending = list(ENTRY_MODULES)
    while pending:
        file_name = pending.pop()
        source = ROOT / file_name
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [node.module]
            else:
                continue
            for name in imports:
                dependency = name.split(".", 1)[0] + ".py"
                if (ROOT / dependency).is_file() and dependency not in modules:
                    modules.add(dependency)
                    pending.append(dependency)
    return modules


def copy_python_runtime(destination: Path) -> None:
    source = Path(sys.base_prefix)

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {"include", "libs", "Scripts", "__pycache__"}
        if Path(directory).name == "Lib":
            ignored.add("site-packages")
        return ignored.intersection(names)

    shutil.copytree(source, destination, ignore=ignore)
    copy_runtime_packages(destination)
    probe = subprocess.run([str(destination / "python.exe"), "-I", "-c",
                            "import tkinter,websockets,requests,socks,zstandard; "
                            "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; "
                            "from zoneinfo import ZoneInfo; ZoneInfo('America/Los_Angeles'); "
                            "print(websockets.__version__)"],
                           capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise RuntimeError("Bundled Python runtime failed its tkinter/websockets check: " + probe.stderr)


def build_windows_installer(release: Path) -> Path:
    release = release.resolve()
    clean = assert_clean_payload(release / "App")
    print(f"Clean-user payload check passed ({clean['files_checked']} files).", flush=True)
    tools = release / "App" / "tools"
    tools.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "installer" / "pnpm.cmd", tools / "pnpm.cmd")
    sdk = windows_sdk()
    sdk_version = max((path.name for path in (sdk / "Lib").iterdir() if path.is_dir()),
                      key=lambda value: tuple(int(part) for part in value.split(".")))
    llvm = SOURCE / "third_party" / "llvm-build" / "Release+Asserts" / "bin"
    build_dir = ROOT / "build" / "windows-installer"
    build_dir.mkdir(parents=True, exist_ok=True)
    obj = build_dir / "launcher.obj"
    resource = build_dir / "launcher.res"
    launcher = release / "App" / "BlueBean.exe"
    installer = release / "BlueBean-Setup-Windows-x64.exe"
    # Keep each build isolated from previously opened installer executables.
    inno_output = release / "inno-payload"
    inno_output.mkdir(exist_ok=True)
    installer_payload = inno_output / installer.name

    includes = sdk / "Include" / sdk_version
    subprocess.run([
        str(llvm / "clang-cl.exe"), "/nologo", "/c", "/O2", "/W0", "/GS-",
        "/Zl", "/DUNICODE", "/D_UNICODE",
        f"/I{includes / 'um'}", f"/I{includes / 'shared'}",
        f"/Fo{obj}", str(ROOT / "installer" / "launcher.cpp"),
    ], cwd=ROOT, check=True)
    subprocess.run([
        str(sdk / "bin" / sdk_version / "x64" / "rc.exe"), "/nologo",
        f'/dBlueBeanVersionText="{VERSION}"', f'/dBlueBeanVersionNumber={VERSION.replace(".", ",")},0',
        f"/fo{resource}", str(ROOT / "installer" / "launcher.rc"),
    ], cwd=ROOT, check=True)
    subprocess.run([
        str(llvm / "lld-link.exe"), "/nologo", "/subsystem:windows",
        "/entry:WinMainCRTStartup", "/nodefaultlib", "/machine:x64",
        f"/out:{launcher}", str(obj), str(resource),
        f"/libpath:{sdk / 'Lib' / sdk_version / 'um' / 'x64'}",
        "kernel32.lib", "user32.lib",
    ], cwd=ROOT, check=True)

    permission_obj = build_dir / "configure-permissions.obj"
    permission_helper = build_dir / "BlueBean-Permissions.exe"
    subprocess.run([
        str(llvm / "clang-cl.exe"), "/nologo", "/c", "/O2", "/W0", "/GS-", "/Zl",
        "/DUNICODE", "/D_UNICODE", f"/I{includes / 'um'}", f"/I{includes / 'shared'}",
        f"/Fo{permission_obj}", str(ROOT / "installer" / "configure_permissions.cpp"),
    ], cwd=ROOT, check=True)
    subprocess.run([
        str(llvm / "lld-link.exe"), "/nologo", "/subsystem:windows",
        "/entry:WinMainCRTStartup", "/nodefaultlib", "/machine:x64",
        f"/out:{permission_helper}", str(permission_obj), str(resource),
        f"/libpath:{sdk / 'Lib' / sdk_version / 'um' / 'x64'}",
        "kernel32.lib", "shell32.lib", "advapi32.lib",
    ], cwd=ROOT, check=True)

    compiler = Path(os.environ.get("BLUEBEAN_ISCC") or
                    ROOT / "build" / "tools" / "InnoSetup" / "ISCC.exe")
    if not compiler.is_file():
        raise FileNotFoundError("Install Inno Setup or set BLUEBEAN_ISCC to ISCC.exe")
    print("Building the standard Windows installation wizard...", flush=True)
    with (release / "inno-build.log").open("wb") as log:
        subprocess.run([str(compiler), f"/DPayloadRoot={release / 'App'}", f"/DProductVersion={VERSION}",
                        f"/DPermissionHelper={permission_helper}",
                        f"/DInstallerOutput={inno_output}", str(ROOT / "installer" / "BlueBean.iss")],
                       cwd=ROOT, check=True, timeout=3600, stdout=log, stderr=subprocess.STDOUT)
    if installer_payload.stat().st_size < 1024 * 1024:
        raise RuntimeError("Windows setup executable is unexpectedly small")
    # Inno's loader needs TEMP before any Pascal installer code runs. A native
    # GUI bootstrap selects a private directory without changing global ACLs.
    bootstrap_obj = build_dir / "setup-bootstrap.obj"
    bootstrap_res = build_dir / "setup-bootstrap.res"
    bootstrap = build_dir / "setup-bootstrap.exe"
    subprocess.run([
        str(llvm / "clang-cl.exe"), "/nologo", "/c", "/O2", "/W0", "/GS-", "/Zl",
        "/DUNICODE", "/D_UNICODE", f"/I{includes / 'um'}", f"/I{includes / 'shared'}",
        f"/Fo{bootstrap_obj}", str(ROOT / "installer" / "setup_bootstrap.cpp"),
    ], cwd=ROOT, check=True)
    subprocess.run([str(sdk / "bin" / sdk_version / "x64" / "rc.exe"), "/nologo",
                    f'/dBlueBeanVersionText="{VERSION}"', f'/dBlueBeanVersionNumber={VERSION.replace(".", ",")},0',
                    f"/fo{bootstrap_res}", str(ROOT / "installer" / "setup_bootstrap.rc")],
                   cwd=ROOT, check=True)
    subprocess.run([
        str(llvm / "lld-link.exe"), "/nologo", "/subsystem:windows", "/entry:WinMainCRTStartup",
        "/nodefaultlib", "/machine:x64", f"/out:{bootstrap}", str(bootstrap_obj),
        str(bootstrap_res), f"/libpath:{sdk / 'Lib' / sdk_version / 'um' / 'x64'}",
        "kernel32.lib", "user32.lib", "shell32.lib", "advapi32.lib", "bcrypt.lib",
    ], cwd=ROOT, check=True)
    wrapped = release / "BlueBean-Setup-Windows-x64.wrapped.exe"
    with bootstrap.open("rb") as stub, installer_payload.open("rb") as payload, wrapped.open("wb") as output:
        shutil.copyfileobj(stub, output)
        offset = output.tell()
        digest = hashlib.sha256()
        while chunk := payload.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
        output.write(struct.pack("<16sQQ32s", b"BBSETUP_PAYLOAD1", offset,
                                 installer_payload.stat().st_size, digest.digest()))
    wrapped.replace(installer)
    finalize_installer(installer)
    print(f"Installer: {installer} ({installer.stat().st_size / (1024 ** 3):.2f} GiB)")
    return installer


def build() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    release = ROOT / "build" / "installers" / f"BlueBean-{stamp}"
    app = release / "App"
    browser = app / "browser"
    app.mkdir(parents=True)
    browser.mkdir()

    print("Collecting Chromium runtime dependencies...")
    files = runtime_files()
    for index, source in enumerate(files, 1):
        target = browser / source.relative_to(OUT.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if index % 1000 == 0:
            print(f"  copied {index}/{len(files)} browser files")
    shutil.copy2(ROOT / "assets" / "landou-browser-v2.ico", app / "landou-browser.ico")
    logo = ROOT / "assets" / "startup-logo-transparent.png"
    if logo.exists():
        app_assets = app / "assets"
        app_assets.mkdir(parents=True, exist_ok=True)
        shutil.copy2(logo, app_assets / logo.name)

    for name in local_python_modules():
        shutil.copy2(ROOT / name, app / name)
    shutil.copy2(ROOT / "env_check.html", app / "env_check.html")
    shutil.copy2(ROOT / "env_check.js", app / "env_check.js")
    shutil.copytree(ROOT / "lan_plugin", app / "lan_plugin")
    shutil.copytree(ROOT / "lan_seats", app / "lan_seats",
                     ignore=shutil.ignore_patterns("__pycache__", "node_modules"))

    print("Copying the DSH runtime...")
    if not DSH.is_dir():
        raise FileNotFoundError(f"DeepSeek Harness installation was not found: {DSH}")
    shutil.copytree(DSH, app / "dsh", ignore=shutil.ignore_patterns("*.log", "*.dmp"))
    print("Bundling Python runtime...")
    copy_python_runtime(app / "runtime")

    for source, target in ((ROOT / "LICENSE", app / "licenses" / "BlueBean-LICENSE"),
                           (SOURCE / "LICENSE", app / "licenses" / "Chromium-LICENSE")):
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    # The product license refers to README.md; ship the public product text
    # alongside it rather than the development workspace's historical README.
    readme = ROOT / "opensource" / "README.md"
    if not readme.is_file():
        readme = ROOT / "README.md"
    shutil.copy2(readme, app / "licenses" / "README.md")

    for name in ("THIRD_PARTY_NOTICES.md", "LICENSES/DEEPSEEK-HARNESS-MIT.txt",
                 "LICENSES/CHROMIUM-BSD-3-CLAUSE.txt"):
        source = ROOT / name
        if not source.is_file():
            source = ROOT / "opensource" / name
        if source.is_file():
            target = app / "licenses" / Path(name).name
            shutil.copy2(source, target)

    return build_windows_installer(release)


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"BUILD FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
