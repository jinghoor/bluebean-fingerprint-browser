"""Link the current objects into an independent runtime; never stop a user browser."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.work/auth-popup'
BUILD = Path(r'E:\chromium\src\out\Release-pgo')
RUNTIME = OUT / 'browser'
RUNTIME.mkdir(parents=True, exist_ok=True)
template = Path(json.loads((ROOT / '.work/clean-first-run/stage.json').read_text())['app']) / 'browser'
if not (RUNTIME / 'chrome.exe').exists():
    shutil.copytree(template, RUNTIME, dirs_exist_ok=True)

command = json.loads((OUT / 'link-command.json').read_text())['command']
executable, arguments = command.split(' ', 1)
# ninja compdb -x expands the response inside its enclosing shell quotes.
boundary = arguments.index(' "', arguments.index('/PDB:') + len('/PDB:'))
prefix, expanded = arguments[:boundary], arguments[boundary + 2:]
assert expanded.endswith('"') and 'obj/chrome/browser/core/env_overlay.obj' in expanded
arguments = prefix + ' ' + expanded[:-1]
for flag, name in (('OUT', 'chrome.dll'), ('PDB', 'chrome.dll.pdb'), ('IMPLIB', 'chrome.dll.lib')):
    arguments, count = re.subn(r'"/' + flag + r':[^\"]+"',
                               lambda _: '"/' + flag + ':' + (RUNTIME / name).as_posix() + '"', arguments)
    assert count == 1, flag
rsp = OUT / 'isolated-link.rsp'
rsp.write_text(arguments, encoding='utf-8')
started = time.monotonic()
with (OUT / 'link.log').open('w') as log:
    result = subprocess.run([str((BUILD / executable).resolve()), '@' + str(rsp)], cwd=BUILD,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
if result.returncode == 0:
    subprocess.run(['icacls', str(RUNTIME), '/setintegritylevel', '(OI)(CI)M', '/T', '/C'],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run(['icacls', str(RUNTIME), '/grant', '*S-1-15-2-1:(OI)(CI)(RX)',
                    '*S-1-15-2-2:(OI)(CI)(RX)', '/T', '/C'],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
print(json.dumps({'exit': result.returncode, 'seconds': round(time.monotonic()-started, 1),
                  'runtime': str(RUNTIME), 'log': str(OUT / 'link.log')}), flush=True)
raise SystemExit(result.returncode)
