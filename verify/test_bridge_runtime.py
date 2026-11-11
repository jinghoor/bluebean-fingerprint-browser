"""Source updates must not be reported ready while an old bridge is still loaded."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import _ensure_bridge as launch
from bridge_runtime import source_revision
from browser_backend import BrowserBackend, BrowserControlUnavailable
from lan_bridge import Bridge


class RevisionTests(unittest.TestCase):
    def test_changes_to_lazy_local_imports_change_revision(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            (root/'lan_bridge.py').write_text('def run():\n import helper\n',encoding='utf-8')
            (root/'helper.py').write_text('value=1\n',encoding='utf-8')
            before=source_revision(root)
            (root/'private-data.json').write_text('{"user":"fixture"}',encoding='utf-8')
            self.assertEqual(source_revision(root),before)
            (root/'helper.py').write_text('value=2\n',encoding='utf-8')
            self.assertNotEqual(source_revision(root),before)

    def test_import_cycles_are_bounded_and_deterministic(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            (root/'lan_bridge.py').write_text('import helper\n',encoding='utf-8')
            (root/'helper.py').write_text('import lan_bridge\n',encoding='utf-8')
            self.assertEqual(source_revision(root),source_revision(root))


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.patchers=[patch.object(launch,'startup_lock',side_effect=lambda *_a,**_k:nullcontext()),
            patch.object(launch,'source_revision',return_value='new'),
            patch.object(launch,'load_config',return_value=(8123,'fixture-token')),
            patch.object(launch,'atomic_json'),patch.object(launch,'start_bridge'),
            patch.object(launch,'stop_idle_owned_bridge'),patch.object(launch,'probe')]
        self.lock,self.revision,self.config,self.write,self.start,self.stop,self.probe=[p.start() for p in self.patchers]
        self.start.return_value.pid=42
        self.start.return_value.poll.return_value=None

    def tearDown(self):
        for p in reversed(self.patchers):p.stop()

    def test_current_bridge_is_reused_without_restart(self):
        self.probe.return_value={'ok':True,'pid':41,'current':True,'busy':False}
        result=launch.ensure_bridge(False)
        self.assertTrue(result['runtime_current'])
        self.assertFalse(result['started'])
        self.start.assert_not_called()
        self.stop.assert_not_called()

    def test_deferred_reload_is_visible_and_does_not_start_duplicate_service(self):
        self.probe.return_value={'ok':True,'pid':41,'current':False,'busy':True}
        self.stop.return_value=False
        result=launch.ensure_bridge(False)
        self.assertTrue(result['reload_pending'])
        self.assertFalse(result['runtime_current'])
        self.start.assert_not_called()
        self.write.assert_not_called()

    def test_idle_reload_preserves_endpoint_and_requires_new_revision(self):
        self.probe.side_effect=[{'ok':True,'pid':41,'current':False,'busy':False},
            {'ok':True,'pid':42,'current':True,'busy':False}]
        self.stop.return_value=True
        result=launch.ensure_bridge(False)
        self.assertTrue(result['reloaded'])
        self.assertTrue(result['runtime_current'])
        self.start.assert_called_once_with(8123)
        self.stop.assert_called_once()

    def test_foreign_service_is_never_terminated(self):
        self.probe.side_effect=[{'ok':False,'pid':41,'detail':'different instance'},
            {'ok':True,'pid':42,'current':True,'busy':False}]
        self.assertTrue(launch.ensure_bridge(False)['runtime_current'])
        self.stop.assert_not_called()
        self.start.assert_called_once_with(0)


class PortRecordTests(unittest.TestCase):
    def test_missing_control_is_known_unsent_not_proof_browser_is_closed(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            with patch('browser_backend.DATA_ROOT',root), \
                 patch('browser_backend.DEVTOOLS_PORT_FILE',root/'missing'), \
                 patch.object(BrowserBackend,'_cached_port',None), \
                 patch.object(BrowserBackend,'_port_from_process',return_value=None):
                with self.assertRaises(BrowserControlUnavailable) as raised:
                    BrowserBackend.debug_port()
                self.assertFalse(raised.exception.action_sent)
                self.assertEqual(raised.exception.error_code,'BROWSER_CONTROL_UNAVAILABLE')
                self.assertIn('may still be running',str(raised.exception))

    def test_failed_connection_does_not_report_input_as_possibly_sent(self):
        bridge=Bridge.__new__(Bridge)
        bridge.permissions={'preset':'full'}
        bridge.execution_lock=__import__('threading').RLock()
        bridge.backend=Mock()
        bridge.backend.step_context.side_effect=nullcontext
        bridge.ensure_live=Mock()
        bridge.run_step=Mock(side_effect=BrowserControlUnavailable('control unavailable'))
        reply=bridge.run_task({'env':'fixture','steps':[{'act':{'id':'old'}},{'read':{}}]})
        failed=reply['per_env']['fixture']['results'][0]
        self.assertEqual(failed['error_code'],'BROWSER_CONTROL_UNAVAILABLE')
        self.assertFalse(failed['action_sent'])
        self.assertNotIn('outcome_unknown',failed)
        bridge.run_step.assert_called_once()

    def test_fixed_port_record_does_not_need_powershell_or_devtools_port_file(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            (root/'.env-ui-port.json').write_text(json.dumps({'port':9123}),encoding='utf-8')
            response=Mock(status=200)
            opened=Mock()
            opened.__enter__=Mock(return_value=response)
            opened.__exit__=Mock(return_value=False)
            with patch('browser_backend.DATA_ROOT',root), \
                 patch('browser_backend.DEVTOOLS_PORT_FILE',root/'missing/DevToolsActivePort'), \
                 patch.object(BrowserBackend,'_cached_port',None), \
                 patch.object(BrowserBackend,'_port_from_process',side_effect=AssertionError('must not launch shell')), \
                 patch('browser_backend.LOOPBACK_HTTP.open',return_value=opened):
                self.assertEqual(BrowserBackend.debug_port(),9123)


if __name__=='__main__':unittest.main(verbosity=2)
