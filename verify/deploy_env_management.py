"""Deploy without touching the browser, only when the local bridge is idle."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from _nowindow import no_window
from _ensure_bridge import load_config, ensure_bridge
from _dsh_web import sync_seat_plugin

port,token=load_config()
def api(path, body=None):
    req=urllib.request.Request('http://127.0.0.1:'+str(port)+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=15) as response: return json.load(response)

def inspection_running():
    job=api('/v1/lan/manage/proxy-job',{}).get('job')
    return bool(job and job.get('status') in ('running','cancelling'))

health=api('/v1/health')
if health.get('env_management_api') != 2 or '--restart' in sys.argv:
    # Old bridge lacks a busy health bit; conservatively require no SDK sessions.
    if health.get('chat_busy') or api('/v1/sessions').get('sessions') or inspection_running():
        raise SystemExit('Bridge sessions exist; deployment deferred, no process changed.')
    script=f"$listener=Get-NetTCPConnection -LocalPort {port} -State Listen; $bridgePid=$listener.OwningProcess; $proc=Get-CimInstance Win32_Process -Filter ('ProcessId='+$bridgePid); [pscustomobject]@{{pid=$bridgePid;name=$proc.Name;command=$proc.CommandLine}} | ConvertTo-Json -Compress"
    proc=json.loads(subprocess.check_output(['powershell','-NoProfile','-Command',script],creationflags=no_window(),text=True))
    if proc['name'].lower() not in ('python.exe','pythonw.exe') or 'lan_bridge.py' not in proc['command']:
        raise SystemExit('Listener identity mismatch; deployment deferred.')
    # Check again immediately before stopping the identified bridge alone.
    if api('/v1/health').get('chat_busy') or api('/v1/sessions').get('sessions') or inspection_running(): raise SystemExit('Bridge became busy; deployment deferred.')
    subprocess.run(['taskkill','/PID',str(proc['pid']),'/F'],check=True,capture_output=True,creationflags=no_window())
    result=ensure_bridge(verbose=False)
    if not result['ok']: raise SystemExit('Bridge restart failed; see logs/lan-bridge.log')
sync_seat_plugin()
health=api('/v1/health')
rows=api('/v1/lan/manage/list',{})
baseline_path = Path(sys.argv[sys.argv.index('--baseline')+1]) if '--baseline' in sys.argv else ROOT/'.work/env-management-upgrade/baseline.json'
baseline=json.loads(baseline_path.read_text(encoding='utf-8'))
current=json.loads(Path('E:/chromium-envs/env-ui/Default/envs.json').read_text(encoding='utf-8'))
assert current==baseline['env_state'], 'Native definitions changed; inspect before proceeding.'
assert health['env_management_api']==2 and rows['api_version']==2
assert sorted(e['env_id'] for e in rows['envs'])==sorted(e['env_id'] for e in baseline['env_state']['envs'])
assert rows['active']==baseline['env_state']['active']
profile=Path('E:/chromium-envs/env-ui/Default')
assert all((profile/c['path']).is_file() for c in baseline['cookies'])
report={'ok':True,'env_management_api':health['env_management_api'],
        'native_definitions_unchanged':True,'active':rows['active'],
        'env_ids':[e['env_id'] for e in rows['envs']], 'cookie_files_present':len(baseline['cookies']),
        'browser_restarted':False,'time':time.time()}
output_dir = Path(sys.argv[sys.argv.index('--output')+1]) if '--output' in sys.argv else ROOT/'.work/env-management-upgrade'
output_dir.mkdir(parents=True,exist_ok=True)
(output_dir/'deployment.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
