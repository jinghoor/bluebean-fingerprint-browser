"""Regression cases from agent execution review; no user profiles or live inputs."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_backend import BrowserBackend, Observation
from lan_bridge import Bridge, needs_approval


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.backend=BrowserBackend(port=9999)
        self.cdp=Mock()
        self.backend._cdp=Mock(return_value=self.cdp)
        self.summary={'url':'https://example.test/','count':1,'names':['Submit'],
                      'ready':'interactive','fingerprint':123}
        self.cdp.evaluate.return_value=json.dumps({'url':'https://example.test/',
            'ready':'interactive','document_token':'doc',
            'elements':[{'id':'e0','tag':'button','name':'Submit','href':'',
                         'selector':'#submit','bbox':[0,0,30,20]}],'_snapshot':self.summary})

    def test_latest_observation_ids_do_not_alias_previous_target(self):
        first=self.backend.observe('A')['elements'][0]['id']
        second=self.backend.observe('A')['elements'][0]['id']
        self.assertNotEqual(first,second)
        self.cdp.reset_mock()
        result=self.backend.act('A',first)
        self.assertEqual(result['error_code'],'STALE_OBSERVATION')
        self.assertFalse(result['action_sent'])
        self.cdp.send.assert_not_called()
        self.cdp.evaluate.assert_not_called()

    def test_missing_observation_for_type_is_known_unsent(self):
        result=self.backend.type_text('A','outdated','must-not-type')
        self.assertEqual(result['error_code'],'STALE_OBSERVATION')
        self.assertFalse(result['action_sent'])
        self.backend._cdp.assert_not_called()

    def test_summary_not_duplicated_in_tool_output(self):
        result=self.backend.observe('A')
        self.assertNotIn('_snapshot',result)
        self.assertNotIn('summary',result)
        self.assertEqual(self.backend.observations['A'].summary['names'],['Submit'])
        self.assertTrue(self.backend.observations['A'].summary['readiness']['background_loading'])
        self.cdp.evaluate.assert_called_once()

    def test_no_observation_result_does_not_silently_report_empty_page(self):
        self.backend.observe('A')
        self.cdp.evaluate.return_value=None
        with self.assertRaisesRegex(RuntimeError,'observation was not returned'):
            self.backend.observe('A')
        self.assertNotIn('A',self.backend.observations)

    def test_transport_failure_invalidates_old_observation(self):
        self.backend.observe('A')
        self.cdp.evaluate.side_effect=RuntimeError('connection closed')
        with self.assertRaises(RuntimeError):self.backend.observe('A')
        self.assertNotIn('A',self.backend.observations)

    def test_bridge_uses_observation_summary_without_another_roundtrip(self):
        bridge=Bridge.__new__(Bridge)
        bridge.backend=self.backend
        bridge.permissions={'preset':'full'}
        bridge.snapshots={}
        with patch('lan_bridge.audit'):
            result=bridge.run_step('A',{'observe':{}})
        self.assertEqual(result['env'],'A')
        self.assertEqual(bridge.snapshots['A']['count'],1)
        self.cdp.evaluate.assert_called_once()

    def test_missing_summary_clears_old_baseline_without_delaying_observation(self):
        bridge=Bridge.__new__(Bridge)
        bridge.backend=Mock()
        bridge.backend.step_context.side_effect=nullcontext
        bridge.backend.observe.return_value={'env':'A'}
        bridge.backend.observations={'A':Observation('A',1)}
        bridge.permissions={'preset':'full'}
        bridge.snapshots={'A':self.summary}
        with patch('lan_bridge.audit'):
            bridge.run_step('A',{'observe':{}})
        self.assertNotIn('A',bridge.snapshots)
        bridge.backend.snapshot.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def test_failed_ownership_check_is_not_discovered_twice(self):
        backend=BrowserBackend(port=9123)
        old=Mock()
        backend._sessions['A']=('old',old)
        backend.strict_resolution=True
        backend.target_for_env=Mock(side_effect=RuntimeError('ownership not confirmed'))
        with patch('browser_backend.BrowserCdp') as connect:
            with self.assertRaisesRegex(RuntimeError,'ownership'):
                backend._cdp('A')
        backend.target_for_env.assert_called_once_with('A')
        old.close.assert_called_once()
        self.assertNotIn('A',backend._sessions)
        connect.assert_not_called()

    def test_changed_authoritative_target_is_resolved_once_and_reused(self):
        backend=BrowserBackend(port=9123)
        old,new=Mock(),Mock()
        backend._sessions['A']=('old',old)
        backend.strict_resolution=True
        backend.target_for_env=Mock(return_value={'id':'new','webSocketDebuggerUrl':'ws://127.0.0.1/fixture'})
        with patch('browser_backend.BrowserCdp',return_value=new) as connect:
            with backend.step_context():
                self.assertIs(backend._cdp('A'),new)
                self.assertIs(backend._cdp('A'),new)
        backend.target_for_env.assert_called_once_with('A')
        connect.assert_called_once_with('ws://127.0.0.1/fixture',open_timeout=8)
        old.close.assert_called_once()

    def test_pinned_port_failure_does_not_search_other_browsers(self):
        backend=BrowserBackend(port=9123)
        with patch('browser_backend.LOOPBACK_HTTP.open',side_effect=OSError('disconnected')), \
             patch.object(BrowserBackend,'debug_port') as rediscover:
            with self.assertRaises(OSError):backend.targets()
        self.assertEqual(backend._explicit_port,9123)
        rediscover.assert_not_called()

    def test_target_discovery_uses_direct_loopback_http(self):
        backend=BrowserBackend(port=9123)
        response=Mock()
        response.read.return_value=b'[{"id":"fixture","type":"page"}]'
        opened=Mock()
        opened.__enter__=Mock(return_value=response)
        opened.__exit__=Mock(return_value=False)
        with patch('browser_backend.LOOPBACK_HTTP.open',return_value=opened) as direct, \
             patch('browser_backend.urllib.request.urlopen') as system_proxy:
            self.assertEqual(backend.targets()[0]['id'],'fixture')
        direct.assert_called_once_with('http://127.0.0.1:9123/json/list',timeout=8)
        system_proxy.assert_not_called()

    def test_only_read_only_wait_bypasses_write_approval(self):
        self.assertFalse(needs_approval('workspace','wait_for'))
        for action in ('act','type','goto','batch','clicks'):
            self.assertTrue(needs_approval('workspace',action))


if __name__=='__main__':unittest.main(verbosity=2)
