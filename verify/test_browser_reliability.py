"""Failures must be explicit, must stop dependent steps and never replay writes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_backend import BrowserBackend, BrowserCdp
from _env_driver import Cdp
from lan_bridge import Bridge


class TransportTests(unittest.TestCase):
    def test_missing_response_is_not_empty_success(self):
        with patch.object(Cdp, 'send', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                BrowserCdp.__new__(BrowserCdp).send('Input.dispatchMouseEvent')

    def test_protocol_error_is_not_empty_success(self):
        with patch.object(Cdp, 'send', return_value={'error': {'message': 'target closed'}}):
            with self.assertRaisesRegex(RuntimeError, 'target closed'):
                BrowserCdp.__new__(BrowserCdp).send('Runtime.evaluate')

    def test_javascript_exception_is_not_missing_element(self):
        with patch.object(Cdp, 'send', return_value={'result': {'exceptionDetails': {'text': 'bad selector'}}}):
            with self.assertRaisesRegex(RuntimeError, 'bad selector'):
                BrowserCdp.__new__(BrowserCdp).evaluate('expression')


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = BrowserBackend(port=9999)
        self.cdp = Mock()
        self.backend._cdp = Mock(return_value=self.cdp)

    def test_ready_navigation_returns_without_fixed_delay(self):
        self.cdp.send.side_effect = [
            {'result': {'loaderId': 'new'}},
            {'result': {'frameTree': {'frame': {'loaderId': 'new', 'url': 'https://example.org'}}}},
        ]
        self.cdp.evaluate.return_value = 'interactive'
        self.backend.observations['A'] = object()
        with patch('browser_backend.time.sleep') as sleep:
            result = self.backend.goto('A', 'https://example.org')
        self.assertEqual(result['ready'], 'interactive')
        sleep.assert_not_called()
        self.assertNotIn('A', self.backend.observations)
        self.assertEqual(sum(c.args[0] == 'Page.navigate' for c in self.cdp.send.call_args_list), 1)

    def test_old_document_ready_does_not_finish_new_navigation(self):
        self.cdp.send.side_effect = [
            {'result': {'loaderId': 'new'}},
            {'result': {'frameTree': {'frame': {'loaderId': 'old'}}}},
            {'result': {'frameTree': {'frame': {'loaderId': 'new'}}}},
        ]
        self.cdp.evaluate.return_value = 'complete'
        with patch('browser_backend.time.sleep') as sleep:
            self.backend.goto('A', 'https://example.org')
        sleep.assert_called_once_with(.05)

    def test_navigation_failure_explicit(self):
        self.cdp.send.return_value = {'result': {'errorText': 'net::ERR_NAME_NOT_RESOLVED'}}
        result = self.backend.goto('A', 'https://invalid.example')
        self.assertFalse(result['ok'])
        self.assertTrue(result['navigation_sent'])
        self.cdp.evaluate.assert_not_called()

    def test_explicit_navigation_delay_preserved(self):
        self.cdp.send.return_value = {'result': {'loaderId': 'new'}}
        with patch('browser_backend.time.sleep') as sleep:
            result = self.backend.goto('A', 'https://example.org', 800)
        sleep.assert_called_once_with(.8)
        self.assertEqual(result['waited_ms'], 800)

    def test_uncertain_kernel_click_is_never_replayed_over_cdp(self):
        self.backend.kernel_available = lambda: True
        self.backend.kernel_task = Mock(side_effect=TimeoutError('response lost'))
        result = self.backend.click_selectors('A', ['button'])
        self.assertFalse(result['ok'])
        self.assertTrue(result['outcome_unknown'])
        self.cdp.evaluate.assert_not_called()
        self.cdp.send.assert_not_called()

    def test_same_step_reuses_validation_but_next_step_validates_again(self):
        del self.backend._cdp
        self.backend.strict_resolution = True
        self.backend._sessions['A'] = ('target', self.cdp)
        self.backend.target_for_env = Mock(return_value={'id': 'target'})
        with self.backend.step_context():
            self.backend._cdp('A')
            self.backend._cdp('A')
        self.assertEqual(self.backend.target_for_env.call_count, 1)
        with self.backend.step_context():
            self.backend._cdp('A')
        self.assertEqual(self.backend.target_for_env.call_count, 2)

    def test_failed_step_discards_connection_without_replay(self):
        self.backend._sessions['A'] = ('target', self.cdp)
        with self.assertRaises(RuntimeError):
            with self.backend.step_context():
                self.backend._step_local.connections['A'] = self.cdp
                raise RuntimeError('lost response')
        self.cdp.close.assert_called_once()
        self.assertNotIn('A', self.backend._sessions)

    def test_live_failure_not_cached_as_empty_environment_catalog(self):
        with patch('env_cli.send', return_value={'ok': False, 'error': 'busy'}):
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                self.backend.live_state()
        self.assertIsNone(self.backend._live_cache)

    def test_runtime_state_uses_compact_native_command_and_caches_briefly(self):
        reply = {'ok': True, 'active': 'fb-020', 'running_envs': ['fb-008', 'fb-020']}
        with patch('env_cli.resolve_profile_dir', return_value=Path(r'C:\profile')), \
                patch('env_cli.send', return_value=reply) as send:
            self.assertEqual(self.backend.runtime_state(), reply)
            self.assertEqual(self.backend.runtime_state(), reply)
        send.assert_called_once_with(Path(r'C:\profile'), {'command': 'runtime'}, 2.5)

    def test_runtime_state_falls_back_for_browser_builds_without_compact_command(self):
        full = {'active': 'fb-020', 'envs': [
            {'env_id': 'fb-008', 'tabs': [{}]}, {'env_id': 'fb-012', 'tabs': []},
            {'env_id': 'fb-020', 'tabs': [{}]}]}
        with patch('env_cli.resolve_profile_dir', return_value=Path(r'C:\profile')), \
                patch('env_cli.send', side_effect=[{'ok': False, 'error': 'unknown command'}, full]) as send:
            state = self.backend.runtime_state()
        self.assertEqual(state, {'ok': True, 'active': 'fb-020', 'running_envs': ['fb-008', 'fb-020']})
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[1], {'command': 'runtime'})
        self.assertEqual(send.call_args_list[1].args[1], {'command': 'list'})


class BridgeOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.permissions = {'preset': 'full'}
        self.bridge.snapshots = {}
        self.bridge.backend = Mock()
        self.bridge.backend.step_context.side_effect = nullcontext

    def test_snapshot_failure_does_not_turn_completed_click_into_retry(self):
        self.bridge.backend.act.return_value = {'action': 'click', 'trusted': True}
        self.bridge.backend.snapshot.side_effect = RuntimeError('navigation destroyed context')
        with patch('lan_bridge.audit'):
            result = self.bridge.run_step('A', {'act': {'id': 'e0'}})
        self.assertEqual(result['action'], 'click')
        self.assertIn('snapshot_warning', result)
        self.assertNotIn('error', result)
        self.bridge.backend.act.assert_called_once()
        self.bridge.backend._drop_session.assert_called_once_with('A')

    def test_ensure_live_uses_activation_ack_without_second_native_list_roundtrip(self):
        self.bridge.udd = Path(r'C:\profile')
        self.bridge.permissions = {'preset': 'full'}
        self.bridge.backend.live_state.return_value = {
            'active': 'fb-008', 'envs': [{'env_id': 'fb-008'}, {'env_id': 'fb-020'}]}
        self.bridge.backend._live_cache = (1.0, {'active': 'fb-008'})
        with patch('env_cli.resolve_profile_dir', return_value=Path(r'C:\profile')), \
                patch('env_cli.send', return_value={'ok': True, 'active': 'fb-020'}) as send, \
                patch('lan_bridge.audit'):
            self.bridge.ensure_live('fb-020')
        self.bridge.backend.live_state.assert_called_once_with(max_age=0)
        send.assert_called_once_with(Path(r'C:\profile'),
                                     {'command': 'activate', 'env_id': 'fb-020'}, 20)
        self.assertIsNone(self.bridge.backend._live_cache)

    def test_ensure_live_current_environment_is_a_noop(self):
        self.bridge.backend.live_state.return_value = {
            'active': 'fb-020', 'envs': [{'env_id': 'fb-020'}]}
        with patch('env_cli.send') as send:
            self.bridge.ensure_live('fb-020')
        send.assert_not_called()
        self.bridge.backend.live_state.assert_called_once_with(max_age=0)


if __name__ == '__main__':
    unittest.main()
