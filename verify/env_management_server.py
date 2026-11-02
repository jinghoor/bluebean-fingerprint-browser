"""Temporary-profile HTTP fixture used by the isolated real-browser tests."""
import json
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_env_management import fixture
from lan_bridge import make_handler
from env_management import ManagementError

tmp = tempfile.TemporaryDirectory(prefix='bluebean-ui-test-')
bridge = fixture(tmp.name, probe=lambda proxy: (time.sleep(.5) or {'ok':True,'exit_ip':'203.0.113.10',
    'latency_ms':42,'city':'测试城市','region':'测试地区','country':'测试国家','timezone':'Asia/Shanghai',
    'asn':'AS64500','org':'测试网络','geo_source':'fixture','geo_ip':'203.0.113.10'}))
bridge.env_management.background_live = True
base_save = bridge.account_vault.save
control = {'fail_account':False,'fail_list':False,'fail_save':False,'fail_job_status':False,'proxy_delay':.5}
native_counts = {'native_attempts':0,'native_writes':0}
base_command = bridge.env_management._command
def command(payload):
    native_counts['native_attempts'] += 1
    if control.get('native_offline'): raise ManagementError('浏览器尚未启动或暂时未响应',503,outcome_unknown=False)
    result = base_command(payload)
    native_counts['native_writes'] += 1
    return result
bridge.env_management._command = command
bridge.env_management._probe=lambda proxy: (time.sleep(control['proxy_delay']) or {'ok':True,'exit_ip':'203.0.113.10',
    'latency_ms':42,'city':'测试城市','region':'测试地区','country':'测试国家','timezone':'Asia/Shanghai',
    'asn':'AS64500','org':'测试网络','geo_source':'fixture','geo_ip':'203.0.113.10'})
def save_account(*args, **kwargs):
    if control['fail_account']: raise OSError('injected temporary vault failure')
    return base_save(*args, **kwargs)
bridge.account_vault.save = save_account
base_record = bridge.env_management._record
def record(*args, **kwargs):
    if control.get('fail_records'): raise OSError('injected maintenance write failure')
    return base_record(*args, **kwargs)
bridge.env_management._record = record
dispatch = bridge.env_management.dispatch
def controlled(action, payload):
    if control['fail_job_status'] and action=='proxy-job' and payload.get('action','status')=='status': raise ConnectionError('injected status disconnect')
    if control['fail_list'] and action=='list': raise ConnectionError('injected disconnect')
    if control['fail_save'] and action=='save': raise OSError('injected disk error')
    return dispatch(action,payload)
bridge.env_management.dispatch = controlled

class Handler(make_handler(bridge, 'isolated-tests')):
    def do_POST(self):
        if self.path == '/__test/control':
            payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            if payload.pop('forget_jobs',False):
                with bridge.env_management.job_lock: bridge.env_management.jobs={}
            control.update(payload)
            bridge.backend.offline=bool(control.get('native_offline'))
            self._send({'ok':True,**native_counts}); return
        return super().do_POST()

server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
Path(sys.argv[1]).write_text(json.dumps({'port':server.server_port,'profile':tmp.name}), encoding='utf-8')
try: server.serve_forever()
finally: tmp.cleanup()
