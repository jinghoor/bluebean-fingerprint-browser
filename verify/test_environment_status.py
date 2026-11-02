"""Isolated runtime truth, status conflicts and real task lifecycle regression tests."""
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from environment_status import AgentActivity, ProxyHealth, aggregate_status, project_status
from verify.test_env_management import fixture
from verify.test_session_browser_scope import ScopeTests
from env_management import EnvironmentManagement


class StatusTests(unittest.TestCase):
    def test_proxy_probe_uses_proxy_dns_and_never_inherits_machine_proxy(self):
        with patch('requests.Session') as constructor:
            session = constructor.return_value.__enter__.return_value
            response = session.get.return_value.__enter__.return_value
            response.json.return_value = {'ip':'203.0.113.7'}
            self.assertTrue(EnvironmentManagement.probe_proxy_health('socks5://127.0.0.1:1234')['ok'])
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get.call_args.kwargs['proxies']['https'], 'socks5h://127.0.0.1:1234')

    def test_priority_matrix(self):
        for known in (False, True):
            for running in (False, True):
                for network in ('unchecked', 'success', 'failed', 'mismatch'):
                    for agent in ('idle', 'running', 'complete', 'failed'):
                        row = project_status({'tabs': [{}] if running else []}, known, network, {'state': agent})
                        expected = ('agent-running' if agent == 'running' else 'unknown' if not known
                                    else 'stopped' if not running else 'network-error' if network in ('failed', 'mismatch')
                                    else 'agent-complete' if agent == 'complete' else 'running')
                        self.assertEqual(row['state'], expected, (known, running, network, agent))

    def test_concurrent_activity_failure_cleanup_and_secret_redaction(self):
        tracker = AgentActivity()
        first, second = tracker.begin('a'), tracker.begin('a')
        tracker.finish(first, True)
        self.assertEqual(tracker.snapshot()['a']['state'], 'running')
        tracker.finish(second, False, 'https://user:secret@host ERR_PROXY_CONNECTION_FAILED')
        self.assertEqual(tracker.snapshot()['a']['network_error'], 'ERR_PROXY_CONNECTION_FAILED')
        self.assertNotIn('secret', str(tracker.snapshot()))
        self.assertEqual(tracker.snapshot(set()), {})
        self.assertEqual(tracker.snapshot({'a'}), {}, 'reopening cannot restore an old completion')

    def test_overlapping_pages_preserve_failure_until_the_whole_wave_finishes(self):
        tracker = AgentActivity()
        first, second = tracker.begin('account-a'), tracker.begin('account-a')
        tracker.finish(first, False, 'ERR_PROXY_CONNECTION_FAILED')
        third = tracker.begin('account-a')
        row = tracker.snapshot()['account-a']
        self.assertEqual((row['active_count'], row['failed_count']), (2, 1))
        self.assertEqual(row['network_error'], 'ERR_PROXY_CONNECTION_FAILED')
        tracker.finish(second, True)
        self.assertEqual(tracker.snapshot()['account-a']['state'], 'running')
        tracker.finish(third, True)
        row = tracker.snapshot()['account-a']
        self.assertEqual((row['state'], row['completed_count'], row['failed_count']), ('failed', 2, 1))
        tracker.finish(first, True)  # Duplicate / late callbacks are harmless.
        self.assertEqual(tracker.snapshot()['account-a']['failed_count'], 1)
        retry = tracker.begin('account-a')
        row = tracker.snapshot()['account-a']
        self.assertEqual((row['active_count'], row['failed_count']), (1, 0))
        self.assertNotIn('network_error', row)
        tracker.finish(first, False)  # An old callback cannot affect the retry.
        tracker.finish(retry, True)
        self.assertEqual(tracker.snapshot()['account-a']['state'], 'complete')

    def test_many_threads_account_for_every_operation_and_isolate_environments(self):
        tracker = AgentActivity()
        tokens = [tracker.begin('account-a' if i < 20 else 'account-b') for i in range(40)]
        untouched = tracker.begin('other-environment')
        gate = threading.Barrier(40)
        def finish(i):
            gate.wait(timeout=5)
            tracker.finish(tokens[i], i % 4 != 0)
        threads = [threading.Thread(target=finish, args=(i,)) for i in range(40)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(6)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        for env in ('account-a', 'account-b'):
            row = tracker.snapshot()[env]
            self.assertEqual((row['state'], row['completed_count'], row['failed_count']), ('failed', 15, 5))
        self.assertEqual(tracker.snapshot()['other-environment']['active_count'], 1)
        tracker.finish(untouched, True)

    def test_parent_sums_accounts_and_never_hides_an_earlier_failure(self):
        tracker = AgentActivity()
        a1, a2, b1 = tracker.begin('account-a'), tracker.begin('account-a'), tracker.begin('account-b')
        def parent():
            states = tracker.snapshot()
            return aggregate_status([project_status({'tabs':[{}]}, True, 'success', row) for row in states.values()], True)
        row = parent()
        self.assertEqual((row['state'], row['agent_active_count']), ('agent-running', 3))
        tracker.finish(a1, False)
        tracker.finish(b1, True)
        row = parent()
        self.assertEqual(row['state'], 'agent-running')
        self.assertEqual((row['agent_active_count'], row['agent_failed_count'], row['agent_completed_count']), (1, 1, 1))
        self.assertTrue(row['agent_warning'])
        tracker.finish(a2, True)
        row = parent()
        self.assertNotEqual(row['state'], 'agent-complete')
        self.assertEqual((row['agent_active_count'], row['agent_failed_count'], row['agent_completed_count']), (0, 1, 2))
        retry = tracker.begin('account-a')
        tracker.finish(retry, True)
        self.assertEqual(parent()['state'], 'agent-complete')

    def test_older_network_failure_is_not_made_fresh_by_a_sibling_success(self):
        tracker = AgentActivity()
        first, second = tracker.begin('a'), tracker.begin('a')
        with patch('environment_status.time.time', return_value=100):
            tracker.finish(first, False, 'ERR_PROXY_CONNECTION_FAILED')
        with patch('environment_status.time.time', return_value=200):
            tracker.finish(second, True)
        row = tracker.snapshot()['a']
        self.assertEqual(row['network_error_at'], 100)
        self.assertEqual(row['updated_at'], 200)
        self.assertEqual(row['state'], 'failed')

    def test_live_refresh_does_not_discard_partial_failure_during_startup(self):
        tracker = AgentActivity()
        first, second = tracker.begin('a'), tracker.begin('a')
        tracker.finish(first, False)
        self.assertEqual(tracker.snapshot(set())['a']['failed_count'], 1)
        tracker.finish(second, True)
        self.assertEqual(tracker.snapshot(set()), {})

    def test_aggregate_ignores_stopped_network_and_keeps_failure(self):
        member = lambda live, net, agent, at=1: project_status({'tabs': [{}] if live else []}, True, net, {'state': agent, 'updated_at': at})
        self.assertEqual(aggregate_status([member(False, 'failed', 'complete'), member(True, 'unchecked', 'idle')], True)['state'], 'running')
        row = aggregate_status([member(True, 'failed', 'complete'), member(True, 'success', 'running')], True)
        self.assertEqual(row['state'], 'agent-running')
        self.assertTrue(row['network_warning'])
        row = aggregate_status([member(True, 'success', 'complete'), member(True, 'success', 'failed', 2)], True)
        self.assertTrue(row['agent_warning'])

    def test_proxy_is_async_bounded_deduplicated_and_normalizes_ipv6(self):
        release = threading.Event()
        started = []
        def probe(proxy):
            started.append(proxy)
            release.wait(2)
            return {'ok': True, 'exit_ip': '2001:db8::1'}
        health = ProxyHealth(probe)
        keys = [('p'+str(i), '2001:0db8:0:0:0:0:0:1') for i in range(4)]
        start = time.monotonic()
        health.read(keys+keys)
        self.assertLess(time.monotonic()-start, .3)
        self.assertEqual(len(health.pending), 2)
        health.read(keys)
        self.assertEqual(len(health.pending), 2)
        release.set()
        deadline = time.monotonic()+3
        while len(health.read(keys)) < 4 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(started), 4)
        self.assertTrue(all(x['status']=='success' for x in health.read(keys).values()))

    def test_cached_read_does_not_start_proxy_probes(self):
        calls=[]
        health=ProxyHealth(lambda proxy: calls.append(proxy) or {'ok':True,'exit_ip':'203.0.113.1'})
        key=('http://proxy.test:8080','')
        self.assertEqual(health.read([key],start=False),{})
        time.sleep(.03)
        self.assertEqual(calls,[])
        self.assertEqual(health.read([key]),{})
        deadline=time.monotonic()+1
        while not calls and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(calls,['http://proxy.test:8080'])

    def test_runtime_status_uses_runtime_ids_for_multiple_background_environments(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = fixture(root)
            manager = bridge.env_management
            bridge.agent_activity = AgentActivity()
            rows = bridge.backend.env_state()
            rows['envs'][0]['tabs'] = [{'url': 'https://example.test'}]
            rows['envs'][1]['tabs'] = [{'url': 'https://background.test'}]
            rows['envs'][2]['tabs'] = []
            with patch.object(bridge.backend, 'env_state', return_value=rows), \
                    patch.object(bridge.backend, 'runtime_state', return_value={'ok': True, 'active': rows['active'], 'running_envs': ['fb-008', 'fb-012']}), \
                    patch.object(bridge.backend, 'live_state', side_effect=AssertionError('status must not enumerate all tabs')):
                status = manager.runtime_status()
            self.assertEqual(status['states']['fb-008']['state'], 'running')
            self.assertEqual(status['states']['fb-012']['state'], 'running')
            self.assertEqual(status['states']['fb-020']['state'], 'stopped')
            child = {**rows['envs'][0], 'env_id':'acct-child'}
            rows['envs'].append(child)
            rows['active'] = 'acct-child'
            token = bridge.agent_activity.begin('acct-child')
            bridge.agent_activity.finish(token, True)
            account_data = {'fb-020': {'accounts':[{'session_env_id':'acct-child'}]}}
            with patch.object(bridge.backend, 'env_state', return_value=rows), \
                    patch.object(bridge.backend, 'runtime_state', return_value={'ok': True, 'active': 'acct-child', 'running_envs': ['fb-008', 'acct-child']}), \
                    patch.object(bridge.backend, 'live_state', side_effect=AssertionError('status must not enumerate all tabs')), \
                    patch.object(manager, '_workspace', return_value=SimpleNamespace(metadata=lambda: account_data)):
                status = manager.runtime_status()
                self.assertEqual(status['active'], 'fb-020')
                self.assertEqual(status['active_session'], 'acct-child')
                self.assertEqual(status['states']['fb-020']['state'], 'agent-complete')
                self.assertEqual(status['states']['acct-child']['state'], 'agent-complete')
            with patch.object(bridge.backend, 'runtime_state', side_effect=ConnectionError('offline')):
                self.assertEqual(manager.runtime_status()['states']['fb-020']['state'], 'unknown')

    def test_runtime_aggregates_multiple_accounts_once_even_when_retained_metadata_overlaps(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = fixture(root)
            manager = bridge.env_management
            bridge.agent_activity = AgentActivity()
            rows = bridge.backend.env_state()
            rows['envs'].extend([{'env_id': child, 'proxy':'direct', 'tabs':[{'target_id':child+'-page'}]}
                                 for child in ('account-a', 'account-b')])
            accounts = {'fb-020': {'accounts':[{'session_env_id':'account-a'}, {'session_env_id':'account-b'}],
                                    'retained_sessions':['account-a']}}
            a1, a2, b1 = [bridge.agent_activity.begin(child) for child in ('account-a','account-a','account-b')]
            with patch.object(bridge.backend, 'env_state', return_value=rows), patch.object(bridge.backend, 'runtime_state', return_value={'ok': True, 'active': 'fb-020', 'running_envs': ['fb-020','account-a','account-b']}), patch.object(bridge.backend, 'live_state', side_effect=AssertionError('status must not enumerate all tabs')), patch.object(manager, '_workspace', return_value=SimpleNamespace(metadata=lambda:accounts)):
                states = manager.runtime_status()['states']
                self.assertEqual(states['fb-020']['agent_active_count'], 3)
                self.assertEqual(states['account-a']['agent_active_count'], 2)
                self.assertEqual(states['account-b']['agent_active_count'], 1)
                self.assertEqual(states['fb-008']['agent'], 'idle')
                bridge.agent_activity.finish(a1, False)
                bridge.agent_activity.finish(b1, True)
                self.assertEqual(manager.runtime_status()['states']['fb-020']['state'], 'agent-running')
                bridge.agent_activity.finish(a2, True)
                row = manager.runtime_status()['states']['fb-020']
                self.assertTrue(row['agent_warning'])
                self.assertEqual((row['agent_active_count'], row['agent_completed_count'], row['agent_failed_count']), (0, 2, 1))

    def test_runtime_combines_proxy_evidence_and_agent_without_false_completion(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = fixture(root, probe=lambda proxy: {'ok':False})
            manager = bridge.env_management
            bridge.agent_activity = AgentActivity()
            rows = bridge.backend.env_state()
            rows['active'] = 'fb-008'
            rows['envs'][0].update(proxy='http://127.0.0.1:1234', tabs=[{}])
            with patch.object(bridge.backend, 'env_state', return_value=rows), patch.object(bridge.backend, 'runtime_state', return_value={'ok': True, 'active': rows['active'], 'running_envs': ['fb-008']}), patch.object(bridge.backend, 'live_state', side_effect=AssertionError('status must not enumerate all tabs')):
                manager.runtime_status()
                self.assertFalse(manager.proxy_health.pending, 'status must not start a proxy probe')
                self.assertEqual(manager.runtime_status()['states']['fb-008']['network'],'unchecked')
                key = ('http://127.0.0.1:1234', '')
                with manager.proxy_health.lock:
                    manager.proxy_health.results[key] = (time.monotonic(), {'status':'failed','at':time.time()})
                self.assertEqual(manager.runtime_status()['states']['fb-008']['state'], 'network-error')
                token = bridge.agent_activity.begin('fb-008')
                status = manager.runtime_status()['states']['fb-008']
                self.assertEqual(status['state'], 'agent-running')
                self.assertTrue(status['network_warning'])
                bridge.agent_activity.finish(token, True)
                self.assertEqual(manager.runtime_status()['states']['fb-008']['state'], 'network-error')
                # New positive evidence resolves the warning and restores the completion marker.
                key = ('http://127.0.0.1:1234', '')
                with manager.proxy_health.lock:
                    manager.proxy_health.results[key] = (time.monotonic(), {'status':'success','at':time.time()})
                self.assertEqual(manager.runtime_status()['states']['fb-008']['state'], 'agent-complete')
                rows['active'] = 'fb-020'
                self.assertEqual(manager.runtime_status()['states']['fb-008']['state'], 'agent-complete')
                rows['active'] = 'fb-008'
                self.assertEqual(manager.runtime_status()['states']['fb-008']['state'], 'agent-complete')


class AgentLifecycleTests(ScopeTests):
    def setUp(self):
        super().setUp()
        self.bridge.agent_activity = AgentActivity()

    def test_denial_and_invalid_task_do_not_create_activity(self):
        self.task(env='fb-020')
        self.assertEqual(self.bridge.agent_activity.snapshot(), {})
        self.grant(['fb-020'])
        self.bridge.run_task({'env':'fb-020','steps':[]}, session_id='A')
        self.assertEqual(self.bridge.agent_activity.snapshot(), {})

    def test_activity_tracks_actual_selected_session_and_exception(self):
        self.grant(['fb-020'])
        self.bridge.env_management = SimpleNamespace(selected_session=lambda env:'acct-precise')
        def step(env, step, sid=None):
            self.assertEqual(self.bridge.agent_activity.snapshot()['acct-precise']['state'], 'running')
            return {'ok':True}
        self.bridge.run_step = step
        self.assertTrue(self.task()['ok'])
        self.assertEqual(self.bridge.agent_activity.snapshot()['acct-precise']['state'], 'complete')
        self.bridge.ensure_live = lambda env: (_ for _ in ()).throw(RuntimeError('ERR_PROXY_CONNECTION_FAILED'))
        self.assertFalse(self.task()['ok'])
        self.assertEqual(self.bridge.agent_activity.snapshot()['acct-precise']['state'], 'failed')

    def test_parallel_limit_queues_every_environment_instead_of_dropping_the_rest(self):
        targets = ['fb-'+str(i) for i in range(12, 18)]
        self.bridge.backend.live_state = lambda **kw: {'window_per_env':True, 'active':targets[0], 'envs':[{'env_id':env} for env in targets]}
        self.bridge.permissions = {'max_parallel':2}
        barrier = threading.Barrier(2)
        observed_counts = []
        def step(env, value, sid=None):
            observed_counts.append(sum(row.get('active_count', 0) for row in self.bridge.agent_activity.snapshot().values()))
            barrier.wait(timeout=3)
            return {'ok':True}
        self.bridge.run_step = step
        reply = self.bridge.run_task({'targets':targets, 'mode':'parallel', 'steps':[{'read':{}}]})
        self.assertTrue(reply['ok'])
        self.assertEqual(set(reply['per_env']), set(targets))
        self.assertEqual(reply['summary']['envs'], 6)
        self.assertEqual(max(observed_counts), 2)
        self.assertTrue(all(row['state']=='complete' for row in self.bridge.agent_activity.snapshot().values()))

    def test_repeated_environment_ids_do_not_repeat_browser_actions(self):
        reply = self.bridge.run_task({'targets':['fb-020','fb-020'], 'steps':[{'read':{}}]})
        self.assertTrue(reply['ok'])
        self.assertEqual(self.calls.count(('step', 'fb-020')), 1)

    def test_account_mapping_failure_is_reported_without_losing_other_results(self):
        targets = ['fb-020', 'fb-021', 'fb-022']
        self.bridge.backend.live_state = lambda **kw: {'window_per_env':True}
        def selected(env):
            if env=='fb-021': raise ValueError('account removed')
            return env+'-account'
        self.bridge.env_management = SimpleNamespace(selected_session=selected)
        reply = self.bridge.run_task({'targets':targets, 'mode':'parallel', 'steps':[{'read':{}}]})
        self.assertFalse(reply['ok'])
        self.assertEqual(set(reply['per_env']), set(targets))
        self.assertEqual(reply['summary']['failed'], 1)
        self.assertFalse(reply['per_env']['fb-021']['ok'])
        self.assertEqual(self.bridge.agent_activity.snapshot()['fb-020-account']['state'], 'complete')

    def test_readonly_busy_probe_detects_a_browser_task_on_another_thread(self):
        self.grant(['fb-020'])
        entered, release = threading.Event(), threading.Event()
        def step(*args):
            entered.set()
            release.wait(2)
            return {'ok':True}
        self.bridge.run_step = step
        worker = threading.Thread(target=self.task)
        self.assertFalse(self.bridge.execution_busy())
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.bridge.execution_busy())
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(self.bridge.execution_busy())


if __name__ == '__main__':
    unittest.main()
