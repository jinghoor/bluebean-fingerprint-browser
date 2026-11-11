"""Scope enforcement tests: no browser activation is allowed before authorization."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_browser_scope import SessionBrowserScopes
from lan_bridge import Bridge
from browser_backend import BrowserBackend


class Backend:
    strict_resolution = False

    def live_state(self, **kwargs):
        return {"active": "fb-012", "envs": [{"env_id": f"fb-{i:03}"} for i in range(12, 23)]}

    def env_state(self):
        return self.live_state()


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scopes = SessionBrowserScopes(Path(self.tmp.name) / 'grants.json')
        self.known = {f"fb-{i:03}" for i in range(12, 23)}
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.browser_scopes = self.scopes
        self.bridge.execution_lock = threading.RLock()
        self.bridge.backend = Backend()
        self.bridge.permissions = {}
        self.calls = []
        self.bridge.ensure_live = lambda env: self.calls.append(('activate', env))
        self.bridge.run_step = lambda env, step, sid=None: self.calls.append(('step', env)) or {'step': 'read'}
        self.audit = patch('lan_bridge.audit')
        self.audit.start()

    def tearDown(self):
        self.audit.stop()
        self.tmp.cleanup()

    def grant(self, ids, sid='A'):
        return self.scopes.set(sid, ids, self.known)

    def task(self, **args):
        return self.bridge.run_task({'steps': [{'read': {}}], **args}, session_id='A')

    def test_empty_scope_denies_default(self):
        self.assertFalse(self.task()['ok'])
        self.assertEqual(self.calls, [])

    def test_account_bindings_persist_per_chat_and_drop_on_revoke(self):
        self.grant(['fb-020']); self.grant(['fb-020'], 'B')
        self.scopes.bind_account('A','fb-020','account-a')
        self.scopes.bind_account('B','fb-020','account-b')
        loaded=SessionBrowserScopes(self.scopes.path)
        self.assertEqual(loaded.selected_account('A','fb-020'),'account-a')
        self.assertEqual(loaded.selected_account('B','fb-020'),'account-b')
        self.grant(['fb-020','fb-021'])
        self.assertEqual(self.scopes.selected_account('A','fb-020'),'account-a')
        self.grant([]); self.grant(['fb-020'])
        self.assertIsNone(self.scopes.selected_account('A','fb-020'))
        with self.assertRaises(PermissionError): self.scopes.bind_account('A','fb-021','foreign')

    def test_empty_scope_denies_explicit(self):
        self.assertTrue(self.task(env='fb-020')['scope_denied'])
        self.assertEqual(self.calls, [])

    def test_single_uses_grant_not_active(self):
        self.grant(['fb-020'])
        self.assertTrue(self.task()['ok'])
        self.assertEqual(self.calls, [('activate', 'fb-020'), ('step', 'fb-020')])

    def test_granted_environment_maps_to_its_selected_isolated_account_session(self):
        self.grant(['fb-020'])
        self.bridge.env_management=type('Workspace',(),{'selected_session':lambda _self,env:'acct-unique-session' if env=='fb-020' else env})()
        self.assertTrue(self.task()['ok'])
        self.assertEqual(self.calls,[('activate','acct-unique-session'),('step','acct-unique-session')])

    def test_denies_unselected_before_activation(self):
        self.grant(['fb-020'])
        self.assertTrue(self.task(env='fb-012')['scope_denied'])
        self.assertEqual(self.calls, [])

    def test_mixed_targets_rejected_atomically(self):
        self.grant(['fb-020'])
        self.assertFalse(self.task(targets=['fb-020', 'fb-012'])['ok'])
        self.assertEqual(self.calls, [])

    def test_wildcard_means_selection(self):
        self.grant(['fb-020', 'fb-021'])
        self.assertEqual(list(self.task(targets=['*'])['per_env']), ['fb-020', 'fb-021'])
        self.assertNotIn(('activate', 'fb-012'), self.calls)

    def test_multiselect_above_parallel_limit_is_not_truncated(self):
        ids = [f'fb-{i:03}' for i in range(16, 23)]
        self.grant(ids)
        result = self.task()
        self.assertEqual(list(result['per_env']), ids)
        self.assertEqual(result['mode'], 'sequential')

    def test_sessions_are_independent(self):
        self.grant(['fb-020'])
        self.grant(['fb-021'], 'B')
        result = self.bridge.run_task({'steps': [{'read': {}}]}, session_id='B')
        self.assertEqual(list(result['per_env']), ['fb-021'])
        self.assertEqual(self.scopes.get('A')['env_ids'], ['fb-020'])

    def test_persists_across_restart(self):
        self.grant(['fb-020', 'fb-021'])
        self.assertEqual(SessionBrowserScopes(self.scopes.path).get('A')['env_ids'], ['fb-020', 'fb-021'])

    def test_unknown_and_wildcard_cannot_be_granted(self):
        for ids in [['fb-missing'], ['*'], 'fb-020', [None]]:
            with self.assertRaises(ValueError):
                self.grant(ids)

    def test_clear_revokes_access(self):
        self.grant(['fb-020'])
        self.grant([])
        self.assertFalse(self.task()['ok'])
        self.assertEqual(self.calls, [])

    def test_revoke_between_steps_stops_subsequent_step(self):
        self.grant(['fb-020'])
        def step(env, value, sid):
            self.calls.append(('step', env))
            self.grant([])
            return {'step': 'read'}
        self.bridge.run_step = step
        result = self.task(steps=[{'read': {}}, {'read': {}}])
        self.assertFalse(result['ok'])
        self.assertEqual(self.calls.count(('step', 'fb-020')), 1)

    def test_conflicting_or_invalid_targets_have_no_side_effect(self):
        self.grant(['fb-020'])
        for args in [{'env': 'fb-020', 'targets': ['fb-021']}, {'targets': '*'}, {'targets': [None]}]:
            self.assertFalse(self.task(**args)['ok'])
        self.assertEqual(self.calls, [])

    def test_failed_activation_does_not_run_step(self):
        self.grant(['fb-020'])
        def fail(env):
            raise RuntimeError('cannot activate')
        self.bridge.ensure_live = fail
        self.assertFalse(self.task()['ok'])
        self.assertEqual(self.calls, [])

    def test_denied_or_failed_step_stops_dependent_steps(self):
        self.grant(['fb-020'])
        for outcome in [{'denied': True, 'error': 'approval deny'}, {'ok': False, 'error': 'navigation failed'}]:
            self.calls.clear()
            self.bridge.run_step = lambda *args: self.calls.append('run') or outcome
            result = self.task(steps=[{'goto': {}}, {'read': {}}])
            self.assertFalse(result['ok'])
            self.assertEqual(self.calls.count('run'), 1)

    def test_malformed_steps_rejected_before_activation(self):
        self.grant(['fb-020'])
        for steps in [[{'unknown': {}}], [{'goto': 'https://example.com'}], [{'read': {}, 'wait': {}}]]:
            self.assertFalse(self.task(steps=steps)['ok'])
        self.assertEqual(self.calls, [])

    def test_read_sequence_activates_once_but_mutation_rechecks(self):
        self.grant(['fb-020'])
        result = self.task(steps=[{'read': {}}, {'observe': {}}, {'read': {}}, {'act': {'id': 'e0'}}, {'read': {}}])
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls.count(('activate', 'fb-020')), 2)
        self.assertEqual(self.calls.count(('step', 'fb-020')), 5)

    def test_target_waits_do_not_repeat_activation(self):
        self.grant(['fb-020'])
        result=self.task(steps=[{'observe':{}},{'wait_for':{'selector':'#ready'}},
                                {'wait_for':{'text':'Done'}},{'read':{}}])
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls.count(('activate','fb-020')),1)

    def test_target_wait_cannot_access_unselected_environment(self):
        self.grant(['fb-020'])
        result=self.task(env='fb-012',steps=[{'wait_for':{'selector':'#ready'}}])
        self.assertTrue(result['scope_denied'])
        self.assertEqual(self.calls,[])

    def test_failed_target_wait_stops_click_without_retry(self):
        self.grant(['fb-020'])
        self.bridge.run_step=lambda *args:self.calls.append('run') or {
            'ok':False,'action_sent':False,'error_code':'PAGE_CONDITION_TIMEOUT'}
        result=self.task(steps=[{'wait_for':{'selector':'#ready'}},{'act':{'id':'e0'}}])
        self.assertFalse(result['ok'])
        self.assertEqual(self.calls.count('run'),1)

    def test_scope_can_be_revoked_while_a_step_is_in_flight(self):
        self.grant(['fb-020'])
        entered, release = threading.Event(), threading.Event()
        output = []
        def slow(*args):
            entered.set()
            release.wait(2)
            return {'step': 'wait'}
        self.bridge.run_step = slow
        worker = threading.Thread(target=lambda: output.append(self.task(steps=[{'wait': {}}, {'read': {}}])))
        worker.start()
        saved = threading.Event()
        saver = threading.Thread(target=lambda: (self.grant([]), saved.set()))
        try:
            self.assertTrue(entered.wait(1))
            saver.start()
            self.assertTrue(saved.wait(.3), 'grant save blocked behind browser I/O')
        finally:
            release.set()
            worker.join(3)
            if saver.ident is not None:
                saver.join(3)
        self.assertFalse(output[0]['ok'])
        self.assertEqual(len(output[0]['per_env']['fb-020']['results']), 2)
        self.assertIn('请先', output[0]['per_env']['fb-020']['error'])

    def test_mutation_exception_marks_unknown_outcome_and_stops(self):
        self.grant(['fb-020'])
        def fail(*args):
            raise TimeoutError('lost click reply')
        self.bridge.run_step = fail
        result = self.task(steps=[{'act': {'id': 'e0'}}, {'read': {}}])
        self.assertFalse(result['ok'])
        steps = result['per_env']['fb-020']['results']
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0]['outcome_unknown'])

    def test_strict_resolution_does_not_guess_from_url(self):
        backend = BrowserBackend.__new__(BrowserBackend)
        backend.strict_resolution = True
        backend.live_state = lambda: {'active': 'fb-020', 'envs': [{'env_id': 'fb-020', 'tabs': []}]}
        backend.targets = lambda: [{'id': 'foreign', 'url': 'https://example.org/?e=fb-020'}]
        with self.assertRaisesRegex(RuntimeError, 'authoritative'):
            backend.target_for_env('fb-020')

    def test_strict_resolution_uses_authoritative_target(self):
        backend = BrowserBackend.__new__(BrowserBackend)
        backend.strict_resolution = True
        backend.live_state = lambda: {'envs': [{'env_id': 'fb-020', 'tabs': [{'target_id': 'right'}]}]}
        backend.targets = lambda: [{'id': 'wrong'}, {'id': 'right'}]
        self.assertEqual(backend.target_for_env('fb-020')['id'], 'right')


if __name__ == '__main__':
    unittest.main(verbosity=2)
