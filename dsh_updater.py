"""Download and atomically install official DSH desktop updates.

The product runs DSH in its Web/CLI mode, so DSH's Electron update window is
not active. This small updater uses the same official stable feed and keeps
the product-owned DSH home outside the replaceable program directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from bluebean_paths import APP_ROOT, DATA_ROOT, DSH_EXE


FEED_URL = "https://download.deepseek.com/dsh-desk/feeds/win-x64/stable.yml"
USER_AGENT = "BlueBean-Fingerprint-Browser-Dsh-Updater"
_VERSION_RE = re.compile(r"^version:\s*([^\s]+)", re.MULTILINE)
_URL_RE = re.compile(r"^path:\s*>-\s*\n\s+(https?://\S+)", re.MULTILINE)
_SHA_RE = re.compile(r"^sha512:\s*>-\s*\n\s+([^\s]+)", re.MULTILINE)
_SIZE_RE = re.compile(r"^\s*size:\s+(\d+)", re.MULTILINE)


class DshUpdateError(RuntimeError):
    """An update could not be safely prepared or installed."""


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    """Return a comparison key for DSH's semver-like prerelease versions."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([a-z]+)\.(\d+))?", value)
    if not match:
        raise DshUpdateError(f"unsupported DSH version: {value}")
    phase = {None: 4, "alpha": 1, "beta": 2, "rc": 3}.get(match.group(4))
    if phase is None:
        raise DshUpdateError(f"unsupported DSH prerelease: {value}")
    return tuple(int(match.group(index)) for index in (1, 2, 3)) + (phase, int(match.group(5) or 0))


def parse_feed(text: str) -> dict[str, str | int]:
    """Parse the small, fixed official update manifest without PyYAML."""
    version = _VERSION_RE.search(text)
    url = _URL_RE.search(text)
    digest = _SHA_RE.search(text)
    size = _SIZE_RE.search(text)
    if not all((version, url, digest, size)):
        raise DshUpdateError("official DSH feed is incomplete")
    return {
        "version": version.group(1),
        "url": url.group(1),
        "sha512": digest.group(1),
        "size": int(size.group(1)),
    }


def read_feed(url: str = FEED_URL) -> dict[str, str | int]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return parse_feed(response.read().decode("utf-8", "replace"))


def _asar_file(archive: Path, relative: str) -> bytes:
    """Read one file from an Electron ASAR using its documented header."""
    with archive.open("rb") as stream:
        header = struct.unpack("<4I", stream.read(16))
        header_json = stream.read(header[3])
        tree = json.loads(header_json.decode("utf-8"))
        node = tree
        for part in relative.replace("\\", "/").split("/"):
            node = node["files"][part]
        if "offset" not in node or "size" not in node:
            raise DshUpdateError(f"ASAR entry is not a file: {relative}")
        # The ASAR header is padded to the size stored in its first field.
        # Older bundles happen to make this equal to ``16 + header[3]`` while
        # newer official DSH bundles can add a few padding bytes.
        data_offset = 8 + header[1] + int(node["offset"])
        stream.seek(data_offset)
        data = stream.read(int(node["size"]))
    if len(data) != int(node["size"]):
        raise DshUpdateError(f"ASAR entry is truncated: {relative}")
    return data


def installed_version(dsh_dir: Path | None = None) -> str:
    dsh_dir = (dsh_dir or DSH_EXE.parent).resolve()
    package = dsh_dir / "resources" / "app.asar"
    try:
        metadata = json.loads(_asar_file(package, "dsh/package.json").decode("utf-8"))
        version = str(metadata["version"])
        _version_key(version)
        return version
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DshUpdateError(f"cannot read installed DSH version: {dsh_dir}") from exc


def _verify(path: Path, release: dict[str, str | int]) -> None:
    if path.stat().st_size != int(release["size"]):
        raise DshUpdateError("downloaded DSH installer size does not match the official feed")
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual = base64.b64encode(digest.digest()).decode("ascii")
    if actual != release["sha512"]:
        raise DshUpdateError("downloaded DSH installer SHA-512 does not match the official feed")


