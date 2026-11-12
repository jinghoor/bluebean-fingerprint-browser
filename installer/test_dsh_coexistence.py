"""Regression checks for private DSH startup, without user credentials or UI."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _runtime_state import DSH_ENVIRONMENT_VERSION, dsh_environment
import _dsh_web
from dsh_client import DshRuntime


class EnvironmentTests(unittest.TestCase):
    def test_foreign_carrier_is_removed_without_changing_the_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            parent = {'PATH': 'fixture-path', 'DSH_HOME': 'foreign-home',
                      'DSH_DESKTOP_NODE_EXECUTABLE': 'foreign-node',
                      'dsh_desktop_host_protocol': 'foreign-ipc',
                      'ELECTRON_RUN_AS_NODE': '0', 'Electron_No_Asar': '1',
                      'NODE_OPTIONS': '--require foreign-hook', 'node_path': 'foreign-modules',
                      'DSH_WEB_URL': 'http://127.0.0.1:1234',
                      'LAN_BRIDGE_TOKEN': 'foreign-token', 'LAN_BRIDGE_URL': 'foreign-bridge',
                      'DSH_PERMISSION_MODE': 'workspace-write',
                      'HTTPS_PROXY': 'http://127.0.0.1:7890',
                      'DEEPSEEK_API_KEY': 'fixture-key', 'TEMP': 'deleted-setup-temp'}
            before = dict(parent)
            env = dsh_environment(data, data / 'DSH', parent)
            self.assertEqual(parent, before)
            self.assertEqual(env['DSH_HOME'], str((data / 'DSH').resolve()))
            self.assertEqual(env['ELECTRON_RUN_AS_NODE'], '1')
            for key in ('DSH_DESKTOP_NODE_EXECUTABLE', 'dsh_desktop_host_protocol',
                        'Electron_No_Asar', 'NODE_OPTIONS', 'node_path',
                        'DSH_WEB_URL', 'LAN_BRIDGE_TOKEN', 'LAN_BRIDGE_URL'):
                self.assertNotIn(key, env)
            for key in ('DSH_PERMISSION_MODE', 'HTTPS_PROXY', 'DEEPSEEK_API_KEY', 'PATH'):
                self.assertEqual(env[key], parent[key])
            self.assertTrue(Path(env['TEMP']).is_dir())
            self.assertEqual(env['TEMP'], env['TMP'])
            self.assertEqual(env['TEMP'], env['TMPDIR'])

    def test_two_homes_have_distinct_home_and_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            a = dsh_environment(base / 'A', base / 'A/DSH', {})
            b = dsh_environment(base / 'B', base / 'B/DSH', {})
            self.assertNotEqual(a['DSH_HOME'], b['DSH_HOME'])
            self.assertNotEqual(a['TEMP'], b['TEMP'])

    def test_sdk_uses_private_environment_and_home_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / '.lan-bridge.json').write_text(json.dumps({'port': 12346, 'token': 'own-token'}))
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    'DSH_DESKTOP_NODE_EXECUTABLE': 'foreign', 'LAN_BRIDGE_TOKEN': 'foreign',
                    'LAN_BRIDGE_URL': 'http://foreign', 'DEEPSEEK_API_KEY': 'fixture-key'}))
                stack.enter_context(patch('dsh_client.DATA_ROOT', data))
                popen = stack.enter_context(patch('dsh_client.subprocess.Popen'))
                stack.enter_context(patch('dsh_client.threading.Thread'))
                DshRuntime(data / 'sdk')
                env = popen.call_args.kwargs['env']
            self.assertEqual(env['LAN_BRIDGE_TOKEN'], 'own-token')
            self.assertEqual(env['LAN_BRIDGE_URL'], 'http://127.0.0.1:12346')
            self.assertEqual(env['DSH_HOME'], str((data / 'sdk').resolve()))
            self.assertNotIn('DSH_DESKTOP_NODE_EXECUTABLE', env)


class ProbeTests(unittest.TestCase):
    def check_probe(self, result):
        shell = Mock()
        shell.status = 200
        shell.read.return_value = b'<html>fixture</html>'
        shell.__enter__ = Mock(return_value=shell)
        shell.__exit__ = Mock(return_value=False)
        api = Mock()
        api.read.return_value = json.dumps({'type': 'server-response',
                                          'rpcId': 'bluebean-health', 'result': result}).encode()
        api.__enter__ = Mock(return_value=api)
        api.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.side_effect = [shell, api]
        with patch('_dsh_web.urllib.request.build_opener', return_value=opener):
            answer = _dsh_web.probe('http://127.0.0.1:12345/?token=fixture')
        request = opener.open.call_args_list[1].args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:12345/api/agentPresets/list')
        self.assertEqual(json.loads(request.data)['payload'], {'args': {}})
        return answer

    def test_page_without_agent_api_is_not_healthy(self):
        self.assertFalse(self.check_probe({'ok': False, 'error': {'code': 'gateway/unavailable'}})[0])

    def test_shell_and_agent_api_are_healthy(self):
        self.assertTrue(self.check_probe({'ok': True, 'value': {}})[0])


class LifecycleTests(unittest.TestCase):
    def test_web_launch_uses_its_own_environment_and_loopback(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            data = Path(directory)
            log = data / 'logs/dsh-web.log'
            for key, value in {'DATA_ROOT': data, 'HOME': data / 'DSH',
                               'LOG': log, 'STATE': data / '.dsh-web.json'}.items():
                stack.enter_context(patch.object(_dsh_web, key, value))
            stack.enter_context(patch.dict(os.environ, {
                'DSH_DESKTOP_NODE_EXECUTABLE': 'foreign', 'NODE_OPTIONS': '--require missing',
                'LAN_BRIDGE_TOKEN': 'foreign', 'TEMP': 'deleted-setup-temp'}))
            for target in ('prepare_profile', 'sync_seat_plugin'):
                stack.enter_context(patch.object(_dsh_web, target))
            stack.enter_context(patch.object(_dsh_web, 'api_key', return_value=''))
            stack.enter_context(patch.object(_dsh_web, 'probe', return_value=(True, 'fixture API OK')))
            stack.enter_context(patch.object(_dsh_web, 'plugin_signature', return_value='plugins'))
            stack.enter_context(patch.object(_dsh_web, 'bridge_signature', return_value='bridge'))
            stack.enter_context(patch.object(_dsh_web, 'process_identity', return_value={'started': 1234}))
            def launched(*args, **kwargs):
                kwargs['stdout'].write(b'dsh web: http://127.0.0.1:12345/?token=fixture\n')
                kwargs['stdout'].flush()
                return Mock(pid=123, poll=Mock(return_value=None))
            popen = stack.enter_context(patch.object(_dsh_web.subprocess, 'Popen', side_effect=launched))
            result = _dsh_web.start(timeout=1)
            env = popen.call_args.kwargs['env']
            command = popen.call_args.args[0]
            self.assertTrue(result['ok'])
            self.assertEqual(result['runtime_environment_version'], DSH_ENVIRONMENT_VERSION)
            self.assertEqual(env['DSH_HOME'], str((data / 'DSH').resolve()))
            self.assertNotIn('NODE_OPTIONS', env)
            self.assertNotIn('LAN_BRIDGE_TOKEN', env)
            self.assertEqual(command[-5:], ['--host', '127.0.0.1', '--port', '0', '--no-open'])

    def test_healthy_current_service_is_reused(self):
        state = {'pid': 123, 'url': 'http://127.0.0.1:12345/?token=fixture',
                 'plugin_signature': 'plugins', 'bridge_signature': 'bridge',
                 'runtime_environment_version': DSH_ENVIRONMENT_VERSION}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(_dsh_web, 'DATA_ROOT', Path(directory)))
            stack.enter_context(patch.object(_dsh_web, 'load_state', return_value=state))
            stack.enter_context(patch.object(_dsh_web, 'owns_state', return_value=True))
            stack.enter_context(patch.object(_dsh_web, 'plugin_signature', return_value='plugins'))
            stack.enter_context(patch.object(_dsh_web, 'bridge_signature', return_value='bridge'))
            stack.enter_context(patch.object(_dsh_web, 'probe', return_value=(True, 'API OK')))
            start = stack.enter_context(patch.object(_dsh_web, 'start'))
            stop = stack.enter_context(patch.object(_dsh_web, 'stop'))
            self.assertTrue(_dsh_web.ensure()['reused'])
            start.assert_not_called()
            stop.assert_not_called()

    def test_foreign_process_is_never_stopped(self):
        with patch.object(_dsh_web, 'load_state', return_value={'pid': 123}), \
                patch.object(_dsh_web, 'owns_state', return_value=False), \
                patch.object(_dsh_web.subprocess, 'run') as run:
            self.assertTrue(_dsh_web.stop()['foreign_state_ignored'])
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
