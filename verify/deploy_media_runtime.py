"""Deploy an accepted media DLL without stopping the current development browser.

Windows may permit renaming a mapped image, but not replacing it directly.
Keep the old image as a backup; on a sharing violation leave everything intact.
"""
import hashlib
import json
from pathlib import Path
import os
import shutil
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.work/media-playback'
BUILD = Path(r'E:\chromium\src\out\Release-pgo')
source = OUT / 'browser/chrome.dll'
assert json.loads((OUT / 'build-result.json').read_text())['exit'] == 0
for name in ('decode-after.json', 'decode-native.json'):
    report = json.loads((OUT / name).read_text())
    assert report['passed'] and not report['baseline'], name
    assert all(case['ok'] for mode in report['modes'] for case in mode['results']), name
expected = json.loads((OUT / 'build-result.json').read_text())['dll_sha256']
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
assert digest(source) == expected
target = BUILD / 'chrome.dll'
old_hash = digest(target)
report = {'installed_product_modified': False, 'installer_built': False, 'uploaded': False,
    'browser_restarted': False, 'dll_sha256': expected, 'development_output': str(BUILD)}
if old_hash == expected:
    report['deployed'] = True
else:
    # Refuse to replace a DLL changed independently after this task's baseline.
    assert old_hash == '69584d02974cde84827d9017364afb7b6b71ddf381a532915223fac8b881293b', 'development DLL changed independently'
    candidate = BUILD / ('chrome.media-' + expected[:12] + '.dll')
    backup = BUILD / ('chrome.pre-media-' + old_hash[:12] + '.dll')
    assert not backup.exists(), 'backup path already exists'
    shutil.copy2(source, candidate)
    assert digest(candidate) == expected
    try:
        os.rename(target, backup)
    except PermissionError:
        report.update(deployed=False, pending_file=str(candidate), reason='mapped DLL is locked; close the development browser before retrying')
    else:
        try:
            os.replace(candidate, target)
        except BaseException:
            os.rename(backup, target)
            raise
        report.update(deployed=True, backup=str(backup))
report['restart_required_for_current_browser'] = True
report['recorded_at'] = time.time()
(OUT / 'deployment.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
