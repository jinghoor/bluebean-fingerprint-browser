"""Ensure pnpm hardlinks cannot leak bridge settings between product homes."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

parser=argparse.ArgumentParser()
parser.add_argument('--app',type=Path,required=True)
parser.add_argument('--data',type=Path,required=True)
args=parser.parse_args()
app=args.app.resolve()
base=args.data.resolve()
template=base/'Template/lan_seats/lib/client.js'
template.parent.mkdir(parents=True,exist_ok=True)
original=(app/'lan_seats/lib/client.js').read_bytes()
template.write_bytes(original)
homes=[]
for i in range(2):
    home=base/str(i)
    plugin=home/'DSH/profiles/web/node_modules/lan-seats/lib/client.js'
    plugin.parent.mkdir(parents=True,exist_ok=True)
    os.link(template,plugin)
    config={'port':52000+i,'token':secrets.token_hex(16)}
    (home/'.lan-bridge.json').write_text(json.dumps(config))
    homes.append((home,plugin,config))
for home,plugin,config in homes:
    env=dict(os.environ,BLUEBEAN_HOME=str(base/'Template'),BLUEBEAN_DATA=str(home),DSH_HOME=str(home/'DSH'))
    process=subprocess.run([str(app/'runtime/python.exe'),'-I','-B','-c',
        'import sys;sys.path.insert(0,'+repr(str(app))+');import _dsh_web;_dsh_web.sync_seat_plugin()'],
        env=env,cwd=app,stdin=subprocess.DEVNULL,capture_output=True,text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
    assert process.returncode==0,process.stderr
    assert template.read_bytes()==original,'Bundled template was modified through a hardlink'
for home,plugin,config in homes:
    text=plugin.read_text(encoding='utf-8')
    assert str(config['port']) in text and config['token'] in text
    assert all(other['token'] not in text for _,_,other in homes if other!=config)
print(json.dumps({'template_immutable':True,'two_home_credentials_isolated':True,'passed':True}))
