"""Link completed media-enabled objects into a separate development runtime.

The running user browser is never stopped or overwritten by this script.
Generate link-commands.json with ninja -t compdb -x solink after gn gen.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.work/media-playback'
BUILD = Path(r'E:\chromium\src\out\Release-pgo')
RUNTIME = OUT / 'browser'
RUNTIME.mkdir(parents=True, exist_ok=True)

for line in (OUT / 'runtime-deps.txt').read_text(encoding='utf-8-sig').splitlines():
    relative = line.strip().removeprefix('./')
    if not relative or Path(relative).suffix.lower() in {'.pdb', '.map'}:
        continue
    source = (BUILD / relative).resolve()
    assert source.is_relative_to(BUILD.resolve()), relative
    assert source.is_file(), source
    if source.name == 'chrome.dll':
        continue
    target = RUNTIME / source.relative_to(BUILD)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

commands = json.loads((OUT / 'link-commands.json').read_text(encoding='utf-8-sig'))
matches = [entry for entry in commands if entry.get('output') in ('chrome.dll', './chrome.dll')]
assert len(matches) == 1
executable, arguments = matches[0]['command'].split(' ', 1)
boundary = arguments.index(' "', arguments.index('/PDB:') + len('/PDB:'))
prefix, expanded = arguments[:boundary], arguments[boundary + 2:]
assert expanded.endswith('"') and 'obj/chrome/browser/core/env_overlay.obj' in expanded
arguments = prefix + ' ' + expanded[:-1]
# Leave memory for the user's active apps while optimizing changed modules.
arguments = arguments.replace('/opt:lldltojobs=all', '/opt:lldltojobs=4')
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
    for args in (['/setintegritylevel', '(OI)(CI)M', '/T', '/C'],
                 ['/grant', '*S-1-15-2-1:(OI)(CI)(RX)', '*S-1-15-2-2:(OI)(CI)(RX)', '/T', '/C']):
        subprocess.run(['icacls', str(RUNTIME), *args], check=True, capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
report = {'exit': result.returncode, 'seconds': round(time.monotonic() - started, 1),
    'runtime': str(RUNTIME), 'running_browser_unchanged': True}
if result.returncode == 0:
    report['dll_sha256'] = hashlib.sha256((RUNTIME / 'chrome.dll').read_bytes()).hexdigest()
(OUT / 'build-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report), flush=True)
raise SystemExit(result.returncode)
