"""Read-only release checks: application runtimes may ship, user state may not."""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
from pathlib import Path
import re


APP_DIRECTORIES = frozenset({'assets', 'browser', 'dsh', 'lan_plugin', 'lan_seats', 'licenses', 'runtime', 'tools', '__pycache__'})
APP_FILES = frozenset({'bluebean.exe', 'landou-browser.ico', 'env_check.html', 'env_check.js'})
USER_FILES = frozenset(name.casefold() for name in (
    'envs.json', 'proxies.json', 'proxies.expected.json', 'env-management.json',
    'env-command.json', 'env-result.json', 'env-task-result.json', 'lan-layer-state.json',
    '.deepseek-key', '.lan-bridge.json', '.dsh-web.json', '.env-ui-port.json',
    '.startup-browser.json', '.startup-services.json', '.session-browser-scopes.json',
    '.lan-automation-port', '.lan-automation-token', '.first-run-runtime-ready-v1.json',
    '.env-check-service.json', 'Cookies', 'Cookies-journal', 'Login Data', 'Login Data-journal',
    'History', 'History-journal', 'Web Data', 'Web Data-journal', 'Preferences', 'Secure Preferences', 'Local State',
))


def startup_violations(path: Path) -> list[str]:
    """Reject the legacy automatic fixture generator even in otherwise clean payloads."""
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == 'DEFAULT_ENVS':
            problems.append('startup still contains automatic demo environment definitions')
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'cli' and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'create'):
            problems.append('startup still invokes environment creation')
    return sorted(set(problems))


def audit_payload(app: Path) -> dict:
    app = Path(app).resolve()
    if not app.is_dir():
        raise FileNotFoundError(app)
    violations, count = [], 0

    def reject(path, reason):
        violations.append({'path': path.relative_to(app).as_posix(), 'reason': reason})

    for directory, dirs, files in os.walk(app, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            if path.is_symlink() or path.is_junction():
                reject(path, 'directory link may import data outside the payload')
                dirs.remove(name)
            elif parent == app and name.casefold() not in APP_DIRECTORIES:
                reject(path, 'unapproved application directory; user data must stay outside the payload')
                dirs.remove(name)
            elif path.relative_to(app).as_posix().casefold() in ('dsh/profiles', 'browser/user data', 'browser/default'):
                reject(path, 'runtime includes a user profile')
                dirs.remove(name)
        for name in files:
            path = parent / name
            count += 1
            folded = name.casefold()
            if (parent == app and path.suffix.casefold() not in ('.py', '.pyw')
                    and folded not in APP_FILES and not re.fullmatch(r'unins\d+\.(exe|dat|msg)', folded)):
                reject(path, 'unapproved application file; data and test artifacts may not ship')
            if path.is_symlink():
                reject(path, 'file link may import data outside the payload')
            if (folded in USER_FILES or folded.endswith('.dpapi.json')
                    or fnmatch.fnmatchcase(folded, 'env-account*.json')):
                reject(path, 'personal environment, account, credential or browser state')
    startup = app / 'open_env_window.py'
    if not startup.is_file():
        reject(startup, 'missing startup entry')
    else:
        for reason in startup_violations(startup):
            reject(startup, reason)
    client = app / 'lan_seats/lib/client.js'
    if client.is_file():
        source = client.read_text(encoding='utf-8')
        token = re.search(r"\bconst\s+TOKEN\s*=\s*(['\"])(.*?)\1", source)
        if not token or token.group(2) != '__BLUEBEAN_BRIDGE_TOKEN_INJECTED_AT_RUNTIME__':
            reject(client, 'bundled plugin must contain the credential placeholder, never a development token')
    return {'passed': not violations, 'files_checked': count, 'violations': violations}


def assert_clean_payload(app: Path) -> dict:
    report = audit_payload(app)
    if not report['passed']:
        paths = ', '.join(row['path'] for row in report['violations'][:12])
        raise RuntimeError(f'Refusing to package user/test data or automatic demo startup: {paths}')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    args = parser.parse_args()
    report = audit_payload(args.app)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report['passed'] else 1)
