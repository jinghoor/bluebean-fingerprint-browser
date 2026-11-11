"""Target-specific waits stay bounded, read-only and independent of page load."""
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from page_readiness import wait_for, readiness
from browser_backend import BrowserBackend, Observation
from lan_bridge import Bridge


def sample(**changes):
    return {'matched':True,'document_token':'doc1','url':'https://example.test/',
            'box':[0,0,100,30],'ready':'interactive',**changes}


class Clock:
    def __init__(self): self.now=0
    def monotonic(self): return self.now
    def sleep(self,seconds): self.now+=seconds


class WaitTests(unittest.TestCase):
    def run_wait(self, values, **condition):
        cdp=Mock()
        cdp.evaluate.side_effect=values if isinstance(values,list) else None
        if not isinstance(values,list): cdp.evaluate.return_value=values
        clock=Clock()
        with patch('page_readiness.time.monotonic',side_effect=clock.monotonic), \
             patch('page_readiness.time.sleep',side_effect=clock.sleep):
            result=wait_for(cdp,condition)
        cdp.send.assert_not_called()
        return result,cdp

    def test_interactive_target_returns_before_complete(self):
        result,_=self.run_wait(sample(),selector='#ready',timeout_ms=1000)
        self.assertTrue(result['ok'])
        self.assertFalse(result['action_sent'])
        self.assertFalse(result['readiness']['load_complete'])
        self.assertTrue(result['readiness']['background_loading'])
        self.assertAlmostEqual(result['waited_ms'],100)

    def test_waits_only_until_target_enabled(self):
        result,_=self.run_wait([sample(matched=False,reason='target_disabled'),sample(),sample(),sample()],
                              selector='#ready',timeout_ms=1000)
        self.assertTrue(result['ok'])
        self.assertAlmostEqual(result['waited_ms'],150)

    def test_target_movement_resets_stability(self):
        result,_=self.run_wait([sample(),sample(box=[10,0,100,30]),sample(box=[10,0,100,30]),
                               sample(box=[10,0,100,30])], selector='#moving',timeout_ms=1000)
        self.assertTrue(result['ok'])
        self.assertAlmostEqual(result['waited_ms'],150)

    def test_covered_target_times_out_without_input(self):
        result,cdp=self.run_wait(sample(matched=False,reason='target_covered'),selector='#covered',timeout_ms=120)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'],'PAGE_CONDITION_TIMEOUT')
        self.assertFalse(result['action_sent'])
        self.assertEqual(result['state']['reason'],'target_covered')
        self.assertEqual(result['waited_ms'],120)

    def test_stale_document_or_target_returns_without_click(self):
        for flag in ('stale_document','stale_target'):
            result,_=self.run_wait(sample(**{flag:True}),selector='#old')
            self.assertEqual(result['error_code'],'STALE_OBSERVATION')
            self.assertEqual(result['next_action'],'observe')

    def test_navigation_context_retried_without_replaying_navigation(self):
        result,cdp=self.run_wait([RuntimeError('Execution context was destroyed'),sample()],state='dom')
        self.assertTrue(result['ok'])
        self.assertEqual(cdp.evaluate.call_count,2)

    def test_transport_failure_is_not_a_success_or_action_retry(self):
        with self.assertRaisesRegex(RuntimeError,'connection closed'):
            self.run_wait([RuntimeError('connection closed')],state='dom')

    def test_explicit_complete_still_waits(self):
        result,_=self.run_wait([sample(matched=False),sample(ready='complete')],state='complete')
        self.assertTrue(result['readiness']['load_complete'])

    def test_dom_without_selector_does_not_add_stability_delay(self):
        result,_=self.run_wait(sample(box=None),state='dom')
        self.assertEqual(result['waited_ms'],0)

    def test_invalid_options_rejected_before_evaluation(self):
        for condition in ({'timeout_ms':-1},{'timeout_ms':float('nan')},{'timeout_ms':30001},
                          {'timeout_ms':True},{'stable_ms':1001},{'selector':5},
                          {'state':'actionable'},{'state':'networkidle'},{'script':'arbitrary'}):
            with self.subTest(condition=condition):
                cdp=Mock()
                with self.assertRaises(ValueError):wait_for(cdp,condition)
                cdp.evaluate.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.backend=BrowserBackend(port=9999)
        self.cdp=Mock()
        self.backend._cdp=Mock(return_value=self.cdp)
        self.backend.observations['A']=Observation('A',1,[{
            'id':'e0','selector':'#submit','bbox':[0,0,20,20],
            'tag':'button','name':'Submit','href':''}],document_token='doc1')

    def test_act_wait_timeout_sends_no_input(self):
        failure={'ok':False,'action_sent':False,'error_code':'PAGE_CONDITION_TIMEOUT'}
        with patch.object(self.backend,'wait_for',return_value=failure):
            self.assertEqual(self.backend.act('A','e0'),failure)
        self.cdp.send.assert_not_called()

    def test_input_rechecks_editability_and_stale_document(self):
        with patch.object(self.backend,'wait_for',return_value={'ok':False,'error_code':'STALE_OBSERVATION'}) as wait:
            self.assertFalse(self.backend.type_text('A','e0','must-not-type')['ok'])
        self.assertTrue(wait.call_args.kwargs['editable'])
        self.assertEqual(wait.call_args.kwargs['document_token'],'doc1')
        self.cdp.send.assert_not_called()

    def test_click_uses_current_stable_geometry_only_once(self):
        with patch.object(self.backend,'wait_for',return_value={'ok':True,'state':{'box':[100,80,30,20]}}):
            result=self.backend.act('A','e0')
        self.assertEqual(result['at'],[115,90])
        events=[call.args[1]['type'] for call in self.cdp.send.call_args_list]
        self.assertEqual(events,['mouseMoved','mousePressed','mouseReleased'])

    def test_snapshot_budget_is_bounded(self):
        self.cdp.evaluate.return_value='{"ready":"interactive"}'
        result=self.backend.snapshot('A')
        self.assertEqual(self.cdp.evaluate.call_args.kwargs['timeout'],2)
        self.assertTrue(result['readiness']['dom_ready'])

    def test_partly_offscreen_target_uses_hit_tested_visible_point(self):
        with patch.object(self.backend,'wait_for',return_value={'ok':True,'state':{
                'box':[-190,80,240,20],'point':[25,90]}}):
            result=self.backend.act('A','e0')
        self.assertEqual(result['at'],[25,90])
        self.assertEqual(self.cdp.send.call_args_list[1].args[1]['x'],25)

    def test_unsupported_action_does_not_wait_or_send_input(self):
        with patch.object(self.backend,'wait_for') as wait:
            with self.assertRaises(ValueError):self.backend.act('A','e0','invalid')
            wait.assert_not_called()
        self.cdp.send.assert_not_called()

    def test_wait_step_calls_read_only_probe_and_no_snapshot(self):
        bridge=Bridge.__new__(Bridge)
        bridge.backend=Mock()
        bridge.backend.step_context.side_effect=nullcontext
        bridge.backend.wait_for.return_value={'ok':True,'condition_met':True}
        bridge.permissions={'preset':'full'}
        bridge.snapshots={}
        with patch('lan_bridge.audit'):
            result=bridge.run_step('A',{'wait_for':{'selector':'#success','text':'Done'}})
        self.assertTrue(result['ok'])
        bridge.backend.wait_for.assert_called_once_with('A',selector='#success',text='Done')
        bridge.backend.snapshot.assert_not_called()

    def test_no_snapshot_after_an_action_was_prevented(self):
        bridge=Bridge.__new__(Bridge)
        bridge.backend=Mock()
        bridge.backend.step_context.side_effect=nullcontext
        bridge.backend.act.return_value={'ok':False,'action_sent':False,'error_code':'STALE_OBSERVATION'}
        bridge.permissions={'preset':'full'}
        bridge.snapshots={}
        with patch('lan_bridge.audit'):
            result=bridge.run_step('A',{'act':{'id':'e0'}})
        self.assertFalse(result['ok'])
        bridge.backend.snapshot.assert_not_called()

    def test_packaging_import_closure_includes_readiness(self):
        spec=importlib.util.spec_from_file_location('readiness_builder',Path(__file__).resolve().parents[1]/'build-installer.py')
        builder=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertIn('page_readiness.py',builder.local_python_modules())


if __name__=='__main__':unittest.main(verbosity=2)
