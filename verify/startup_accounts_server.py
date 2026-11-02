"""Disposable real HTTP account portal for headless UI acceptance only."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env_startup_check import make_server
from environment_accounts import AccountStore
from local_account_vault import LocalAccountVault

KEY = 'isolated-startup-accounts-key-0001'
tmp = tempfile.TemporaryDirectory(prefix='bluebean-startup-portal-')
root = Path(tmp.name)
store = AccountStore(LocalAccountVault(root / 'env-account-vault.dpapi.json'))
first = store.save('portal', {'platform_id':'facebook', 'username':'运营主账号',
    'login':'operator@example.test', 'password':'fixture-local-password', 'otp_secret':'JBSWY3DPEHPK3PXP'})
second = store.save('portal', {'platform_id':'instagram', 'login':'second@example.test'})
store.save('portal', {'platform_id':'tiktok', 'login':'very-long-account-name-for-layout-check@example.test',
    'password':'fixture-second-password', 'otp_secret':'JBSWY3DPEHPK3PXQ'})
control = {'credential_delay':0, 'totp_delay':0, 'fail_totp':False,'network_mode':'china','network_delay':0}
counts = {'credential':0, 'totp':0, 'open':0, 'probes':0}
probe_attempts = {}
probe_lock = threading.Lock()
row = {'ok':True, 'env_id':'portal', 'display_name':'运营环境 · fb-020', 'proxy':'direct',
       'expected_exit_ip':'', 'ip_mode':'static', 'unlocked':False}

def command(p):
    if p.get('env_id') != 'portal' or p.get('key') != KEY:
        return {'ok':False, 'error':'expired fixture capability'}
    if p.get('action') == 'unlock': row['unlocked'] = True
    return copy.deepcopy(row)

def probe(proxy,url):
    with probe_lock:
        counts['probes'] += 1
        probe_attempts[url] = probe_attempts.get(url, 0) + 1
        attempt = probe_attempts[url]
    mode=control['network_mode']
    if control['network_delay'] and ('ipinfo.io' in url or 'gstatic.com' in url):
        time.sleep(control['network_delay'])
    if mode=='offline':return {'ok':False,'reachable':False,'status':0,'latency_ms':10}
    if mode=='usa' and 'ipinfo.io' in url:
        return {'ok':True,'body':json.dumps({'ip':'203.0.113.1','country':'US','city':'Test City','timezone':'America/Los_Angeles'}),'latency_ms':13}
    if 'myip.ipip.net' in url and mode!='ip_unavailable':
        return {'ok':True,'body':'当前IP：180.101.49.12 来自于：中国 江苏 南京 电信','latency_ms':16}
    if 'baidu.com' in url:return {'ok':True,'reachable':True,'status':200,'latency_ms':12}
    if 'bilibili.com' in url:return {'ok':False,'reachable':True,'status':412,'latency_ms':23}
    if mode=='cold_recovery' and any(site in url for site in ('gstatic.com','facebook.com','instagram.com','tiktok.com')):
        if attempt==1:return {'ok':False,'reachable':False,'status':0,'error':'ConnectTimeout','latency_ms':3001}
        time.sleep(.6)
        return {'ok':True,'reachable':True,'status':200,'latency_ms':36}
    return {'ok':False,'reachable':False,'status':0,'latency_ms':10}

def open_account(p):
    counts['open'] += 1
    return {'ok':True, 'session_env_id':first['session_env_id']}

server = make_server(root, command=command, probe=probe, account_store=store, account_command=open_account)
original = server.checks.account_action
def action(*args, **kwargs):
    name = args[2]
    if name in ('credential', 'totp'): counts[name] += 1
    time.sleep(control.get(name + '_delay', 0))
    if name == 'totp' and control['fail_totp']: raise ValueError('验证码暂时不可用，请重试')
    return original(*args, **kwargs)
server.checks.account_action = action
Base = server.RequestHandlerClass
class Handler(Base):
    def do_POST(self):
        if self.path == '/__test/control':
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            if data.pop('reset_network', False):
                server.checks.jobs.clear();row['unlocked']=False
                (root/'env-check/portal.json').unlink(missing_ok=True)
                with probe_lock:
                    probe_attempts.clear();counts['probes']=0
            control.update(data)
            return self.send({'ok':True, 'counts':counts})
        return super().do_POST()
server.RequestHandlerClass = Handler
Path(sys.argv[1]).write_text(json.dumps({'port':server.server_port,
    'url':f'http://127.0.0.1:{server.server_port}/?env=portal&key={KEY}',
    'account_id':first['account_id'], 'empty_account_id':second['account_id']}), encoding='utf-8')
try:
    server.serve_forever()
finally:
    server.server_close()
    tmp.cleanup()
