"""Compatibility flags shared by the product and fingerprint launchers."""
from __future__ import annotations

import sys


def compatible_browser_argv(argv: list[str]) -> list[str]:
    """Keep browser-process ACG off on Windows without disabling the sandbox.

    Some Windows installations cannot start our modified Chromium when the
    BrowserDynamicCodeDisabled experiment is enabled. Pin the shipping default
    explicitly; managed DynamicCodeSettings policy still takes precedence in
    Chromium. Renderer mitigations and other feature switches are unchanged.
    Merge duplicate switches: Chromium would otherwise retain only the last.
    """
    result = list(argv)
    if sys.platform != "win32":
        return result

    prefix = "--disable-features="
    features: list[str] = []
    result = []
    position = None
    switches = True
    for argument in argv:
        if argument == "--":
            switches = False
            if position is None:
                position = len(result)
        if switches and argument.startswith(prefix):
            if position is None:
                position = len(result)
            for feature in argument[len(prefix):].split(","):
                feature = feature.strip()
                if feature and feature not in features:
                    features.append(feature)
        else:
            result.append(argument)

    # A disabled feature wins over --enable-features, including field trials.
    # Replace any decorated spelling with the plain feature name.
    features = [feature for feature in features
                if feature.split("<", 1)[0].split(":", 1)[0] != "BrowserDynamicCodeDisabled"]
    features.append("BrowserDynamicCodeDisabled")
    result.insert(len(result) if position is None else position,
                  prefix + ",".join(features))
    return result
