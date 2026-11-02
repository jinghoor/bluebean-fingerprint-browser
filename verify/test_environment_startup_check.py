"""Network policy and capability checks; all state lives in disposable profiles."""
import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env_startup_check import network_check, StartupChecks, make_server, SITES, EXIT_SOURCES, EXIT_FALLBACKS, exit_result
from env_management import definition, validate_definition, ManagementError

KEY = 'a' * 32
PROBE_COUNT = len(SITES) + len(EXIT_SOURCES)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bluebean-startup-check-')
        self.row = {'ok': True, 'env_id': 'test-env', 'display_name': '测试环境', 'proxy': 'socks5://127.0.0.1:10101',
                    'expected_exit_ip': '203.0.113.1', 'ip_mode': 'static'}
        self.calls = []
        self.commands = []
        self.checks = StartupChecks(self.temp.name, self.command, self.probe)

    def tearDown(self):
        self.temp.cleanup()

    def command(self, payload):
        self.commands.append(copy.deepcopy(payload))
        if payload.get('key') != KEY or payload.get('env_id') != 'test-env':
            return {'ok': False, 'error': 'expired'}
        if payload['action'] == 'unlock':
            self.row['unlocked'] = True
            if payload['decision'] == 'dynamic':
                self.row['ip_mode'] = 'dynamic'
            if payload['decision'] == 'baseline':
                self.row['expected_exit_ip'] = payload['exit_ip']
        return copy.deepcopy(self.row)

    def probe(self, proxy, url):
        self.calls.append((proxy, url))
        return {'ok': True, 'status': 200, 'latency_ms': 23,
                'body': json.dumps({'ip': '203.0.113.1', 'country': 'US', 'city': 'Test City', 'timezone': 'America/New_York'})}

    def start(self):
        self.checks.start('test-env', KEY)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = self.checks.status('test-env', KEY)
            if row['status'] != 'checking' and not row.get('checking_network') and (not row.get('allowed') or row.get('unlocked')):
                return row
            time.sleep(.01)
        self.fail('startup check timed out')

    def test_matching_exit_unlocks(self):
        result = self.start()
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(result['unlocked'])
        self.assertEqual(len(self.calls), PROBE_COUNT)
        self.assertTrue((Path(self.temp.name) / 'env-check/test-env.json').exists())

    def test_system_mode_still_checks_exit_and_websites(self):
        self.row['proxy'] = 'system'
        result = self.start()
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(result['unlocked'])
        self.assertEqual(len(self.calls), PROBE_COUNT)
        self.assertTrue(all(proxy == 'system' for proxy, _ in self.calls))

    def test_system_probe_uses_windows_settings_transport(self):
        from unittest.mock import patch
        from env_startup_check import probe_url
        with patch('env_startup_check.probe_system_url', return_value={'ok':True}) as probe:
            self.assertTrue(probe_url('system','https://ipinfo.io/json')['ok'])
            probe.assert_called_once_with('https://ipinfo.io/json')

    def test_windows_system_transport_local_http(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows Internet Settings transport')
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from env_startup_check import probe_system_url
        class Local(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body=b'{"ip":"203.0.113.1"}'
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Local)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            result=probe_system_url(f'http://127.0.0.1:{server.server_port}/exit')
            self.assertTrue(result['ok'],result)
            self.assertEqual(json.loads(result['body'])['ip'],'203.0.113.1')
            self.assertGreaterEqual(result['latency_ms'],0)
        finally:
            server.shutdown();server.server_close()

    def test_direct_displays_exit_without_static_ip_comparison(self):
        self.row['proxy'] = 'direct'
        result = self.start()
        self.assertEqual(result['status'], 'direct')
        self.assertTrue(result['unlocked'])
        self.assertEqual(result['exit_ip'],'203.0.113.1')
        self.assertEqual(len(self.calls),PROBE_COUNT)
        self.assertTrue(all(proxy=='direct' for proxy,_ in self.calls))

    def test_mainland_exit_survives_foreign_lookup_failure_and_site_restrictions(self):
        def domestic(proxy,url):
            self.calls.append((proxy,url))
            if url=='https://myip.ipip.net/':
                return {'ok':True,'body':'当前IP：180.101.49.12 来自于：中国 江苏 南京 电信','latency_ms':16}
            if 'baidu.com' in url:return {'ok':True,'reachable':True,'status':200,'latency_ms':12}
            if 'bilibili.com' in url:return {'ok':False,'reachable':True,'status':412,'latency_ms':21}
            return {'ok':False,'reachable':False,'status':0}
        row=network_check({**self.row,'expected_exit_ip':'180.101.49.12'},domestic)
        self.assertEqual(row['exit_ip'],'180.101.49.12');self.assertEqual(row['status'],'matched')
        self.assertTrue(row['allowed']);self.assertEqual(row['ip_source'],'IPIP')
        self.assertTrue(row['probes']['百度']['ok']);self.assertEqual(row['probes']['哔哩哔哩']['status'],412)
        self.assertEqual(set(row['probes']),{name for name,_ in SITES})
        self.assertTrue(all(proxy==self.row['proxy'] for proxy,_ in self.calls))
    def test_plain_domestic_fallback_and_ipv6_preserve_real_address_without_fake_geo(self):
        for address in ('180.101.49.12','2001:4860:4860::8888'):
            calls=[]
            def probe(proxy,url):
                calls.append((proxy,url))
                return {'ok':True,'body':address,'latency_ms':14} if url=='https://ip.3322.net/' else {'ok':False}
            row=network_check({**self.row,'expected_exit_ip':address},probe)
            self.assertEqual(row['exit_ip'],address);self.assertEqual(row['status'],'matched')
            self.assertEqual(row['ip_source'],'3322');self.assertNotIn('country',row['geo'])
            self.assertEqual(len(calls),PROBE_COUNT+len(EXIT_FALLBACKS))
            self.assertTrue(all(proxy==self.row['proxy'] for proxy,_ in calls))
    def test_ip_lookup_failure_is_not_reported_as_network_offline(self):
        def probe(proxy,url):
            return {'ok':True,'reachable':True,'status':200} if 'baidu.com' in url else {'ok':False,'status':0}
        row=network_check(self.row,probe)
        self.assertEqual(row['network_status'],'online');self.assertEqual(row['ip_error'],'ip_lookup_failed')
        self.assertIn('网络已连通',row['error']);self.assertFalse(row['allowed']);self.assertEqual(row['exit_ip'],'')
        direct=network_check({**self.row,'proxy':'direct'},probe)
        self.assertTrue(direct['allowed']);self.assertEqual(direct['status'],'direct')
        self.assertIn('网络已连通',direct['warning'])
    def test_ip_service_restriction_is_not_reported_as_network_offline(self):
        def probe(proxy,url):
            if url == EXIT_SOURCES[0][1]:
                return {'ok':False,'reachable':True,'status':403,'body':'access denied'}
            return {'ok':False,'reachable':False,'status':0}
        row=network_check(self.row,probe)
        self.assertEqual(row['exit_ip'],'')
        self.assertEqual(row['network_status'],'online')
        self.assertEqual(row['ip_error'],'ip_lookup_failed')
        self.assertFalse(row['allowed'])
    def test_cold_site_connections_recover_once_on_the_same_route(self):
        from collections import Counter
        calls=Counter();partials=[]
        def cold(proxy,url):
            self.assertEqual(proxy,self.row['proxy']);calls[url]+=1
            if url in dict(SITES).values():
                if url==dict(SITES)['哔哩哔哩']:
                    return {'ok':False,'reachable':True,'status':412,'latency_ms':21}
                if calls[url]==1:
                    return {'ok':False,'reachable':False,'status':0,'error':'ConnectTimeout','latency_ms':3001}
            return self.probe(proxy,url)
        result=network_check(self.row,cold,partials.append)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['exit_ip'],'203.0.113.1')
        for name,url in SITES:
            if name=='哔哩哔哩':
                self.assertEqual(calls[url],1);self.assertEqual(result['probes'][name]['status'],412)
            else:
                self.assertEqual(calls[url],2);self.assertTrue(result['probes'][name]['recovered'])
                self.assertEqual(result['probes'][name]['first_error'],'ConnectTimeout')
                self.assertEqual(result['probes'][name]['first_latency_ms'],3001)
                self.assertEqual(result['probes'][name]['latency_ms'],23)
        self.assertTrue(any(any(p.get('retrying') for p in r['probes'].values()) for r in partials))
        self.assertFalse(any(p.get('retrying') for p in result['probes'].values()))
        for _,url in EXIT_SOURCES:self.assertEqual(calls[url],1)
        for _,url in EXIT_FALLBACKS:self.assertEqual(calls[url],0)
    def test_persistent_transient_failure_stops_after_one_recheck(self):
        from collections import Counter
        calls=Counter()
        def failed(proxy,url):
            calls[url]+=1
            if url==SITES[0][1]:return {'ok':False,'status':0,'error':'ProxyError','latency_ms':30}
            return self.probe(proxy,url)
        result=network_check(self.row,failed)
        self.assertEqual(calls[SITES[0][1]],2)
        self.assertFalse(result['probes'][SITES[0][0]]['ok'])
        self.assertFalse(result['probes'][SITES[0][0]]['recovered'])
        self.assertEqual(result['probes'][SITES[0][0]]['attempts'],2)
        self.assertTrue(result['allowed'])
        for _,url in SITES[1:]:self.assertEqual(calls[url],1)
    def test_http_restrictions_and_certificate_errors_are_not_retried(self):
        from collections import Counter
        for failure in ({'ok':False,'reachable':True,'status':412},
                        {'ok':False,'reachable':True,'status':403},
                        {'ok':False,'reachable':True,'status':429},
                        {'ok':False,'reachable':False,'status':0,'error':'SSLError'}):
            calls=Counter()
            def restricted(proxy,url):
                calls[url]+=1
                return failure if url==SITES[0][1] else self.probe(proxy,url)
            network_check(self.row,restricted)
            self.assertEqual(calls[SITES[0][1]],1)
    def test_unreachable_route_does_not_double_every_website_request(self):
        calls=[]
        def offline(proxy,url):
            calls.append((proxy,url))
            return {'ok':False,'status':0,'reachable':False,'error':'ConnectTimeout'}
        result=network_check(self.row,offline)
        self.assertEqual(result['ip_error'],'network_unreachable')
        self.assertEqual(len(calls),PROBE_COUNT+len(EXIT_FALLBACKS))
    def test_real_http_first_connection_reset_recovers_without_manual_refresh(self):
        import socket
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from unittest.mock import patch
        from env_startup_check import probe_url
        calls={};lock=threading.Lock()
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                with lock:
                    calls[self.path]=calls.get(self.path,0)+1;attempt=calls[self.path]
                if self.path=='/site' and attempt==1:
                    self.close_connection=True
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                body=json.dumps({'ip':'203.0.113.1'}).encode() if self.path=='/ip' else b'User-agent: *'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Endpoint)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with patch('env_startup_check.SITES',[('Google',base+'/site')]),patch('env_startup_check.EXIT_SOURCES',[('local',base+'/ip')]):
                result=network_check({**self.row,'proxy':'direct'},probe_url)
            self.assertEqual(result['exit_ip'],'203.0.113.1')
            self.assertTrue(result['probes']['Google']['ok'])
            self.assertTrue(result['probes']['Google']['recovered'])
            self.assertEqual(calls,{'/site':2,'/ip':1})
        finally:server.shutdown();server.server_close()
    def test_all_probes_unreachable_never_invent_an_address(self):
        row=network_check(self.row,lambda proxy,url:{'ok':False,'status':0})
        self.assertEqual(row['exit_ip'],'');self.assertEqual(row['ip_error'],'network_unreachable')
        self.assertEqual(row['network_status'],'unreachable');self.assertFalse(row['allowed'])
    def test_exit_parser_rejects_error_pages_and_malformed_payloads(self):
        for body in ('<html>IP 180.101.49.12 blocked</html>','{"ip":"invalid"}','[]','null','429 Too Many Requests'):
            self.assertIsNone(exit_result({'ok':True,'body':body}))
        self.assertIsNone(exit_result({'ok':False,'body':'180.101.49.12'}))
        self.assertEqual(exit_result({'ok':True,'body':'180.101.49.12\n'})['ip'],'180.101.49.12')
    def test_direct_is_unlocked_before_slow_informational_probes_complete(self):
        self.row['proxy']='direct';release=threading.Event()
        self.checks.probe=lambda proxy,url:(release.wait(2),self.probe(proxy,url))[1]
        try:
            self.checks.start('test-env',KEY)
            deadline=time.monotonic()+1
            while time.monotonic()<deadline and not self.row.get('unlocked'):time.sleep(.01)
            row=self.checks.status('test-env',KEY)
            self.assertTrue(row['unlocked']);self.assertTrue(row['checking_network'])
        finally:release.set()
        row=self.start();self.assertEqual(row['exit_ip'],'203.0.113.1')
    def test_valid_exit_is_published_before_slow_website_finishes(self):
        release=threading.Event();partials=[]
        def probe(proxy,url):
            if 'baidu.com' in url:release.wait(2)
            return self.probe(proxy,url)
        worker=threading.Thread(target=lambda:network_check(self.row,probe,partials.append));worker.start()
        try:
            deadline=time.monotonic()+1
            while time.monotonic()<deadline and not any(p.get('exit_ip') for p in partials):time.sleep(.01)
            self.assertTrue(any(p.get('exit_ip')=='203.0.113.1' for p in partials));self.assertTrue(worker.is_alive())
        finally:release.set();worker.join()
    def test_direct_transport_does_not_use_proxy_environment(self):
        from unittest.mock import patch,MagicMock
        from env_startup_check import probe_url
        session=MagicMock();session.__enter__.return_value=session
        session.get.return_value.status_code=200;session.get.return_value.text='180.101.49.12'
        with patch('requests.Session',return_value=session):
            self.assertTrue(probe_url('direct','https://ip.3322.net/')['ok'])
        self.assertFalse(session.trust_env);self.assertEqual(session.proxies,{})

    def test_changed_runtime_proxy_requires_restart_without_probing(self):
        self.row['restart_required'] = True
        result = self.start()
        self.assertEqual(result['status'], 'error')
        self.assertIn('重启', result['error'])
        self.assertEqual(self.calls, [])

    def test_real_http_proxy_transport_and_latency(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from env_startup_check import probe_url
        requested = []
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                requested.append(self.path)
                body = b'{"ip":"203.0.113.1"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers();self.wfile.write(body)
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True);thread.start()
        try:
            result = probe_url(f'http://127.0.0.1:{proxy.server_port}', 'http://destination.invalid/exit')
            self.assertTrue(result['ok'])
            self.assertTrue(result['reachable'])
            self.assertIsInstance(result['latency_ms'], int)
            self.assertEqual(requested, ['http://destination.invalid/exit'])
        finally:
            proxy.shutdown();proxy.server_close();thread.join()

    def test_mismatch_remains_locked(self):
        self.row['expected_exit_ip'] = '203.0.113.2'
        result = self.start()
        self.assertEqual(result['status'], 'mismatch')
        self.assertFalse(result.get('unlocked', False))
        self.assertFalse(any(c['action'] == 'unlock' for c in self.commands))

    def test_continue_once_keeps_saved_ip(self):
        self.row['expected_exit_ip'] = '203.0.113.2'
        result = self.start()
        self.checks.decide('test-env', KEY, 'continue_once', result['receipt'])
        self.assertEqual(self.row['expected_exit_ip'], '203.0.113.2')
        self.assertEqual(self.row['ip_mode'], 'static')

    def test_dynamic_persists_without_overwriting_original_ip(self):
        self.row['expected_exit_ip'] = '203.0.113.2'
        result = self.start()
        self.checks.decide('test-env', KEY, 'dynamic', result['receipt'])
        self.assertEqual(self.row['ip_mode'], 'dynamic')
        self.assertEqual(self.row['expected_exit_ip'], '203.0.113.2')
        self.assertTrue(self.checks.status('test-env', KEY)['unlocked'])

    def test_dynamic_exit_changes_do_not_warn(self):
        self.row.update(ip_mode='dynamic', expected_exit_ip='203.0.113.2')
        result = self.start()
        self.assertEqual(result['status'], 'dynamic')
        self.assertTrue(result['unlocked'])
        self.assertEqual(len(self.calls), PROBE_COUNT)

    def test_no_baseline_requires_explicit_save(self):
        self.row['expected_exit_ip'] = ''
        result = self.start()
        self.assertEqual(result['status'], 'unrecorded')
        with self.assertRaises(ValueError):
            self.checks.decide('test-env', KEY, 'pass', result['receipt'])
        self.checks.decide('test-env', KEY, 'baseline', result['receipt'])
        self.assertEqual(self.row['expected_exit_ip'], '203.0.113.1')

    def test_unknown_exit_never_passes(self):
        self.checks.probe = lambda proxy, url: {'ok': False, 'status': 0}
        result = self.start()
        self.assertEqual(result['status'], 'error')
        with self.assertRaises(ValueError):
            self.checks.decide('test-env', KEY, 'dynamic', result['receipt'])

    def test_site_failures_do_not_misreport_proxy_failure(self):
        result = network_check(self.row, lambda p, u: self.probe(p, u) if 'ipinfo' in u else {'ok': False, 'status': 403, 'latency_ms': 19})
        self.assertEqual(result['status'], 'matched')
        self.assertTrue(result['allowed'])
        self.assertTrue(all(not p['ok'] for p in result['probes'].values()))

    def test_invalid_ip_fallback(self):
        result = network_check(self.row, lambda p, u: {'ok': True, 'body': json.dumps({'ip': 'invalid'})} if 'ipinfo' in u else self.probe(p, u))
        self.assertEqual(result['status'], 'matched')

    def test_canonical_ipv6_comparison(self):
        self.row['expected_exit_ip'] = '2001:0db8:0000::1'
        result = network_check(self.row, lambda p, u: {'ok': True, 'body': '{"ip":"2001:db8::1"}', 'status': 200})
        self.assertEqual(result['status'], 'matched')

    def test_stale_configuration_cannot_unlock(self):
        self.row['expected_exit_ip'] = '203.0.113.2'
        result = self.start()
        self.row['proxy'] = 'http://127.0.0.1:8081'
        with self.assertRaises(ValueError):
            self.checks.decide('test-env', KEY, 'continue_once', result['receipt'])
        self.assertEqual(self.checks.status('test-env', KEY)['status'], 'stale')

    def test_wrong_capability_receipt_and_environment_rejected(self):
        with self.assertRaises(ValueError):
            self.checks.start('test-env', 'b' * 32)
        with self.assertRaises(ValueError):
            self.checks.start('../escape', KEY)
        self.row['expected_exit_ip'] = '203.0.113.2'
        self.start()
        with self.assertRaises(ValueError):
            self.checks.decide('test-env', KEY, 'continue_once', 'forged')
        with self.assertRaises(ValueError):
            self.checks.decide('test-env', KEY, 'baseline', self.checks.status('test-env', KEY)['receipt'])

    def test_close_does_not_require_successful_probe(self):
        self.checks.decide('test-env', KEY, 'close')
        self.assertEqual(self.commands[-1]['action'], 'close')
        self.assertEqual(self.calls, [])

    def test_concurrent_requests_share_one_check(self):
        released = threading.Event()
        self.checks.probe = lambda p, u: (released.wait(2), self.probe(p, u))[1]
        first = self.checks.start('test-env', KEY)
        second = self.checks.start('test-env', KEY)
        self.assertEqual(first['receipt'], second['receipt'])
        released.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.checks.status('test-env', KEY).get('unlocked'):
            time.sleep(.01)
        self.assertEqual(len(self.calls), PROBE_COUNT)

    def test_http_rejects_cross_origin_and_unscoped_page(self):
        server = make_server(self.temp.name, command=self.command, probe=self.probe)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('POST', '/decision', '{}', {'Origin': 'https://untrusted.example', 'Content-Type': 'application/json'})
            response = connection.getresponse();response.read()
            self.assertEqual(response.status, 403)
            connection.request('GET', '/?env=test-env&key=' + 'b' * 32)
            response = connection.getresponse();response.read()
            self.assertEqual(response.status, 409)
            connection.request('GET', '/?env=test-env&key=' + KEY)
            response = connection.getresponse();body = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b'frame-ancestors', response.getheader('Content-Security-Policy').encode())
            self.assertIn(b'account-list', body)
            self.assertNotIn('打开环境主页'.encode(), body)
            connection.request('GET', '/client.js')
            response = connection.getresponse();script = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn('接受本次风险'.encode(), script)
            self.assertIn('text/javascript', response.getheader('Content-Type'))
            connection.close()
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_ip_mode_validation_and_legacy_default(self):
        self.assertEqual(definition({})['ip_mode'], 'static')
        row = {'env_id': 'test-env', 'display_name': 'Test', 'proxy': 'direct', 'ip_mode': 'dynamic'}
        self.assertEqual(validate_definition(row)['ip_mode'], 'dynamic')
        with self.assertRaises(ManagementError):
            validate_definition({**row, 'ip_mode': 'unknown'})


if __name__ == '__main__':
    unittest.main()