def download_release(release: dict[str, str | int], destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"deepseek-harness-{release['version']}-win-x64.exe"
    if target.exists():
        _verify(target, release)
        return target
    temporary = target.with_suffix(".download")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(str(release["url"]), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
    _verify(temporary, release)
    temporary.replace(target)
    return target


def _extractor(explicit: Path | None) -> Path:
    candidates = [explicit] if explicit else []
    configured = os.environ.get("BLUEBEAN_DSH_EXTRACTOR", "").strip()
    if configured:
        candidates.append(Path(configured))
    for name in ("7z.exe", "7zr.exe", "7z", "7zr"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise DshUpdateError("7-Zip extractor is required to unpack the official DSH installer")


def extract_release(installer: Path, destination: Path, extractor: Path | None = None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    tool = _extractor(extractor)
    result = subprocess.run(
        [str(tool), "x", "-y", str(installer), f"-o{destination}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if result.returncode:
        shutil.rmtree(destination, ignore_errors=True)
        raise DshUpdateError(f"7-Zip could not unpack DSH: {result.stderr[-500:]}")
    if not (destination / "DeepSeek Harness.exe").is_file():
        shutil.rmtree(destination, ignore_errors=True)
        raise DshUpdateError("official DSH installer did not contain its executable")
    return destination


def _stop_owned_web() -> None:
    # Import lazily so checking an update never starts or changes DSH.
    try:
        import _dsh_web

        state = _dsh_web.load_state()
        if _dsh_web.owns_state(state):
            _dsh_web.stop()
    except Exception:
        # A stale or unreadable state must not cause us to kill another DSH.
        return


def install_payload(payload: Path, expected_version: str) -> dict[str, str]:
    payload = payload.resolve()
    if not (payload / "DeepSeek Harness.exe").is_file():
        raise DshUpdateError("payload does not contain DeepSeek Harness.exe")
    if installed_version(payload) != expected_version:
        raise DshUpdateError("payload version does not match the official release")

    current = DSH_EXE.parent.resolve()
    current.parent.mkdir(parents=True, exist_ok=True)
    _stop_owned_web()
    backup = current.parent / f"{current.name}.backup-{installed_version(current)}-{int(time.time())}"
    staging = current.parent / f"{current.name}.new-{uuid.uuid4().hex[:8]}"
    if staging.exists() or backup.exists():
        raise DshUpdateError("an update staging or backup directory already exists")
    shutil.move(str(payload), str(staging))
    moved_current = False
    try:
        current.replace(backup)
        moved_current = True
        staging.replace(current)
    except Exception:
        if moved_current and backup.exists() and not current.exists():
            backup.replace(current)
        # Keep a failed staging directory for diagnosis and a later retry.
        # It is outside the active DSH path and cannot affect the running app.
        raise
    journal = DATA_ROOT / "updates" / "dsh-last-update.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({
        "from": installed_version(backup), "to": expected_version,
        "current": str(current), "backup": str(backup),
        "data_root": str(DATA_ROOT.resolve()), "updated_at": int(time.time()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"from": installed_version(backup), "to": expected_version,
            "current": str(current), "backup": str(backup)}


def update(extractor: Path | None = None) -> dict[str, str | bool]:
    current = installed_version()
    release = read_feed()
    version = str(release["version"])
    if _version_key(version) <= _version_key(current):
        return {"updated": False, "current": current, "available": version}
    root = DATA_ROOT / "updates" / "dsh" / version
    installer = download_release(release, root)
    payload = extract_release(installer, root / "payload", extractor)
    result = install_payload(payload, version)
    return {"updated": True, **result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely update the embedded DSH from its official feed")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--extractor", type=Path)
    args = parser.parse_args()
    if args.check:
        current = installed_version()
        release = read_feed()
        print(json.dumps({"current": current, "available": release["version"],
                          "update_available": _version_key(str(release["version"])) > _version_key(current)},
                         ensure_ascii=False))
        return 0
    if args.update:
        print(json.dumps(update(args.extractor), ensure_ascii=False))
        return 0
    parser.error("choose --check or --update")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
