"""Real loopback auth relay checks, with disposable profiles and fake credentials."""
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
import time
import unittest

from env_management import ManagementError, definition
from environment_proxy import ProxySettings
from environment_proxy import _CHILDREN
from _runtime_state import process_identity
from verify.test_env_management import fixture


class ProxyEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bluebean-four-field-proxy-')
        self.root = Path(self.tmp.name)
        self.bridge = fixture(self.tmp.name, probe=lambda _: {'ok': True, 'exit_ip': '203.0.113.10', 'latency_ms': 12})
        self.manager = self.bridge.env_management
        self.store = self.manager.proxy_settings
        self.requests = []
        received = self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def authorized(self):
                received.append((self.command, self.path, self.headers.get('Proxy-Authorization')))
                if self.headers.get('Proxy-Authorization') != 'Basic ' + base64.b64encode(b'fixture-user:fixture-secret').decode():
                    self.send_response(407); self.send_header('Content-Length', '0'); self.end_headers()
                    return False
                return True
            def do_GET(self):
                if not self.authorized(): return
                body = b'authenticated-page'
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_CONNECT(self):
                if not self.authorized(): return
                self.send_response(200); self.end_headers()
                self.wfile.write(b'tunnel-ready'); self.wfile.flush()
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.upstream.daemon_threads = True
        self.worker = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.worker.start()
        self.fields = {'host': '127.0.0.1', 'port': str(self.upstream.server_port), 'username': 'fixture-user', 'password': 'fixture-secret'}

    def tearDown(self):
        for path in self.store.directory.glob('*.json'):
            state = json.loads(path.read_text())
            identity = process_identity(state['pid'])
            if identity.get('alive') and identity.get('started') == state.get('started'):
                os.kill(state['pid'], signal.SIGTERM)
            child = _CHILDREN.pop(state['pid'], None)
            if child: child.wait(timeout=5)
        self.upstream.shutdown(); self.upstream.server_close(); self.worker.join(timeout=2)
        self.tmp.cleanup()

    def save(self, **overrides):
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == 'fb-008')
        return self.manager.save({**definition(row), 'revision': row['revision'], 'proxy_fields': self.fields, **overrides})

    @staticmethod
    def get(proxy):
        from urllib.parse import urlsplit
        endpoint = urlsplit(proxy)
        conn = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=4)
        try:
            conn.request('GET', 'http://fixture.invalid/account', headers={'Proxy-Authorization': 'must-be-replaced'})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_four_fields_authenticate_http_and_https_tunnel(self):
        saved = self.save()
        self.assertEqual(self.get(saved['proxy']), (200, b'authenticated-page'))
        from urllib.parse import urlsplit
        with socket.create_connection(('127.0.0.1', urlsplit(saved['proxy']).port), timeout=4) as sock:
            sock.sendall(b'CONNECT fixture.invalid:443 HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n')
            reply = b''
            while b'tunnel-ready' not in reply:
                data = sock.recv(4096)
                if not data: break
                reply += data
            self.assertIn(b'200 Connection Established', reply)
            self.assertIn(b'tunnel-ready', reply)
        self.assertEqual([r[0] for r in self.requests], ['GET', 'CONNECT'])

    def test_password_encrypted_lists_and_backups_do_not_expose_it(self):
        result = self.save()
        for path in [self.store.path, self.bridge.backend.path, self.manager.path]:
            self.assertNotIn('fixture-secret', path.read_text(encoding='utf-8'))
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == 'fb-008')
        self.assertEqual(row['proxy_fields']['password'], '')
        self.assertTrue(row['proxy_fields']['has_password'])
        self.assertEqual(row['proxy_fields']['port'], str(self.upstream.server_port))
        self.assertNotIn('fixture-secret', json.dumps(self.manager.backup({})))
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_unchanged_saved_password_and_test_receipt_reuse_same_relay(self):
        first = self.save()
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == 'fb-008')
        checked = self.manager.test_proxy({'proxy': row['proxy'], 'proxy_fields': row['proxy_fields']})
        self.assertEqual(checked['proxy'], first['proxy'])
        second = self.save(proxy_fields=row['proxy_fields'], notes='only this changes')
        self.assertEqual(second['proxy'], first['proxy'])
        self.manager.attach_proxy_test({'env_id': 'fb-008', 'receipt': checked['receipt']})
        self.assertEqual(len(self.store.read()), 1)
        self.assertEqual(self.get(second['proxy'])[0], 200)

    def test_wrong_password_cannot_fall_back_to_direct(self):
        bad = self.save(proxy_fields={**self.fields, 'password': 'wrong'})
        self.assertEqual(self.get(bad['proxy'])[0], 407)

    def test_password_change_uses_a_new_relay_without_breaking_previous_one(self):
        old = self.save()
        changed = self.save(proxy_fields={**self.fields, 'password': 'wrong'})
        self.assertNotEqual(old['proxy'], changed['proxy'])
        self.assertEqual(self.get(old['proxy'])[0], 200)
        self.assertEqual(self.get(changed['proxy'])[0], 407)

    def test_validation_never_changes_environment_or_starts_relay(self):
        before = self.bridge.backend.env_state()
        invalid = [{'host': '192.0.2.1'}, {'port': '8080'}, {**self.fields, 'port': '65536'},
                   {**self.fields, 'host': 'bad/path'}, {**self.fields, 'password': ''},
                   {**self.fields, 'username': ''}, {**self.fields, 'username': 'bad\r\nuser'}]
        for fields in invalid:
            with self.subTest(fields=list(fields)):
                with self.assertRaises(ManagementError): self.save(proxy_fields=fields)
        self.assertEqual(before, self.bridge.backend.env_state())
        self.assertFalse(self.store.path.exists())

    def test_no_default_ip_or_port_and_all_empty_means_direct(self):
        result = self.save(proxy_fields={'host': '', 'port': '', 'username': '', 'password': ''})
        self.assertEqual(result['proxy'], 'direct')
        self.assertFalse(self.store.path.exists())
        settings = self.store.normalize({'host': '2001:db8::1', 'port': '1234'})
        self.assertEqual(settings['proxy'], 'http://[2001:db8::1]:1234')

    def test_existing_proxy_scheme_and_hidden_ip_policy_are_preserved(self):
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == 'fb-008')
        self.manager.save({**definition(row), 'revision': row['revision'], 'proxy': 'socks5://127.0.0.1:9988',
                           'expected_exit_ip': '203.0.113.9', 'ip_mode': 'dynamic'})
        result = self.save(proxy_fields={'host': '192.0.2.10', 'port': '1080', 'username': '', 'password': ''})
        self.assertEqual(result['proxy'], 'socks5://192.0.2.10:1080')
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == 'fb-008')
        self.assertEqual(row['expected_exit_ip'], '203.0.113.9')
        self.assertEqual(row['ip_mode'], 'dynamic')
        self.assertEqual(self.store.normalize({'host': '', 'port': ''}, 'system')['proxy'], 'system')

    def test_relay_recovers_same_address_after_process_exit(self):
        saved = self.save()
        path = next(self.store.directory.glob('*.json'))
        previous = json.loads(path.read_text())
        os.kill(previous['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and process_identity(previous['pid']).get('alive'):
            time.sleep(.02)
        reloaded = ProxySettings(self.root)
        self.assertEqual(reloaded.restore([saved['proxy']]), [saved['proxy']])
        self.assertNotEqual(json.loads(path.read_text())['pid'], previous['pid'])
        self.assertEqual(self.get(saved['proxy'])[0], 200)

    def test_agent_can_save_and_edit_four_fields_without_returning_password(self):
        import secrets
        from environment_agent import EnvironmentAgentService
        from session_browser_scope import SessionBrowserScopes
        self.bridge.permissions = {'kill_switch': False}
        self.bridge.execution_lock = threading.RLock()
        self.bridge.browser_scopes = SessionBrowserScopes(self.root / 'scopes.json')
        service = EnvironmentAgentService(self.bridge, 'fixture-token')
        def execute(arguments):
            key = secrets.token_hex(32)
            prepared = service.dispatch({'phase': 'prepare', 'tool': 'environment_save', 'arguments': arguments,
                                         'session_id': 'fixture', 'request_id': key})
            self.assertNotIn('fixture-secret', json.dumps(prepared))
            result = service.dispatch({'phase': 'execute', 'session_id': 'fixture', 'request_id': key})
            self.assertTrue(result['ok'], result)
            self.assertNotIn('fixture-secret', json.dumps(result))
            return result['result']
        created = execute({'display_name': '代理接口验收', 'proxy_fields': self.fields})
        env = created['env_id']
        row = next(r for r in self.manager.list()['envs'] if r['env_id'] == env)
        public = {k: row['proxy_fields'][k] for k in ('host', 'port', 'username', 'password')}
        updated = execute({'env_id': env, 'revision': row['revision'], 'proxy': row['proxy'], 'proxy_fields': public, 'notes': 'preserve auth'})
        self.assertEqual(created['proxy'], updated['proxy'])
        self.assertEqual(self.get(updated['proxy'])[0], 200)


if __name__ == '__main__': unittest.main()
