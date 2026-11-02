"""Upgrade a disposable install while real product services hold runtime files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from _runtime_state import process_identity

parser=argparse.ArgumentParser()
parser.add_argument('--installer',type=Path,required=True)
parser.add_argument('--app',type=Path,required=True)
parser.add_argument('--data',type=Path,required=True)
parser.add_argument('--report',type=Path,required=True)
args=parser.parse_args()
app,data=args.app.resolve(),args.data.resolve()
assert data.name=='Data' and 'BlueBeanAcceptance' in str(data)
assert 'BlueBeanAcceptance' in str(app)
report={'checks':{}}
tracked={}
for name in ('.lan-bridge.json','.dsh-web.json'):
    state=json.loads((data/name).read_text(encoding='utf-8'))
    pid=int(state['pid'])
    identity=process_identity(pid)
    assert identity.get('alive') and Path(identity['image']).is_relative_to(app)
    tracked[pid]=identity
report['old_service_pids']=list(tracked)
preserve=[data/'.first-run-runtime-ready-v1.json',data/'BrowserProfile/Default/envs.json']
(data/'upgrade-user-data-sentinel.txt').write_text('User data must survive a live-service upgrade.',encoding='utf-8')
preserve.append(data/'upgrade-user-data-sentinel.txt')
hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in preserve}
unrelated=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(240)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW)
try:
    report['checks']['real_background_services_running']=len(tracked)>=2
    started=time.monotonic()
    log=args.report.with_suffix('.install.log')
    install_report=args.report.with_suffix('.install.json')
    result=subprocess.run([sys.executable,'-B','-u',str(ROOT/'build/test-normal-install.py'),
        str(args.installer.resolve()),'--directory',str(app),'--acceptance',
        '--log',str(log),'--report',str(install_report)],cwd=ROOT,
        stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=180)
    report['seconds']=round(time.monotonic()-started,3)
    report['checks']['upgrade_installer_success']=result.returncode==0
    if result.returncode:
        report['installer_output']=(result.stdout+result.stderr)[-2500:]
    report['checks']['old_runtime_holders_stopped']=all(not process_identity(pid).get('alive') or
        process_identity(pid).get('started')!=identity.get('started') for pid,identity in tracked.items())
    report['checks']['unrelated_python_untouched']=unrelated.poll() is None
    report['checks']['user_data_unchanged']=all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in hashes.items())
    log_text=log.read_text(encoding='utf-8-sig',errors='replace') if log.is_file() else ''
    report['restart_manager_log']=[line for line in log_text.splitlines() if any(x in line.lower() for x in ('restartmanager','restart manager','close applications','closing applications','shutting down','application found'))][-25:]
    report['passed']=all(report['checks'].values())
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    assert report['passed'],'Hot upgrade regression failed; see report'
finally:
    if unrelated.poll() is None:
        unrelated.terminate();unrelated.wait(timeout=5)
    # Never touch another installation or a reused PID, including on failure.
    for pid,identity in tracked.items():
        current=process_identity(pid)
        if current.get('alive') and current.get('started')==identity.get('started'):
            subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
