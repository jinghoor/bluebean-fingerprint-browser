"""Run two real DSH backends using disposable homes and no visible windows.

This checks local coexistence, not paid model responses or the Desktop UI.
No existing DSH process or profile is stopped or changed.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import http.cookiejar
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _runtime_state import dsh_environment
from _nowindow import no_window
from websockets.sync.client import connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dsh-exe', required=True, type=Path)
    parser.add_argument('--standalone-dsh-exe', type=Path,
                        help='use the separately installed Desktop executable for the second backend')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--work-dir', type=Path, help='Parent for disposable homes accessible to the tested process identity')
    args = parser.parse_args()
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    exe = args.dsh_exe.resolve()
    standalone_exe = args.standalone_dsh_exe.resolve() if args.standalone_dsh_exe else exe
    report = {'checks': {}, 'model_response_tested': False, 'desktop_ui_tested': False}
    processes = []
    logs = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='bluebean-dsh-coexist-', dir=args.work_dir) as directory:
        base = Path(directory)

        def launch(name, embedded):
            launch_exe = exe if embedded else standalone_exe
            cli = launch_exe.parent / 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'
            data = base / name
            data.mkdir(exist_ok=True)
            if embedded:
                # A launcher can itself be a child of another DSH Desktop.
                parent = dict(os.environ, DSH_HOME=str(base / 'foreign'),
                              DSH_DESKTOP_NODE_EXECUTABLE='foreign-node',
                              ELECTRON_NO_ASAR='1', LAN_BRIDGE_TOKEN='foreign-token',
                              NODE_OPTIONS='--require missing-foreign-module',
                              TEMP=str(base / 'deleted-installer-temp'),
                              TMP=str(base / 'deleted-installer-temp'))
                env = dsh_environment(data, data / 'DSH', parent)
            else:
                env = dict(os.environ, DSH_HOME=str(data / 'DSH'), ELECTRON_RUN_AS_NODE='1')
                for key in list(env):
                    if key.upper() in {'NODE_OPTIONS', 'NODE_PATH', 'ELECTRON_NO_ASAR'}:
                        env.pop(key)
            # These tests never use or read the operator's model credentials.
            env.pop('DEEPSEEK_API_KEY', None)
            log_path = data / 'runtime.log'
            log = log_path.open('ab')
            logs.append(log)
            command = [str(launch_exe), str(cli), 'web', '--host', '127.0.0.1',
                       '--port', '0', '--no-open']
            process = subprocess.Popen(command,
                                       cwd=data, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, creationflags=no_window())
            processes.append(process)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                text = log_path.read_text(encoding='utf-8', errors='replace')
                match = re.search(r'dsh web:\s+(http://127\.0\.0\.1:\d+/\?token=[\w-]+)', text)
                if match:
                    url = match.group(1)
                    jar = http.cookiejar.CookieJar()
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                        urllib.request.HTTPCookieProcessor(jar))
                    with opener.open(url, timeout=5) as response:
                        assert b'<html' in response.read(2000).lower()
                    parts = urllib.parse.urlsplit(url)
                    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, '', '', ''))
                    return {'process': process, 'root': root, 'jar': jar,
                            'opener': opener, 'data': data}
                if process.poll() is not None:
                    # Keep sanitized startup diagnostics before the temporary home is removed.
                    safe_log = re.sub(r'(?i)(token=)[^\s&]+', r'\1[redacted]', text[-4000:])
                    report['startup_failure'] = {'backend': name, 'exit': process.returncode,
                                                 'diagnostic': safe_log}
                    raise RuntimeError(f'{name} backend exited with {process.returncode}')
                time.sleep(0.1)
            raise TimeoutError(f'{name} backend did not become ready')

        def rpc(server, endpoint='agentPresets/list', arguments=None):
            request = urllib.request.Request(server['root'] + '/api/' + endpoint,
                data=json.dumps({'type': 'client-request', 'rpcId': 'coexist-probe',
                                 'method': endpoint,
                                 'payload': {'args': arguments or {}}}).encode(),
                headers={'Content-Type': 'application/json', 'Origin': server['root']})
            with server['opener'].open(request, timeout=5) as response:
                reply = json.load(response)
            assert reply['rpcId'] == 'coexist-probe'
            if not reply['result']['ok']:
                error = reply['result'].get('error', {})
                raise ValueError(f"test RPC {endpoint}: {error.get('code')}: {error.get('details')}")
            return reply['result']['value']

        def cookie(server):
            return '; '.join(f'{c.name}={c.value}' for c in server['jar'])

        def stream(server):
            uri = server['root'].replace('http://', 'ws://', 1) + '/api/remote.mux'
            options = {'proxy': None} if 'proxy' in inspect.signature(connect).parameters else {}
            return connect(uri, origin=server['root'], additional_headers={'Cookie': cookie(server)},
                           open_timeout=5, close_timeout=2, **options)

        try:
            b = launch('standalone', False)
            a = launch('embedded', True)
            assert a['root'] != b['root']
            report['checks']['distinct_dynamic_ports'] = True
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(lambda server=s: [rpc(server) for _ in range(10)])
                           for s in (a, b)]
                for item in pending:
                    item.result()
            report['checks']['twenty_parallel_agent_preset_requests'] = True
            with stream(a) as ws_a, stream(b) as ws_b:
                assert ws_a.ping().wait(3)
                assert ws_b.ping().wait(3)
                rpc(a)
                rpc(b)
                for server in (a, b):
                    created = rpc(server, 'session/create', {'request': {'cwd': str(server['data'])}})
                    accepted = rpc(server, 'session/prompt', {'request': {
                        'sessionId': created['sessionId'], 'requestId': str(uuid4()),
                        'content': [{'type': 'text', 'text': 'Compute 1+99.'}], 'mode': 'steer'}})
                    assert accepted['accepted']
                # No credentials: the model may report authentication is required,
                # but that must never disconnect the local API or the other Host.
                time.sleep(2)
                assert ws_a.ping().wait(3)
                assert ws_b.ping().wait(3)
                rpc(a)
                rpc(b)
            report['checks']['simultaneous_websockets'] = True
            report['checks']['both_accept_prompts_without_backend_disconnect'] = True
            foreign = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(a['root'] + '/api/agentPresets/list',
                data=json.dumps({'type': 'client-request', 'rpcId': 'foreign',
                                 'method': 'agentPresets/list', 'payload': {'args': {}}}).encode(),
                headers={'Content-Type': 'application/json', 'Origin': a['root'], 'Cookie': cookie(b)})
            try:
                foreign.open(request, timeout=3)
                raise AssertionError('another home cookie was admitted')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            report['checks']['foreign_auth_rejected'] = True
            b['process'].terminate()
            b['process'].wait(10)
            for _ in range(10):
                rpc(a)
            report['checks']['embedded_survives_other_backend_exit'] = True
            report['checks']['foreign_electron_node_and_deleted_temp_ignored'] = True
            assert a['process'].poll() is None
            report['passed'] = True
        except Exception as error:
            report['passed'] = False
            # Avoid logging launch tokens, cookies, or credential-bearing URLs.
            report['error'] = type(error).__name__
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True, creationflags=no_window())
                    process.wait(10)
            for log in logs:
                log.close()
            report['seconds'] = round(time.monotonic() - started, 3)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
