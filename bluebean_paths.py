"""Shared install and per-user data paths for BlueBean Browser."""

from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(os.environ.get("BLUEBEAN_HOME") or Path(__file__).resolve().parent)
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
DATA_ROOT = Path(os.environ.get("BLUEBEAN_DATA") or
                 LOCAL_APP_DATA / "BlueBean Fingerprint Browser" / "Data")

_PACKAGED_CHROME = APP_ROOT / "browser" / "chrome.exe"
_PACKAGED_DSH_EXE = APP_ROOT / "dsh" / "DeepSeek Harness.exe"
_PACKAGED_DSH_BIN = (APP_ROOT / "dsh" / "resources" / "app.asar" / "dsh" /
                     "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")

CHROME_EXE = Path(os.environ.get("BLUEBEAN_CHROME") or
                  (_PACKAGED_CHROME if _PACKAGED_CHROME.is_file() else
                   APP_ROOT / "browser" / "chrome.exe"))
PROFILE_ROOT = Path(os.environ.get("BLUEBEAN_PROFILE_ROOT") or
                    DATA_ROOT)
PROFILE_DIR = Path(os.environ.get("BLUEBEAN_PROFILE") or (PROFILE_ROOT / "env-ui"))
PROXY_CONFIG = Path(os.environ.get("BLUEBEAN_PROXY_CONFIG") or (PROFILE_ROOT / "proxies.json"))

DSH_EXE = Path(os.environ.get("BLUEBEAN_DSH_EXE") or
               (_PACKAGED_DSH_EXE if _PACKAGED_DSH_EXE.is_file() else
                LOCAL_APP_DATA / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe"))
DSH_BIN = Path(os.environ.get("BLUEBEAN_DSH_BIN") or
               DSH_EXE.parent / "resources" / "app.asar" / "dsh" / "node_modules" /
               "@deepseek-ai" / "dsh" / "lib" / "bin.js")


def data_path(*parts: str) -> Path:
    return DATA_ROOT.joinpath(*parts)
